(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* Terminal format helpers (mirror hub/ui_format.py) */
  function formatTermPrefix(role) {
    const r = String(role || "").trim().toLowerCase();
    if (r === "user") return "You:";
    if (r === "assistant") return "Grok:";
    return "·";
  }

  function formatToolLine(title, status, summary) {
    const label = String(title || "tool").trim() || "tool";
    const parts = [label];
    const st = String(status || "").trim();
    if (st) parts.push(`[${st}]`);
    const snip = String(summary || "").trim();
    if (snip && !label.includes(snip)) parts.push(snip);
    return parts.join(" ");
  }

  function shouldShowToolLine() {
    return true;
  }

  function parseSimpleMarkdownTable(text) {
    if (!text || !String(text).includes("|")) return null;
    const lines = String(text).split(/\r?\n/);
    const sepRe = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/;
    const looseSepRe = /^\|?:?-{3,}:?(\|:?-{3,}:?)+\|?$/;

    function splitRow(line) {
      let s = line.trim();
      if (s.startsWith("|")) s = s.slice(1);
      if (s.endsWith("|")) s = s.slice(0, -1);
      return s.split("|").map((c) => c.trim());
    }

    for (let i = 0; i < lines.length - 1; i++) {
      const header = lines[i];
      const sep = lines[i + 1];
      if ((header.match(/\|/g) || []).length < 1) continue;
      const sepOk = sepRe.test(sep) || looseSepRe.test(sep.trim().replace(/\s+/g, ""));
      if (!sepOk) continue;
      const cellsHeader = splitRow(header);
      if (cellsHeader.length < 2) continue;
      const rows = [cellsHeader];
      let j = i + 2;
      while (j < lines.length) {
        const row = lines[j];
        if (!row.trim()) break;
        if ((row.match(/\|/g) || []).length < 1) break;
        let cells = splitRow(row);
        if (cells.length < cellsHeader.length) {
          cells = cells.concat(Array(cellsHeader.length - cells.length).fill(""));
        } else if (cells.length > cellsHeader.length) {
          cells = cells.slice(0, cellsHeader.length);
        }
        rows.push(cells);
        j += 1;
      }
      return rows;
    }
    return null;
  }

  function formatPlanSummary(entries) {
    const list = Array.isArray(entries) ? entries : [];
    const n = list.length;
    if (!n) return "plan (empty)";
    let done = 0;
    for (const e of list) {
      const st = normalizeStatus(e && e.status);
      if (st === "completed") done += 1;
    }
    return `plan ${done}/${n}`;
  }

  const state = {
    ws: null,
    wsState: "connecting", // connecting | open | reconnecting | closed
    reconnectAttempt: 0,
    reconnectTimer: null,
    status: {
      agent: "down",
      bind: "local",
      tailscaleIp: null,
      loadedSessionId: null,
      turnRunning: false,
      turnSessionId: null,
    },
    hubVersion: null,
    cliVersion: null,
    compatOk: null,
    compatIssues: [],
    hubSessionIds: [],
    sessionMode: "none", // none | history | live-remote
    attachSwitched: false, // true when live id != viewed foreign history id
    sessions: [],
    filter: "",
    selectedId: null,
    selectedMeta: null,
    commands: [],
    turnRunning: false,
    stickToBottom: true,
    _ignoreScroll: false,
    historyLoadedFor: null,
    historyFingerprint: null,
    historyPollTimer: null,
    streamBuffers: {
      assistantEl: null,
      thoughtEl: null,
      thoughtOpen: false,
      planEl: null,
      activityEl: null,
      tools: new Map(),
      lastToolTitle: "",
    },
    slashOpen: false,
    slashIndex: 0,
    slashItems: [],
    projects: [],
    turnStartedAt: 0,
    lastTermLineAt: 0,
    stallTimer: null,
    stallWarned: false,
    railTab: "sessions", // sessions | files
    mainMode: "chat", // chat | file
    fileViewMode: "edit", // edit | preview (meaningful for markdown only)
    mermaidReady: false,
    fs: {
      root: "",
      filter: "",
      expanded: new Set(),
      cache: new Map(), // rel path -> entries
      openPath: null,
      content: "",
      baseline: "",
      dirty: false,
      loading: false,
      error: null,
      saving: false,
    },
  };

  const els = {
    rail: $("#rail"),
    backdrop: $("#rail-backdrop"),
    sessionList: $("#session-list"),
    sessionEmpty: $("#session-empty"),
    sessionSearch: $("#session-search"),
    tabSessions: $("#tab-sessions"),
    tabFiles: $("#tab-files"),
    panelSessions: $("#panel-sessions"),
    panelFiles: $("#panel-files"),
    fileFilter: $("#file-filter"),
    fileTree: $("#file-tree"),
    fileEmpty: $("#file-empty"),
    chatPanel: $("#chat-panel"),
    filePanel: $("#file-panel"),
    filePathLabel: $("#file-path-label"),
    fileDirty: $("#file-dirty"),
    fileEditor: $("#file-editor"),
    filePreview: $("#file-preview"),
    fileMdModes: $("#file-md-modes"),
    fileStatus: $("#file-status"),
    btnFileBack: $("#btn-file-back"),
    btnFileEdit: $("#btn-file-edit"),
    btnFilePreview: $("#btn-file-preview"),
    btnFileInsert: $("#btn-file-insert"),
    btnFileSave: $("#btn-file-save"),
    transcript: $("#transcript"),
    btnJumpLatest: $("#btn-jump-latest"),
    emptyMain: $("#empty-main"),
    chatTitle: $("#chat-title"),
    chatModel: $("#chat-model"),
    chatCwd: $("#chat-cwd"),
    statusPill: $("#status-pill"),
    statusLabel: $("#status-label"),
    turnStrip: $("#turn-strip"),
    turnStripText: $("#turn-strip-text"),
    turnStripCursor: $("#turn-strip-cursor"),
    form: $("#composer-form"),
    input: $("#composer-input"),
    btnSend: $("#btn-send"),
    btnStop: $("#btn-stop"),
    composerHint: $("#composer-hint"),
    slash: $("#slash-palette"),
    btnMenu: $("#btn-menu"),
    btnNew: $("#btn-new"),
    btnEmptySessions: $("#btn-empty-sessions"),
    btnEmptyNew: $("#btn-empty-new"),
    modalNew: $("#modal-new"),
    projectList: $("#project-list"),
    projectSearch: $("#project-search"),
    projectEmpty: $("#project-empty"),
    projectNewName: $("#project-new-name"),
    btnCreateProject: $("#btn-create-project"),
    toastHost: $("#toast-host"),
    versionBadge: $("#version-badge"),
    versionLabel: $("#version-label"),
    compatDot: $("#compat-dot"),
    sessionBanner: $("#session-banner"),
    sessionBannerText: $("#session-banner-text"),
  };

  function tokenFromQuery() {
    const u = new URL(location.href);
    return u.searchParams.get("token") || "";
  }

  function apiUrl(path) {
    const t = tokenFromQuery();
    if (!t) return path;
    const join = path.includes("?") ? "&" : "?";
    return `${path}${join}token=${encodeURIComponent(t)}`;
  }

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const t = tokenFromQuery();
    const q = t ? `?token=${encodeURIComponent(t)}` : "";
    return `${proto}//${location.host}/ws${q}`;
  }

  function toast(message, kind = "") {
    const el = document.createElement("div");
    el.className = `toast${kind ? " " + kind : ""}`;
    el.textContent = message;
    els.toastHost.appendChild(el);
    setTimeout(() => {
      el.remove();
    }, 4200);
  }

  function relativeTime(iso) {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    const sec = Math.round((Date.now() - t) / 1000);
    if (sec < 60) return "just now";
    const min = Math.round(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.round(min / 60);
    if (hr < 48) return `${hr}h ago`;
    const days = Math.round(hr / 24);
    return `${days}d ago`;
  }

  function basename(p) {
    if (!p) return "";
    const parts = p.replace(/[\\/]+$/, "").split(/[/\\]/);
    return parts[parts.length - 1] || p;
  }

  function truncate(s, limit = 120) {
    s = String(s || "").trim();
    if (s.length <= limit) return s;
    return s.slice(0, Math.max(0, limit - 1)).replace(/\s+$/, "") + "…";
  }

  function normalizeStatus(status) {
    if (status == null) return "pending";
    if (typeof status === "object") {
      status = status.status || status.state || "pending";
    }
    const s = String(status).trim().toLowerCase();
    if (!s) return "pending";
    if (["pending", "queued", "waiting"].includes(s)) return "pending";
    if (["running", "in_progress", "in-progress", "active", "started"].includes(s)) return "running";
    if (["completed", "complete", "ok", "success", "succeeded", "done"].includes(s)) return "completed";
    if (["failed", "error", "errored"].includes(s)) return "failed";
    if (["cancelled", "canceled", "aborted"].includes(s)) return "cancelled";
    if (s.includes("complete") || s === "ok" || s === "success") return "completed";
    if (s.includes("fail") || s.includes("error")) return "failed";
    if (s.includes("cancel") || s.includes("abort")) return "cancelled";
    if (s.includes("run") || s.includes("progress")) return "running";
    return s;
  }

  function toolLabelFromUpdate(update) {
    const meta = update._meta || {};
    const xai = meta["x.ai/tool"];
    if (xai && (xai.label || xai.name)) return String(xai.label || xai.name);
    if (update.title) return String(update.title);
    if (update.tool) return String(update.tool);
    return "tool";
  }

  function toolSummaryFromUpdate(update) {
    const raw = update.rawInput;
    if (raw == null) {
      return extractToolContentSnippet(update);
    }
    if (typeof raw === "string") return truncate(raw);
    if (typeof raw !== "object" || Array.isArray(raw)) return truncate(String(raw));

    const pathKeys = [
      "target_file",
      "path",
      "file",
      "file_path",
      "filepath",
      "filename",
      "cwd",
      "directory",
      "dir",
      "url",
      "uri",
    ];
    for (const key of pathKeys) {
      if (raw[key] != null && String(raw[key]).trim()) return truncate(String(raw[key]).trim());
    }
    for (const key of ["command", "cmd", "shell", "script"]) {
      if (raw[key] != null && String(raw[key]).trim()) return truncate(String(raw[key]).trim());
    }
    for (const key of ["pattern", "query", "search", "q", "grep"]) {
      if (raw[key] != null && String(raw[key]).trim()) return truncate(String(raw[key]).trim());
    }
    const parts = [];
    for (const [k, v] of Object.entries(raw).slice(0, 4)) {
      if (v == null || typeof v === "object") continue;
      const sv = String(v).trim();
      if (!sv) continue;
      parts.push(`${k}=${truncate(sv, 40)}`);
      if (parts.join(", ").length >= 120) break;
    }
    if (parts.length) return truncate(parts.join(", "));
    return "";
  }

  function extractToolContentSnippet(update) {
    const content = update.content;
    if (content == null) return "";
    if (Array.isArray(content)) {
      const parts = [];
      for (const item of content) {
        if (item && typeof item === "object") {
          const inner = item.content != null ? item.content : item;
          const t = extractText(inner);
          if (t.trim()) parts.push(t.trim());
        } else {
          const t = extractText(item);
          if (t.trim()) parts.push(t.trim());
        }
      }
      return truncate(parts.join(" "));
    }
    return truncate(extractText(content));
  }

  function statusClass(status) {
    const s = normalizeStatus(status);
    if (s === "completed") return "ok";
    if (s === "failed") return "fail";
    if (s === "cancelled") return "cancel";
    if (s === "running") return "running";
    return "pending";
  }

  function shortVersion(v) {
    if (!v) return "?";
    const s = String(v).trim();
    // "grok 0.2.93 (hash) [stable]" -> "0.2.93"
    const m = s.match(/\b(\d+\.\d+(?:\.\d+)?)\b/);
    if (m) return m[1];
    return s.length > 18 ? s.slice(0, 16) + "…" : s;
  }

  function updateVersionBadge() {
    if (!els.versionLabel || !els.compatDot) return;
    const hub = shortVersion(state.hubVersion || "?");
    const cli = shortVersion(state.cliVersion || "?");
    els.versionLabel.textContent = `Hub ${hub} · CLI ${cli}`;
    let dot = "unknown";
    let title = "Compatibility not checked yet";
    if (state.compatOk === true) {
      dot = "ok";
      title = "Structural compatibility OK";
    } else if (state.compatOk === false) {
      dot = "warn";
      const issues = (state.compatIssues || []).slice(0, 4).join("; ");
      title = issues || "Compatibility issues";
    }
    els.compatDot.dataset.state = dot;
    if (els.versionBadge) els.versionBadge.title = title;
  }

  function setSessionMode(mode, opts) {
    state.sessionMode = mode || "none";
    if (opts && typeof opts.attachSwitched === "boolean") {
      state.attachSwitched = opts.attachSwitched;
    }
    updateSessionBanner();
  }

  function updateSessionBanner() {
    if (!els.sessionBanner || !els.sessionBannerText) return;
    const mode = state.sessionMode || "none";
    if (mode === "none" || !state.selectedId) {
      els.sessionBanner.classList.add("hidden");
      els.sessionBanner.dataset.mode = "none";
      els.sessionBannerText.textContent = "";
      return;
    }
    els.sessionBanner.classList.remove("hidden");
    els.sessionBanner.dataset.mode = mode;
    if (mode === "live-remote") {
      if (state.attachSwitched) {
        els.sessionBannerText.textContent =
          "Live remote session for this project. Desktop TUI history is separate.";
      } else {
        els.sessionBannerText.textContent = "Live remote session";
      }
    } else {
      els.sessionBannerText.textContent =
        "Viewing saved history. Attach starts a live remote session (desktop TUI stays separate).";
    }
  }

  function isHubCreatedSession(sessionId) {
    if (!sessionId) return false;
    const ids = state.hubSessionIds || [];
    return ids.indexOf(sessionId) >= 0;
  }

  function updateStatusPill() {
    const pill = els.statusPill;
    const label = els.statusLabel;
    let stateKey = "connecting";
    let text = "Connecting";

    if (state.wsState === "reconnecting" || state.wsState === "connecting") {
      stateKey = state.wsState === "reconnecting" ? "reconnecting" : "connecting";
      text = state.wsState === "reconnecting" ? "Reconnecting" : "Connecting";
    } else if (state.wsState === "open") {
      if (state.status.agent !== "up") {
        stateKey = "agent-down";
        text = "Agent down";
      } else if (state.status.bind === "local") {
        stateKey = "local";
        text = "Local only";
      } else {
        stateKey = "connected";
        text = "Connected";
      }
    } else {
      stateKey = "reconnecting";
      text = "Reconnecting";
    }

    pill.dataset.state = stateKey;
    label.textContent = text;
    updateTurnStrip();
  }

  // Client soft-warn only (matches hub.session_policy CLIENT_STALL_WARN_SECONDS).
  // Auto unlock/reset-turn is disabled (CLIENT_STALL_UNLOCK_SECONDS = 0).
  const CLIENT_STALL_WARN_MS = 120000;

  function updateTurnStrip() {
    if (!els.turnStrip || !els.turnStripText) return;
    const running = !!state.turnRunning && !!state.selectedId;
    const model =
      (state.selectedMeta && state.selectedMeta.modelId) ||
      (els.chatModel && !els.chatModel.classList.contains("hidden") ? els.chatModel.textContent : "") ||
      "";
    const tool = (state.streamBuffers && state.streamBuffers.lastToolTitle) || "";
    const idleMs = running
      ? Date.now() - (state.lastTermLineAt || state.turnStartedAt || Date.now())
      : 0;
    // Visual quiet cue only — never unlocks or ends the turn.
    const quietVisual = running && idleMs >= CLIENT_STALL_WARN_MS;

    if (running) {
      els.turnStrip.dataset.state = quietVisual ? "stalled" : "running";
      const parts = ["running"];
      if (tool) parts.push(tool);
      if (model) parts.push(model);
      els.turnStripText.textContent = parts.join(" · ");
      if (els.turnStripCursor) els.turnStripCursor.classList.remove("hidden");
    } else {
      els.turnStrip.dataset.state = "idle";
      const parts = ["idle"];
      if (model && state.selectedId) parts.push(model);
      els.turnStripText.textContent = parts.join(" · ");
      if (els.turnStripCursor) els.turnStripCursor.classList.add("hidden");
    }
  }

  function setComposerEnabled(on) {
    const canSend = on && !state.turnRunning && state.selectedId;
    els.input.disabled = !on || !state.selectedId || state.turnRunning;
    els.btnSend.disabled = !canSend;
    els.btnStop.classList.toggle("hidden", !state.turnRunning || !state.selectedId);
    if (!state.selectedId) {
      els.composerHint.textContent =
        "Remote agent stream. Load a session to chat; desktop TUI stays separate.";
    } else if (state.turnRunning) {
      els.composerHint.textContent = "Turn running…";
    } else if (state.sessionMode === "history") {
      els.composerHint.textContent =
        "History view. Opening attaches a live remote session (not the desktop TUI).";
    } else if (!state.commands.length) {
      els.composerHint.textContent =
        "Live remote stream. Slash commands appear when the agent sends them.";
    } else {
      els.composerHint.textContent = `${state.commands.length} slash commands available. Type / to open palette.`;
    }
    updateTurnStrip();
  }

  function clearStallWatch() {
    if (state.stallTimer) {
      clearInterval(state.stallTimer);
      state.stallTimer = null;
    }
    state.stallWarned = false;
  }

  function noteTermLineActivity() {
    state.lastTermLineAt = Date.now();
    // Activity resets soft-warn baseline; never auto-unlocks.
  }

  function startStallWatch() {
    clearStallWatch();
    state.turnStartedAt = Date.now();
    state.lastTermLineAt = Date.now();
    state.stallWarned = false;
    state.stallTimer = setInterval(() => {
      if (!state.turnRunning) {
        clearStallWatch();
        return;
      }
      const idleMs = Date.now() - (state.lastTermLineAt || state.turnStartedAt || Date.now());
      updateTurnStrip();
      // Soft warn only (TUI-aligned): never reset-turn or unlock the client.
      if (!state.stallWarned && idleMs >= CLIENT_STALL_WARN_MS) {
        state.stallWarned = true;
        toast(
          "Still working (like desktop TUI). Use Stop to cancel.",
          ""
        );
        setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
      }
    }, 2000);
  }

  function setTurnRunning(running) {
    state.turnRunning = !!running;
    if (running) {
      startStallWatch();
    } else {
      clearStallWatch();
      if (state.streamBuffers) state.streamBuffers.lastToolTitle = "";
    }
    setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
  }

  async function applySessionSwitch(fromId, toId, reason, message) {
    if (!toId) return;
    const from = fromId || state.selectedId;
    toast(message || "Remote session started for live streaming", "");
    // Prefer meta from list; fall back to previous cwd
    let meta = state.sessions.find((s) => s.sessionId === toId);
    if (!meta) {
      meta = {
        sessionId: toId,
        title: "Remote session",
        cwd: (state.selectedMeta && state.selectedMeta.cwd) || "",
        updatedAt: new Date().toISOString(),
        modelId: (state.selectedMeta && state.selectedMeta.modelId) || "",
        path: "",
      };
      state.sessions = [meta, ...state.sessions.filter((s) => s.sessionId !== toId)];
    }
    state.selectedId = toId;
    state.selectedMeta = meta;
    state.commands = [];
    state.streamBuffers = emptyStreamBuffers();
    state.historyLoadedFor = null;
    state.historyFingerprint = null;
    // Hub-created remote session is live stream, not disk history
    if (state.hubSessionIds.indexOf(toId) < 0) {
      state.hubSessionIds = [toId, ...state.hubSessionIds].slice(0, 50);
    }
    setSessionMode("live-remote", {
      attachSwitched: !!(from && from !== toId),
    });

    els.chatTitle.textContent = meta.title || "Remote session";
    if (meta.modelId) {
      els.chatModel.textContent = meta.modelId;
      els.chatModel.classList.remove("hidden");
    }
    els.chatCwd.textContent = meta.cwd || "";
    renderSessions();

    // Fresh stream view for the hub-owned session (system line may arrive via type:system)
    clearTranscript();
    showEmptyMain(false);
    if (message) {
      appendMessage({ role: "system", text: message });
    }
    noteTermLineActivity();

    sendWs({ type: "subscribe", sessionId: toId });
    if (from && from !== toId) {
      sendWs({ type: "unsubscribe", sessionId: from });
    }
    setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
    updateTurnStrip();
  }

  function openRail() {
    els.rail.classList.add("open");
    els.backdrop.hidden = false;
  }

  function closeRail() {
    els.rail.classList.remove("open");
    els.backdrop.hidden = true;
  }

  function renderSessions() {
    const q = state.filter.trim().toLowerCase();
    const items = state.sessions.filter((s) => {
      if (!q) return true;
      return (
        (s.title || "").toLowerCase().includes(q) ||
        (s.cwd || "").toLowerCase().includes(q) ||
        (s.sessionId || "").toLowerCase().startsWith(q) ||
        basename(s.cwd).toLowerCase().includes(q)
      );
    });

    els.sessionList.innerHTML = "";
    els.sessionEmpty.classList.toggle("hidden", items.length > 0);
    if (items.length === 0) {
      const emptyP = els.sessionEmpty.querySelector("p");
      if (emptyP) {
        if (!state.sessions.length) {
          emptyP.textContent = "No sessions yet. Tap New to start one in a project folder.";
        } else if (q) {
          emptyP.textContent = "No sessions match that search.";
        } else {
          emptyP.textContent = "No sessions to show.";
        }
      }
    }

    for (const s of items) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "session-row";
      btn.setAttribute("role", "listitem");
      if (s.sessionId === state.selectedId) btn.classList.add("active");
      if (s.sessionId === state.status.loadedSessionId) btn.classList.add("live");

      const bar = document.createElement("span");
      bar.className = "live-bar";
      bar.setAttribute("aria-hidden", "true");

      const body = document.createElement("span");
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = s.title || "Untitled session";
      const meta = document.createElement("div");
      meta.className = "meta";
      const proj = document.createElement("span");
      proj.textContent = basename(s.cwd) || "project";
      const time = document.createElement("span");
      time.textContent = relativeTime(s.updatedAt);
      meta.append(proj, time);
      body.append(title, meta);
      btn.append(bar, body);
      btn.addEventListener("click", () => openSession(s));
      els.sessionList.appendChild(btn);
    }
  }

  function emptyStreamBuffers() {
    return {
      assistantEl: null,
      thoughtEl: null,
      thoughtOpen: false,
      planEl: null,
      activityEl: null,
      tools: new Map(),
      lastToolTitle: "",
    };
  }

  function clearTranscript() {
    els.transcript.innerHTML = "";
    state.streamBuffers = emptyStreamBuffers();
  }

  function showEmptyMain(show) {
    if (show) {
      if (!$("#empty-main", els.transcript)) {
        const wrap = document.createElement("div");
        wrap.id = "empty-main";
        wrap.className = "empty-main";
        wrap.innerHTML = `
          <div class="empty-card">
            <h2>$ attach stream</h2>
            <p class="empty-sub">Remote agent stream over Tailscale</p>
            <p>Open a session to attach the stream, or start a new one in a project folder.</p>
            <div class="empty-actions">
              <button type="button" id="btn-empty-sessions" class="btn btn-ghost">Browse sessions</button>
              <button type="button" id="btn-empty-new" class="btn btn-accent">New session</button>
            </div>
          </div>`;
        els.transcript.appendChild(wrap);
        $("#btn-empty-sessions", wrap).addEventListener("click", openRail);
        $("#btn-empty-new", wrap).addEventListener("click", openNewModal);
      }
      setSessionMode("none");
    } else {
      const em = $("#empty-main", els.transcript);
      if (em) em.remove();
    }
  }

  function distanceFromBottom() {
    const el = els.transcript;
    return el.scrollHeight - el.scrollTop - el.clientHeight;
  }

  function scrollTranscriptToBottom() {
    const el = els.transcript;
    if (!el) return;
    state._ignoreScroll = true;
    el.scrollTop = el.scrollHeight;
    // release after layout so mid-scroll frames don't flip stickToBottom
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        state._ignoreScroll = false;
        if (state.stickToBottom) el.scrollTop = el.scrollHeight;
        updateJumpLatestUiOnly();
      });
    });
  }

  function updateJumpLatestUiOnly() {
    if (!els.btnJumpLatest) return;
    const nearBottom = distanceFromBottom() < 80;
    els.btnJumpLatest.classList.toggle("hidden", nearBottom || !state.selectedId);
  }

  function updateJumpLatest() {
    if (state._ignoreScroll) {
      updateJumpLatestUiOnly();
      return;
    }
    if (!els.btnJumpLatest) return;
    const nearBottom = distanceFromBottom() < 80;
    state.stickToBottom = nearBottom;
    els.btnJumpLatest.classList.toggle("hidden", nearBottom || !state.selectedId);
  }

  function jumpToLatest() {
    state.stickToBottom = true;
    scrollTranscriptToBottom();
    updateJumpLatestUiOnly();
  }

  function scrollIfSticky() {
    if (!state.stickToBottom) {
      updateJumpLatestUiOnly();
      return;
    }
    scrollTranscriptToBottom();
  }

  function setTermBodyContent(bodyEl, text) {
    const raw = text == null ? "" : String(text);
    const table = parseSimpleMarkdownTable(raw);
    if (!table || table.length < 1) {
      bodyEl.textContent = raw;
      return;
    }

    // Split surrounding text and table block for mixed content
    bodyEl.innerHTML = "";
    const lines = raw.split(/\r?\n/);
    let tableStart = -1;
    const sepRe = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/;
    const looseSepRe = /^\|?:?-{3,}:?(\|:?-{3,}:?)+\|?$/;
    for (let i = 0; i < lines.length - 1; i++) {
      if ((lines[i].match(/\|/g) || []).length < 1) continue;
      if (sepRe.test(lines[i + 1]) || looseSepRe.test(lines[i + 1].trim().replace(/\s+/g, ""))) {
        tableStart = i;
        break;
      }
    }
    if (tableStart < 0) {
      bodyEl.textContent = raw;
      return;
    }

    const before = lines.slice(0, tableStart).join("\n");
    let tableEnd = tableStart + 2;
    while (tableEnd < lines.length && lines[tableEnd].trim() && (lines[tableEnd].match(/\|/g) || []).length >= 1) {
      tableEnd += 1;
    }
    const after = lines.slice(tableEnd).join("\n");

    if (before.trim()) {
      const pre = document.createElement("span");
      pre.textContent = before + (before.endsWith("\n") ? "" : "\n");
      bodyEl.appendChild(pre);
    }

    const wrap = document.createElement("div");
    wrap.className = "term-table-wrap";
    const tbl = document.createElement("table");
    tbl.className = "term-table";
    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    for (const cell of table[0]) {
      const th = document.createElement("th");
      th.textContent = cell;
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    tbl.appendChild(thead);
    if (table.length > 1) {
      const tbody = document.createElement("tbody");
      for (let r = 1; r < table.length; r++) {
        const tr = document.createElement("tr");
        for (const cell of table[r]) {
          const td = document.createElement("td");
          td.textContent = cell;
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
      tbl.appendChild(tbody);
    }
    wrap.appendChild(tbl);
    bodyEl.appendChild(wrap);

    if (after.trim()) {
      const post = document.createElement("span");
      post.textContent = (after.startsWith("\n") ? "" : "\n") + after;
      bodyEl.appendChild(post);
    }
  }

  function renderPlanBody(body, entries) {
    body.innerHTML = "";
    const ul = document.createElement("ul");
    ul.className = "plan-list";
    for (const e of entries || []) {
      const li = document.createElement("li");
      li.className = "plan-item";
      const st = normalizeStatus(e.status);
      li.dataset.status = st;
      const dot = document.createElement("span");
      dot.className = `status-dot plan-dot ${statusClass(st)}`;
      dot.setAttribute("aria-hidden", "true");
      const text = document.createElement("span");
      text.className = "plan-item-text";
      text.textContent = e.content || "";
      li.append(dot, text);
      ul.appendChild(li);
    }
    body.appendChild(ul);
  }

  function appendPlanMessage(msg) {
    const entries = (msg.meta && msg.meta.entries) || [];
    const details = document.createElement("details");
    details.className = "term-line plan";
    details.open = false;
    const summary = document.createElement("summary");
    const prefix = document.createElement("span");
    prefix.className = "term-prefix";
    prefix.textContent = formatTermPrefix("plan");
    const label = document.createElement("span");
    label.className = "plan-summary-label";
    label.textContent = formatPlanSummary(entries);
    summary.append(prefix, label);
    const body = document.createElement("div");
    body.className = "term-body plan-body";
    renderPlanBody(body, entries);
    details.append(summary, body);
    details._planEntries = entries;
    els.transcript.appendChild(details);
    scrollIfSticky();
    return details;
  }

  function upsertPlanLive(entries) {
    let el = state.streamBuffers.planEl;
    if (!el || !el.isConnected) {
      el = appendPlanMessage({ role: "plan", text: "", meta: { entries, kind: "plan" } });
      state.streamBuffers.planEl = el;
    } else {
      el._planEntries = entries;
      const label = el.querySelector(".plan-summary-label");
      if (label) label.textContent = formatPlanSummary(entries);
      const body = el.querySelector(".plan-body") || el.querySelector(".term-body");
      if (body) renderPlanBody(body, entries);
    }
    el.open = false;
    scrollIfSticky();
    return el;
  }

  function createToolLine({ title, status, summary, toolCallId }) {
    const row = document.createElement("div");
    row.className = "term-line tool";
    const st = normalizeStatus(status || "pending");
    row.dataset.status = st;
    if (st === "running" || st === "pending") row.classList.add("running");
    if (toolCallId) row.dataset.toolCallId = toolCallId;

    const prefix = document.createElement("span");
    prefix.className = "term-prefix";
    prefix.textContent = formatTermPrefix("tool");

    const body = document.createElement("span");
    body.className = "term-body";

    const name = document.createElement("span");
    name.className = "tool-name";
    name.textContent = title || "tool";

    const pill = document.createElement("span");
    pill.className = `term-status ${statusClass(st)}`;
    pill.textContent = st;

    body.append(name, document.createTextNode(" "), pill);
    const snip = (summary || "").trim();
    if (snip && !String(title || "").includes(snip)) {
      body.append(document.createTextNode(" " + truncate(snip)));
    }

    row.append(prefix, body);
    row._toolTitle = title || "tool";
    row._toolSummary = snip;
    return row;
  }

  function updateToolLine(row, { title, status, summary }) {
    if (!row) return;
    if (status != null) {
      const st = normalizeStatus(status);
      row.dataset.status = st;
      row.classList.toggle("running", st === "running" || st === "pending");
      const pill = row.querySelector(".term-status");
      if (pill) {
        pill.textContent = st;
        pill.className = `term-status ${statusClass(st)}`;
      }
    }
    if (title) {
      const name = row.querySelector(".tool-name");
      if (name) name.textContent = title;
      row._toolTitle = title;
    }
    if (summary != null && String(summary).trim()) {
      row._toolSummary = String(summary).trim();
      // Rebuild body text after name/status
      const body = row.querySelector(".term-body");
      if (body) {
        const nameEl = body.querySelector(".tool-name");
        const pillEl = body.querySelector(".term-status");
        const nameText = (nameEl && nameEl.textContent) || row._toolTitle || "tool";
        const stText = (pillEl && pillEl.textContent) || row.dataset.status || "";
        body.innerHTML = "";
        const name = document.createElement("span");
        name.className = "tool-name";
        name.textContent = nameText;
        const pill = document.createElement("span");
        pill.className = `term-status ${statusClass(row.dataset.status)}`;
        pill.textContent = stText;
        body.append(name, document.createTextNode(" "), pill);
        const snip = truncate(row._toolSummary);
        if (snip && !nameText.includes(snip)) {
          body.append(document.createTextNode(" " + snip));
        }
      }
    }
  }

  function appendToolLine(meta, text) {
    if (!shouldShowToolLine()) return null;
    noteTermLineActivity();
    const title = text || meta.label || "tool";
    const summary = meta.summary || meta.detail || "";
    const st = meta.status || "pending";
    const id = meta.toolCallId || "";

    // Update existing by id if present
    if (id && state.streamBuffers.tools.has(id)) {
      const existing = state.streamBuffers.tools.get(id);
      if (existing && existing.isConnected) {
        updateToolLine(existing, { title, status: st, summary });
        state.streamBuffers.lastToolTitle = title;
        updateTurnStrip();
        scrollIfSticky();
        return existing;
      }
    }

    const row = createToolLine({
      title,
      status: st,
      summary,
      toolCallId: id,
    });
    els.transcript.appendChild(row);
    if (id) state.streamBuffers.tools.set(id, row);
    state.streamBuffers.lastToolTitle = title;
    updateTurnStrip();
    scrollIfSticky();
    return row;
  }

  function appendMessage(msg, opts = {}) {
    const role = msg.role || "system";
    const text = msg.text || "";
    const meta = msg.meta || {};
    noteTermLineActivity();

    if (role === "thought") {
      const details = document.createElement("details");
      details.className = "term-line thought";
      details.open = !!opts.open;
      const summary = document.createElement("summary");
      const prefix = document.createElement("span");
      prefix.className = "term-prefix";
      prefix.textContent = formatTermPrefix("thought");
      const label = document.createElement("span");
      label.className = "thought-summary-label";
      label.textContent = text ? "thinking…" : "thinking…";
      summary.append(prefix, label);
      const body = document.createElement("div");
      body.className = "term-body";
      body.textContent = text;
      details.append(summary, body);
      els.transcript.appendChild(details);
      if (opts.stream) state.streamBuffers.thoughtEl = details;
      scrollIfSticky();
      return details;
    }

    if (role === "plan") {
      const el = appendPlanMessage(msg);
      if (opts.stream) state.streamBuffers.planEl = el;
      return el;
    }

    if (role === "tool") {
      return appendToolLine(meta, text);
    }

    const div = document.createElement("div");
    div.className = `term-line ${role}`;

    const prefix = document.createElement("span");
    prefix.className = "term-prefix";
    prefix.textContent = formatTermPrefix(role);

    const body = document.createElement("span");
    body.className = "term-body";
    if (role === "assistant" || role === "user") {
      setTermBodyContent(body, text);
    } else {
      body.textContent = text;
    }

    div.append(prefix, body);
    els.transcript.appendChild(div);
    if (opts.stream && role === "assistant") state.streamBuffers.assistantEl = div;
    scrollIfSticky();
    return div;
  }

  function historyFingerprint(messages) {
    if (!messages || !messages.length) return "empty";
    const last = messages.slice(-3);
    return last
      .map((m) => {
        const text = String((m && m.text) || "");
        return `${(m && m.role) || ""}:${text.length}:${text.slice(-48)}`;
      })
      .join("|");
  }

  function applyHistoryMessages(messages, opts = {}) {
    // Live stream owns the transcript while a turn is running (unless forced open)
    if (state.turnRunning && !opts.force) return false;
    const list = messages || [];
    const fp = historyFingerprint(list);
    const sessionId = state.selectedId;
    if (
      !opts.force &&
      fp === state.historyFingerprint &&
      state.historyLoadedFor === sessionId
    ) {
      return false;
    }
    const wasNearBottom = state.stickToBottom || distanceFromBottom() < 120;
    renderHistory(list);
    state.historyLoadedFor = sessionId;
    state.historyFingerprint = fp;
    if (wasNearBottom || opts.jump) {
      jumpToLatest();
    }
    return true;
  }

  function renderHistory(messages) {
    clearTranscript();
    showEmptyMain(false);
    if (!messages || !messages.length) {
      appendMessage({ role: "system", text: "No prior transcript on disk for this session." });
      return;
    }
    for (const m of messages) {
      appendMessage(m);
    }
    state.streamBuffers.assistantEl = null;
    state.streamBuffers.thoughtEl = null;
    state.streamBuffers.planEl = null;
    state.streamBuffers.activityEl = null;
    state.streamBuffers.tools = new Map();
    state.streamBuffers.lastToolTitle = "";
    state.stickToBottom = true;
    scrollIfSticky();
    updateTurnStrip();
  }

  function extractText(content) {
    if (!content) return "";
    if (typeof content === "string") return content;
    if (typeof content === "object") {
      if (content.text) return String(content.text);
      if (Array.isArray(content.content)) return content.content.map(extractText).join("");
    }
    return "";
  }

  function beginNewUserTurn() {
    state.streamBuffers.assistantEl = null;
    state.streamBuffers.thoughtEl = null;
    state.streamBuffers.planEl = null;
    state.streamBuffers.activityEl = null;
    state.streamBuffers.tools = new Map();
    state.streamBuffers.lastToolTitle = "";
    updateTurnStrip();
  }

  function appendToBody(el, text) {
    if (!el) return;
    noteTermLineActivity();
    const body = el.querySelector(".term-body");
    if (!body) return;
    // Streaming: prefer plain text append for speed; re-parse table only if pipes appear
    if (body.querySelector(".term-table")) {
      setTermBodyContent(body, (body._rawText || body.textContent || "") + text);
      body._rawText = (body._rawText || "") + text;
      return;
    }
    body._rawText = (body._rawText || body.textContent || "") + text;
    if (body._rawText.includes("|") && body._rawText.includes("\n")) {
      setTermBodyContent(body, body._rawText);
    } else {
      body.textContent = body._rawText;
    }
  }

  function handleAcpMessage(sessionId, message) {
    if (!sessionId || sessionId !== state.selectedId) return;
    const method = message.method || "";
    if (method !== "session/update" && method !== "_x.ai/session/update") {
      return;
    }
    const update = (message.params && message.params.update) || {};
    const kind = update.sessionUpdate || "";

    if (kind === "user_message_chunk") {
      const text = extractText(update.content);
      if (!text) return;
      const last = els.transcript.lastElementChild;
      if (last && last.classList.contains("user") && last.classList.contains("term-line")) {
        const body = last.querySelector(".term-body");
        if (!body) return;
        const existing = body._rawText != null ? String(body._rawText) : String(body.textContent || "");
        // Exact duplicate (hub echo + ACP full message)
        if (existing === text) {
          scrollIfSticky();
          return;
        }
        // Already have this text as prefix (ACP re-sends shorter/same)
        if (existing.startsWith(text)) {
          scrollIfSticky();
          return;
        }
        // Replacement with longer full message that extends existing stream
        if (text.startsWith(existing)) {
          setTermBodyContent(body, text);
          body._rawText = text;
          scrollIfSticky();
          return;
        }
        // True streaming chunk
        appendToBody(last, text);
      } else {
        beginNewUserTurn();
        const el = appendMessage({ role: "user", text }, { stream: true });
        const body = el && el.querySelector(".term-body");
        if (body) body._rawText = text;
      }
      scrollIfSticky();
      return;
    }

    if (kind === "agent_message_chunk") {
      const text = extractText(update.content);
      if (!text) return;
      let el = state.streamBuffers.assistantEl;
      if (!el || !el.isConnected) {
        el = appendMessage({ role: "assistant", text: "" }, { stream: true });
        state.streamBuffers.assistantEl = el;
        const body = el.querySelector(".term-body");
        if (body) body._rawText = "";
      }
      appendToBody(el, text);
      scrollIfSticky();
      return;
    }

    if (kind === "agent_thought_chunk") {
      const text = extractText(update.content);
      if (!text) return;
      noteTermLineActivity();
      let el = state.streamBuffers.thoughtEl;
      if (!el || !el.isConnected) {
        // Keep thought open while chunks arrive (TUI-like live stream).
        el = appendMessage({ role: "thought", text: "" }, { stream: true, open: true });
        state.streamBuffers.thoughtEl = el;
        state.streamBuffers.thoughtOpen = true;
      } else if (!el.open) {
        el.open = true;
        state.streamBuffers.thoughtOpen = true;
      }
      const body = el.querySelector(".term-body");
      if (body) body.textContent += text;
      const label = el.querySelector(".thought-summary-label");
      if (label) label.textContent = "thinking…";
      scrollIfSticky();
      return;
    }

    if (kind === "plan") {
      const entriesIn = update.entries || [];
      const entries = (Array.isArray(entriesIn) ? entriesIn : []).map((e) => ({
        content: (e && e.content) || "",
        status: normalizeStatus(e && e.status),
        priority: (e && e.priority) || "",
      }));
      upsertPlanLive(entries);
      return;
    }

    if (kind === "tool_call") {
      const id = update.toolCallId || "";
      const label = toolLabelFromUpdate(update);
      const summary = toolSummaryFromUpdate(update);
      const title =
        summary && label && !summary.toLowerCase().includes(label.toLowerCase())
          ? truncate(`${label} ${summary}`, 160)
          : summary || label;
      const status = update.status != null ? normalizeStatus(update.status) : "pending";
      const row = appendToolLine(
        {
          toolCallId: id,
          status,
          summary,
          detail: summary,
          label,
        },
        title
      );
      if (id && row) state.streamBuffers.tools.set(id, row);
      state.streamBuffers.assistantEl = null;
      state.streamBuffers.lastToolTitle = title;
      updateTurnStrip();
      return;
    }

    if (kind === "tool_call_update") {
      const id = update.toolCallId || "";
      const status = normalizeStatus(update.status);
      const title = update.title || toolLabelFromUpdate(update);
      const snippet = extractToolContentSnippet(update) || toolSummaryFromUpdate(update);
      noteTermLineActivity();
      let row = id ? state.streamBuffers.tools.get(id) : null;
      if (!row || !row.isConnected) {
        row = appendToolLine(
          {
            toolCallId: id,
            status,
            summary: snippet,
            detail: snippet,
            label: title,
          },
          title
        );
        if (id && row) state.streamBuffers.tools.set(id, row);
      } else {
        updateToolLine(row, { title, status, summary: snippet });
      }
      state.streamBuffers.lastToolTitle = title;
      updateTurnStrip();
      scrollIfSticky();
      return;
    }

    if (kind === "available_commands_update") {
      const cmds = update.availableCommands || [];
      state.commands = Array.isArray(cmds) ? cmds : [];
      setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
      return;
    }
  }

  async function openSession(session) {
    if (state.turnRunning && state.selectedId && state.selectedId !== session.sessionId) {
      toast("Wait for the current turn to finish before switching sessions.", "danger");
      return;
    }
    if (
      state.fs.dirty &&
      state.selectedId &&
      state.selectedId !== session.sessionId
    ) {
      if (!window.confirm("Discard unsaved changes?")) return;
      state.fs.dirty = false;
    }

    const viewId = session.sessionId;
    state.selectedId = viewId;
    state.selectedMeta = session;
    state.commands = [];
    state.streamBuffers = emptyStreamBuffers();
    state.attachSwitched = false;
    // Disk open = history until attach promotes to live-remote
    if (isHubCreatedSession(viewId)) {
      setSessionMode("live-remote", { attachSwitched: false });
    } else {
      setSessionMode("history", { attachSwitched: false });
    }

    els.chatTitle.textContent = session.title || "Untitled session";
    if (session.modelId) {
      els.chatModel.textContent = session.modelId;
      els.chatModel.classList.remove("hidden");
    } else {
      els.chatModel.classList.add("hidden");
    }
    els.chatCwd.textContent = session.cwd || "";
    renderSessions();
    syncFsForSession();
    closeRail();
    setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
    updateTurnStrip();

    // History for the viewed session first (catch-up / TUI context)
    try {
      const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(viewId)}/history`));
      const data = await res.json();
      applyHistoryMessages(data.messages || [], { force: true, jump: true });
    } catch (err) {
      clearTranscript();
      showEmptyMain(false);
      appendMessage({ role: "system", text: "Failed to load history: " + err });
      state.historyLoadedFor = null;
      state.historyFingerprint = null;
    }

    // Attach-on-open: ensure live hub session for cwd (no foreign session/load)
    let liveId = viewId;
    let switched = false;
    try {
      const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(viewId)}/attach`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cwd: session.cwd || "" }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        // Agent down or attach failed: stay on history view + subscribe view
        toast(data.error || "Attach failed — history only until agent is up", "danger");
        sendWs({ type: "subscribe", sessionId: viewId });
        els.input.focus();
        return;
      }
      liveId = data.liveSessionId || viewId;
      switched = !!data.switched && liveId !== viewId;
      const cwd = data.cwd || session.cwd || "";

      if (state.hubSessionIds.indexOf(liveId) < 0) {
        state.hubSessionIds = [liveId, ...state.hubSessionIds].slice(0, 50);
      }

      if (switched) {
        let meta = state.sessions.find((s) => s.sessionId === liveId);
        if (!meta) {
          meta = {
            sessionId: liveId,
            title: "Remote session",
            cwd,
            updatedAt: new Date().toISOString(),
            modelId: (session && session.modelId) || "",
            path: "",
          };
          state.sessions = [meta, ...state.sessions.filter((s) => s.sessionId !== liveId)];
        }
        state.selectedId = liveId;
        state.selectedMeta = meta;
        els.chatTitle.textContent = meta.title || "Remote session";
        els.chatCwd.textContent = meta.cwd || cwd || "";
        setSessionMode("live-remote", { attachSwitched: true });
        syncFsForSession();
        if (data.message) {
          appendMessage({ role: "system", text: data.message });
        }
        // Pull live session history after switch so remote thread is visible
        try {
          const hres = await fetch(
            apiUrl(`/api/sessions/${encodeURIComponent(liveId)}/history`)
          );
          const hdata = await hres.json();
          if (Array.isArray(hdata.messages) && hdata.messages.length) {
            applyHistoryMessages(hdata.messages, { force: true, jump: true });
          }
        } catch (_) {
          /* keep view history */
        }
        renderSessions();
      } else {
        setSessionMode("live-remote", { attachSwitched: false });
      }
    } catch (err) {
      toast("Attach failed: " + err, "danger");
      sendWs({ type: "subscribe", sessionId: viewId });
      els.input.focus();
      return;
    }

    sendWs({ type: "subscribe", sessionId: liveId });
    if (switched && viewId !== liveId) {
      // Optional: keep view subscription briefly; prefer live only
      sendWs({ type: "unsubscribe", sessionId: viewId });
    }

    setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
    updateTurnStrip();
    els.input.focus();
  }

  function sendWs(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
    }
  }

  function connectWs() {
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
    state.wsState = state.reconnectAttempt ? "reconnecting" : "connecting";
    updateStatusPill();

    const ws = new WebSocket(wsUrl());
    state.ws = ws;

    ws.addEventListener("open", () => {
      state.wsState = "open";
      state.reconnectAttempt = 0;
      updateStatusPill();
      sendWs({ type: "hello" });
      if (state.selectedId) {
        sendWs({ type: "subscribe", sessionId: state.selectedId });
        refreshHistory(state.selectedId);
      }
      setComposerEnabled(true);
    });

    ws.addEventListener("message", (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      onHubMessage(msg);
    });

    ws.addEventListener("close", () => {
      state.wsState = "reconnecting";
      updateStatusPill();
      setComposerEnabled(false);
      scheduleReconnect();
    });

    ws.addEventListener("error", () => {
      try {
        ws.close();
      } catch (_) {}
    });
  }

  function scheduleReconnect() {
    state.reconnectAttempt += 1;
    const delay = Math.min(1000 * Math.pow(1.6, state.reconnectAttempt), 12000);
    state.reconnectTimer = setTimeout(connectWs, delay);
  }

  function onHubMessage(msg) {
    const type = msg.type;
    if (type === "status") {
      state.status = {
        agent: msg.agent || "down",
        bind: msg.bind || "local",
        tailscaleIp: msg.tailscaleIp || null,
        loadedSessionId: msg.loadedSessionId || null,
        turnRunning: !!msg.turnRunning,
        turnSessionId: msg.turnSessionId || null,
      };
      if (msg.hubVersion != null) state.hubVersion = msg.hubVersion;
      if (msg.cliVersion != null) state.cliVersion = msg.cliVersion;
      if (msg.compatOk != null) state.compatOk = !!msg.compatOk;
      if (Array.isArray(msg.compatIssues)) state.compatIssues = msg.compatIssues;
      if (Array.isArray(msg.hubSessionIds)) {
        state.hubSessionIds = msg.hubSessionIds;
        // Promote selected session to live-remote if server says hub-created
        if (
          state.selectedId &&
          state.sessionMode === "history" &&
          isHubCreatedSession(state.selectedId)
        ) {
          setSessionMode("live-remote");
        }
      }
      // Server is source of truth for turnRunning (prevents client/server desync).
      if (msg.turnRunning != null) {
        const running =
          !!msg.turnRunning &&
          (msg.turnSessionId === state.selectedId || !msg.turnSessionId);
        if (running && !state.turnRunning) {
          setTurnRunning(true);
          toast("Turn still running on server…", "");
        } else if (running !== state.turnRunning) {
          setTurnRunning(running);
        } else {
          state.turnRunning = running;
          updateTurnStrip();
        }
      }
      updateVersionBadge();
      updateStatusPill();
      renderSessions();
      setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
      return;
    }
    if (type === "sessions") {
      state.sessions = msg.items || [];
      renderSessions();
      return;
    }
    if (type === "history") {
      if (msg.sessionId === state.selectedId) {
        applyHistoryMessages(msg.messages || []);
      }
      return;
    }
    if (type === "acp") {
      handleAcpMessage(msg.sessionId, msg.message || {});
      return;
    }
    if (type === "system") {
      if (!msg.sessionId || msg.sessionId === state.selectedId) {
        appendMessage({ role: "system", text: msg.text || "" });
      }
      return;
    }
    if (type === "session_switch") {
      const fromId = msg.from || null;
      const toId = msg.to || null;
      // Only react if we were on the old session or already mid-prompt without selection match
      if (
        toId &&
        (!state.selectedId ||
          state.selectedId === fromId ||
          state.selectedId === toId ||
          state.turnRunning)
      ) {
        // Attach-on-open may already have selected live id
        if (state.selectedId === toId && !state.turnRunning) {
          if (state.hubSessionIds.indexOf(toId) < 0) {
            state.hubSessionIds = [toId, ...state.hubSessionIds].slice(0, 50);
          }
          const switched = !!(fromId && fromId !== toId);
          setSessionMode("live-remote", { attachSwitched: switched });
          return;
        }
        applySessionSwitch(fromId, toId, msg.reason || "", msg.message || "");
        // Keep turn running on the new session while prompt is in flight
        if (state.turnRunning || msg.reason === "cli_or_foreign_session") {
          setTurnRunning(true);
        }
      }
      return;
    }
    if (type === "commands") {
      if (msg.sessionId === state.selectedId) {
        state.commands = msg.commands || [];
        setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
      }
      return;
    }
    if (type === "turn") {
      if (msg.sessionId === state.selectedId) {
        setTurnRunning(msg.state === "running");
        if (msg.error && msg.state === "idle") {
          toast(msg.error, "danger");
        }
        setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
        updateStatusPill();
      }
      return;
    }
    if (type === "error") {
      setTurnRunning(false);
      setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
      updateStatusPill();
      const errText = msg.message || "Error";
      const busy = /busy|stuck/i.test(errText);
      toast(busy ? "Agent busy or stuck; try again" : errText, "danger");
      // Never leave Send disabled after error
      els.btnSend.disabled = !(
        state.wsState === "open" &&
        state.status.agent === "up" &&
        state.selectedId &&
        !state.turnRunning
      );
      return;
    }
  }

  function autoGrow() {
    const ta = els.input;
    if (!ta) return;
    const vvHeight =
      (window.visualViewport && window.visualViewport.height) || window.innerHeight || 600;
    const maxPx = Math.max(96, Math.floor(vvHeight * 0.35));
    const cs = getComputedStyle(ta);
    const lineH = parseFloat(cs.lineHeight);
    const linePx = Number.isFinite(lineH) && lineH > 0 ? lineH : 16 * 1.35;
    const padY =
      (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    const minPx = Math.ceil(linePx + padY);

    // Measure content height without leaving a tall empty box
    ta.style.height = "0px";
    ta.style.overflowY = "hidden";
    const scrollH = ta.scrollHeight;
    const h = Math.max(minPx, Math.min(scrollH || minPx, maxPx));
    ta.style.height = h + "px";
    ta.style.overflowY = scrollH > maxPx ? "auto" : "hidden";
    // Keep scroll at top so caret/text stay top-aligned when growing
    ta.scrollTop = 0;
  }

  function setRailTab(tab) {
    const next = tab === "files" ? "files" : "sessions";
    state.railTab = next;
    try {
      sessionStorage.setItem("grh.railTab", next);
    } catch (_) {}
    if (els.tabSessions) {
      els.tabSessions.setAttribute("aria-selected", next === "sessions" ? "true" : "false");
    }
    if (els.tabFiles) {
      els.tabFiles.setAttribute("aria-selected", next === "files" ? "true" : "false");
    }
    if (els.panelSessions) {
      els.panelSessions.classList.toggle("hidden", next !== "sessions");
    }
    if (els.panelFiles) {
      els.panelFiles.classList.toggle("hidden", next !== "files");
    }
    if (next === "files") {
      ensureFsLoaded();
    }
  }

  function isMarkdownPath(path) {
    if (!path) return false;
    const lower = String(path).toLowerCase();
    return lower.endsWith(".md") || lower.endsWith(".markdown");
  }

  function ensureMermaidReady() {
    if (state.mermaidReady) return true;
    if (typeof window.mermaid === "undefined") return false;
    try {
      window.mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "dark",
        darkMode: true,
        fontFamily: "IBM Plex Sans, system-ui, sans-serif",
      });
      state.mermaidReady = true;
      return true;
    } catch (err) {
      console.warn("mermaid.initialize failed", err);
      return false;
    }
  }

  async function renderMermaidBlocks(root) {
    if (!root || !ensureMermaidReady()) return;
    const blocks = Array.from(root.querySelectorAll("pre > code.language-mermaid, pre.language-mermaid > code, code.language-mermaid"));
    const seen = new Set();
    const targets = [];
    for (const code of blocks) {
      const pre = code.closest("pre") || code.parentElement;
      if (!pre || seen.has(pre)) continue;
      seen.add(pre);
      const src = code.textContent || "";
      const wrap = document.createElement("div");
      wrap.className = "mermaid-wrap";
      const diagram = document.createElement("div");
      diagram.className = "mermaid";
      diagram.textContent = src;
      wrap.appendChild(diagram);
      pre.replaceWith(wrap);
      targets.push({ wrap, diagram, src });
    }
    if (!targets.length) return;

    const nodes = targets.map((t) => t.diagram);
    try {
      await window.mermaid.run({ nodes });
    } catch (err) {
      // mermaid.run may reject on first failure; render remaining via per-node fallback
      console.warn("mermaid.run failed", err);
    }

    for (let i = 0; i < targets.length; i++) {
      const { wrap, diagram, src } = targets[i];
      if (wrap.querySelector("svg")) continue;
      try {
        const id = "mmd-" + Date.now() + "-" + i;
        const { svg } = await window.mermaid.render(id, src);
        wrap.innerHTML = "";
        const holder = document.createElement("div");
        holder.innerHTML = svg;
        while (holder.firstChild) wrap.appendChild(holder.firstChild);
      } catch (err) {
        wrap.innerHTML = "";
        const errEl = document.createElement("div");
        errEl.className = "mermaid-error";
        errEl.textContent = "Mermaid error: " + (err && err.message ? err.message : String(err));
        const pre = document.createElement("pre");
        pre.textContent = src;
        wrap.appendChild(errEl);
        wrap.appendChild(pre);
      }
    }
  }

  async function renderMarkdownPreview() {
    if (!els.filePreview) return;
    let source = els.fileEditor ? els.fileEditor.value : "";
    if (!source && state.fs.content) source = state.fs.content;
    if (typeof window.marked === "undefined" || typeof window.DOMPurify === "undefined") {
      els.filePreview.textContent =
        "Markdown library unavailable.\n\n" + (source || "");
      return;
    }
    try {
      if (window.marked && typeof window.marked.setOptions === "function") {
        window.marked.setOptions({ gfm: true, breaks: false });
      }
      const rawHtml =
        typeof window.marked.parse === "function"
          ? window.marked.parse(source || "")
          : window.marked(source || "");
      els.filePreview.innerHTML = window.DOMPurify.sanitize(rawHtml);
      await renderMermaidBlocks(els.filePreview);
    } catch (err) {
      els.filePreview.textContent = "Preview failed: " + err + "\n\n" + (source || "");
    }
  }

  function setFileViewMode(mode) {
    const next = mode === "preview" ? "preview" : "edit";
    state.fileViewMode = next;
    const showPreview = next === "preview";
    if (els.fileEditor) els.fileEditor.classList.toggle("hidden", showPreview);
    if (els.filePreview) els.filePreview.classList.toggle("hidden", !showPreview);
    if (els.btnFileEdit) els.btnFileEdit.setAttribute("aria-selected", showPreview ? "false" : "true");
    if (els.btnFilePreview) els.btnFilePreview.setAttribute("aria-selected", showPreview ? "true" : "false");
    if (showPreview) {
      void renderMarkdownPreview();
      if (els.filePreview) els.filePreview.focus();
    } else if (els.fileEditor && !els.fileEditor.disabled) {
      els.fileEditor.focus();
    }
  }

  function updateMdModeUi(path) {
    const isMd = isMarkdownPath(path);
    if (els.fileMdModes) els.fileMdModes.classList.toggle("hidden", !isMd);
    if (!isMd) {
      setFileViewMode("edit");
    }
  }

  function clearFilePreview() {
    if (els.filePreview) els.filePreview.innerHTML = "";
  }

  function resetFs(opts) {
    const forceClose = !opts || opts.closeFile !== false;
    state.fs.root = "";
    state.fs.filter = "";
    state.fs.expanded = new Set();
    state.fs.cache = new Map();
    state.fs.loading = false;
    state.fs.error = null;
    state.fs.saving = false;
    if (els.fileFilter) els.fileFilter.value = "";
    if (forceClose) {
      state.fs.openPath = null;
      state.fs.content = "";
      state.fs.baseline = "";
      state.fs.dirty = false;
      state.fileViewMode = "edit";
      if (els.fileEditor) {
        els.fileEditor.value = "";
        els.fileEditor.disabled = true;
        els.fileEditor.classList.remove("hidden");
      }
      clearFilePreview();
      if (els.filePreview) els.filePreview.classList.add("hidden");
      if (els.fileMdModes) els.fileMdModes.classList.add("hidden");
      updateFileDirtyUi();
      if (state.mainMode === "file") {
        setMainMode("chat");
      }
    }
    if (els.fileTree) els.fileTree.innerHTML = "";
  }

  function updateFileDirtyUi() {
    const dirty = !!state.fs.dirty;
    if (els.fileDirty) els.fileDirty.classList.toggle("hidden", !dirty);
    if (els.btnFileSave) {
      els.btnFileSave.disabled = !dirty || state.fs.saving || !state.fs.openPath;
    }
  }

  function setMainMode(mode) {
    const next = mode === "file" ? "file" : "chat";
    state.mainMode = next;
    if (els.chatPanel) els.chatPanel.classList.toggle("hidden", next !== "chat");
    if (els.filePanel) els.filePanel.classList.toggle("hidden", next !== "file");
  }

  function closeFileMode(opts) {
    const force = opts && opts.force;
    if (!force && state.fs.dirty) {
      if (!window.confirm("Discard unsaved changes?")) return false;
    }
    state.fs.openPath = null;
    state.fs.content = "";
    state.fs.baseline = "";
    state.fs.dirty = false;
    state.fs.error = null;
    state.fileViewMode = "edit";
    if (els.fileEditor) {
      els.fileEditor.value = "";
      els.fileEditor.disabled = true;
      els.fileEditor.classList.remove("hidden");
    }
    clearFilePreview();
    if (els.filePreview) els.filePreview.classList.add("hidden");
    if (els.fileMdModes) els.fileMdModes.classList.add("hidden");
    if (els.filePathLabel) els.filePathLabel.textContent = "";
    if (els.fileStatus) els.fileStatus.textContent = "";
    updateFileDirtyUi();
    setMainMode("chat");
    renderFileTree();
    return true;
  }

  function ensureFsLoaded() {
    const cwd = (state.selectedMeta && state.selectedMeta.cwd) || "";
    if (!cwd) {
      state.fs.root = "";
      if (els.fileTree) els.fileTree.innerHTML = "";
      if (els.fileEmpty) {
        els.fileEmpty.classList.remove("hidden");
        const p = els.fileEmpty.querySelector("p");
        if (p) p.textContent = "Open a session to browse its project.";
      }
      return;
    }
    if (cwd !== state.fs.root) {
      const hadOpen = !!state.fs.openPath;
      if (hadOpen && state.fs.dirty) {
        if (!window.confirm("Discard unsaved changes?")) {
          // keep previous root; user stayed dirty
          return;
        }
      }
      resetFs({ closeFile: true });
      state.fs.root = cwd;
    }
    if (els.fileEmpty) els.fileEmpty.classList.add("hidden");
    if (!state.fs.cache.has("")) {
      fetchFsList("");
    } else {
      renderFileTree();
    }
  }

  async function fetchFsList(rel) {
    const root = state.fs.root;
    if (!root) return;
    const path = rel || "";
    state.fs.loading = true;
    state.fs.error = null;
    try {
      const q =
        `/api/fs/list?root=${encodeURIComponent(root)}` +
        (path ? `&path=${encodeURIComponent(path)}` : "");
      const res = await fetch(apiUrl(q));
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        let errMsg =
          data.error || `Failed to list directory (HTTP ${res.status})`;
        if (res.status === 404) {
          errMsg =
            "File API missing (HTTP 404). Restart the hub to load new routes.";
        }
        state.fs.error = errMsg;
        if (path === "") {
          if (els.fileEmpty) {
            els.fileEmpty.classList.remove("hidden");
            const p = els.fileEmpty.querySelector("p");
            if (p) p.textContent = state.fs.error;
          }
        } else {
          toast(state.fs.error, "danger");
        }
        return;
      }
      const entries = Array.isArray(data.entries) ? data.entries : [];
      state.fs.cache.set(path, entries);
      if (els.fileEmpty) els.fileEmpty.classList.add("hidden");
      renderFileTree();
    } catch (err) {
      state.fs.error = String(err);
      toast("Failed to list files: " + err, "danger");
    } finally {
      state.fs.loading = false;
    }
  }

  function joinRel(parent, name) {
    const p = parent || "";
    if (!p) return name;
    return p.replace(/\\/g, "/") + "/" + name;
  }

  function formatFileSize(n) {
    if (n == null || n === "" || Number.isNaN(Number(n))) return "";
    const bytes = Number(n);
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function renderFileTree() {
    if (!els.fileTree) return;
    els.fileTree.innerHTML = "";
    const root = state.fs.root;
    if (!root) {
      if (els.fileEmpty) {
        els.fileEmpty.classList.remove("hidden");
        const p = els.fileEmpty.querySelector("p");
        if (p) p.textContent = "Open a session to browse its project.";
      }
      return;
    }
    const filter = (state.fs.filter || "").trim().toLowerCase();
    const entries = state.fs.cache.get("") || null;
    if (!entries) {
      if (els.fileEmpty) {
        els.fileEmpty.classList.remove("hidden");
        const p = els.fileEmpty.querySelector("p");
        if (p) p.textContent = state.fs.loading ? "Loading…" : "No files.";
      }
      return;
    }
    if (els.fileEmpty) els.fileEmpty.classList.add("hidden");

    function appendEntries(parentRel, depth) {
      const list = state.fs.cache.get(parentRel);
      if (!list) return;
      for (const entry of list) {
        const name = entry.name || "";
        const type = entry.type === "dir" ? "dir" : "file";
        const rel = joinRel(parentRel, name);
        const nameMatch = !filter || name.toLowerCase().includes(filter);
        const expanded = state.fs.expanded.has(rel);

        if (type === "file" && filter && !nameMatch) continue;

        // For dirs with filter: show if name matches or we will show any children
        if (type === "dir" && filter && !nameMatch) {
          // still show expanded dirs so nested matches stay reachable after expand
          if (!expanded) continue;
        }

        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "file-row" + (type === "dir" ? " dir" : " file");
        if (state.fs.openPath === rel) btn.classList.add("active");
        btn.style.setProperty("--depth", String(depth));
        btn.setAttribute("role", "treeitem");
        btn.dataset.path = rel;
        btn.dataset.type = type;

        if (type === "dir") {
          const chev = document.createElement("span");
          chev.className = "file-chevron";
          chev.setAttribute("aria-hidden", "true");
          chev.textContent = expanded ? "▾" : "▸";
          btn.appendChild(chev);
        } else {
          const spacer = document.createElement("span");
          spacer.className = "file-chevron";
          spacer.setAttribute("aria-hidden", "true");
          spacer.textContent = " ";
          btn.appendChild(spacer);
        }

        const label = document.createElement("span");
        label.className = "file-name";
        label.textContent = name;
        btn.appendChild(label);

        if (type === "file" && entry.size != null) {
          const meta = document.createElement("span");
          meta.className = "file-meta";
          meta.textContent = formatFileSize(entry.size);
          btn.appendChild(meta);
        }

        if (type === "dir") {
          btn.addEventListener("click", () => toggleDir(rel));
        } else {
          btn.addEventListener("click", () => openFile(rel));
        }
        els.fileTree.appendChild(btn);

        if (type === "dir" && expanded) {
          if (state.fs.cache.has(rel)) {
            appendEntries(rel, depth + 1);
          }
        }
      }
    }

    appendEntries("", 0);
  }

  async function toggleDir(rel) {
    if (state.fs.expanded.has(rel)) {
      state.fs.expanded.delete(rel);
      renderFileTree();
      return;
    }
    state.fs.expanded.add(rel);
    if (!state.fs.cache.has(rel)) {
      renderFileTree();
      await fetchFsList(rel);
    } else {
      renderFileTree();
    }
  }

  async function openFile(rel) {
    if (state.fs.dirty && state.fs.openPath && state.fs.openPath !== rel) {
      if (!window.confirm("Discard unsaved changes?")) return;
    }
    const root = state.fs.root || (state.selectedMeta && state.selectedMeta.cwd) || "";
    if (!root) {
      toast("No project root for this session", "danger");
      return;
    }
    state.fs.loading = true;
    if (els.fileStatus) els.fileStatus.textContent = "Loading…";
    try {
      const q =
        `/api/fs/read?root=${encodeURIComponent(root)}` +
        `&path=${encodeURIComponent(rel)}`;
      const res = await fetch(apiUrl(q));
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        let err = data.error || `Failed to read file (HTTP ${res.status})`;
        if (res.status === 404) {
          err =
            "File API missing (HTTP 404). Restart the hub to load new routes.";
        }
        toast(err, "danger");
        if (els.fileStatus) els.fileStatus.textContent = err;
        return;
      }
      const content = data.content != null ? String(data.content) : "";
      state.fs.root = root;
      state.fs.openPath = rel;
      state.fs.content = content;
      state.fs.baseline = content;
      state.fs.dirty = false;
      state.fs.error = null;
      if (els.fileEditor) {
        els.fileEditor.disabled = false;
        els.fileEditor.value = content;
      }
      if (els.filePathLabel) els.filePathLabel.textContent = rel;
      if (els.fileStatus) {
        const size = data.size != null ? formatFileSize(data.size) : "";
        els.fileStatus.textContent = size ? `Loaded · ${size}` : "Loaded";
      }
      updateFileDirtyUi();
      updateMdModeUi(rel);
      if (isMarkdownPath(rel)) {
        setFileViewMode("preview");
      } else {
        setFileViewMode("edit");
      }
      setMainMode("file");
      renderFileTree();
      closeRail();
      if (state.fileViewMode === "edit" && els.fileEditor) {
        els.fileEditor.focus();
      }
    } catch (err) {
      toast("Failed to read file: " + err, "danger");
      if (els.fileStatus) els.fileStatus.textContent = String(err);
    } finally {
      state.fs.loading = false;
    }
  }

  async function saveOpenFile() {
    if (!state.fs.openPath || !state.fs.root || !state.fs.dirty || state.fs.saving) return;
    const content = els.fileEditor ? els.fileEditor.value : state.fs.content;
    state.fs.saving = true;
    updateFileDirtyUi();
    if (els.fileStatus) els.fileStatus.textContent = "Saving…";
    try {
      const res = await fetch(apiUrl("/api/fs/write"), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root: state.fs.root,
          path: state.fs.openPath,
          content,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        let err = data.error || `Save failed (HTTP ${res.status})`;
        if (res.status === 404) {
          err =
            "File API missing (HTTP 404). Restart the hub to load new routes.";
        }
        toast(err, "danger");
        if (els.fileStatus) els.fileStatus.textContent = err;
        return;
      }
      state.fs.content = content;
      state.fs.baseline = content;
      state.fs.dirty = false;
      const size = data.size != null ? formatFileSize(data.size) : formatFileSize(content.length);
      if (els.fileStatus) els.fileStatus.textContent = size ? `Saved · ${size}` : "Saved";
      updateFileDirtyUi();
      toast("Saved " + state.fs.openPath);
    } catch (err) {
      toast("Save failed: " + err, "danger");
      if (els.fileStatus) els.fileStatus.textContent = String(err);
    } finally {
      state.fs.saving = false;
      updateFileDirtyUi();
    }
  }

  function insertOpenPath() {
    const rel = state.fs.openPath;
    if (!rel) return;
    if (state.fs.dirty) {
      if (!window.confirm("Discard unsaved changes?")) return;
    }
    const path = rel.replace(/\\/g, "/");
    const cur = els.input.value || "";
    const needsSpace = cur.length > 0 && !/\s$/.test(cur);
    els.input.value = cur + (needsSpace ? " " : "") + path;
    closeFileMode({ force: true });
    els.input.focus();
    autoGrow();
    toast("Inserted path");
  }

  function onFileEditorInput() {
    if (!els.fileEditor || !state.fs.openPath) return;
    state.fs.content = els.fileEditor.value;
    state.fs.dirty = state.fs.content !== state.fs.baseline;
    updateFileDirtyUi();
  }

  function syncFsForSession() {
    const cwd = (state.selectedMeta && state.selectedMeta.cwd) || "";
    if (!cwd) {
      if (state.fs.dirty && state.mainMode === "file") {
        // force close without prompt when session cleared
        state.fs.dirty = false;
      }
      resetFs({ closeFile: true });
      if (state.railTab === "files") ensureFsLoaded();
      return;
    }
    if (cwd !== state.fs.root) {
      if (state.fs.dirty && state.mainMode === "file") {
        // session switched under us; confirm was handled in openSession when possible
        state.fs.dirty = false;
      }
      resetFs({ closeFile: true });
      state.fs.root = cwd;
      if (state.railTab === "files") {
        ensureFsLoaded();
      }
    } else if (state.railTab === "files") {
      ensureFsLoaded();
    }
  }

  function isMobile() {
    return window.matchMedia("(max-width: 899px)").matches;
  }

  function submitPrompt() {
    const text = els.input.value.trim();
    if (!text || !state.selectedId || state.turnRunning) return;
    if (state.wsState !== "open" || state.status.agent !== "up") {
      toast("Not connected to agent", "danger");
      return;
    }
    // Successful hub prompt path: if already hub-created, mark live immediately
    if (isHubCreatedSession(state.selectedId)) {
      setSessionMode("live-remote");
    }
    beginNewUserTurn();
    sendWs({
      type: "prompt",
      sessionId: state.selectedId,
      text,
      cwd: (state.selectedMeta && state.selectedMeta.cwd) || "",
    });
    els.input.value = "";
    autoGrow();
    closeSlash();
    setTurnRunning(true);
    setComposerEnabled(true);
    updateStatusPill();
  }

  // Slash palette
  function openSlash(filter) {
    const q = (filter || "").toLowerCase();
    const items = (state.commands || []).filter((c) => {
      const name = (c.name || "").toLowerCase();
      const desc = (c.description || "").toLowerCase();
      return !q || name.includes(q) || desc.includes(q);
    });
    state.slashItems = items.slice(0, 40);
    state.slashIndex = 0;
    if (!state.slashItems.length) {
      if (!state.commands.length) {
        els.slash.innerHTML = `<div class="slash-item"><span class="desc">Load a session for / commands</span></div>`;
        els.slash.classList.remove("hidden");
        state.slashOpen = true;
        return;
      }
      closeSlash();
      return;
    }
    renderSlash();
    els.slash.classList.remove("hidden");
    state.slashOpen = true;
  }

  function renderSlash() {
    els.slash.innerHTML = "";
    state.slashItems.forEach((c, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "slash-item" + (i === state.slashIndex ? " active" : "");
      btn.setAttribute("role", "option");
      if (i === state.slashIndex) btn.setAttribute("aria-selected", "true");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = "/" + (c.name || "");
      const desc = document.createElement("span");
      desc.className = "desc";
      desc.textContent = c.description || "";
      btn.append(name, desc);
      btn.addEventListener("click", () => selectSlash(c));
      els.slash.appendChild(btn);
    });
    const active = els.slash.querySelector(".slash-item.active");
    if (active && typeof active.scrollIntoView === "function") {
      active.scrollIntoView({ block: "nearest" });
    }
  }

  function closeSlash() {
    state.slashOpen = false;
    els.slash.classList.add("hidden");
    els.slash.innerHTML = "";
  }

  function selectSlash(cmd) {
    const name = cmd.name || "";
    const hint = (cmd.input && cmd.input.hint) || "";
    els.input.value = hint ? `/${name} ` : `/${name}`;
    closeSlash();
    els.input.focus();
    autoGrow();
  }

  function onComposerInput() {
    autoGrow();
    const val = els.input.value;
    if (val.startsWith("/")) {
      const firstLine = val.split("\n")[0];
      if (!firstLine.includes(" ") || firstLine === "/") {
        const filter = firstLine.slice(1);
        openSlash(filter);
        return;
      }
    }
    closeSlash();
  }

  async function openNewModal() {
    els.modalNew.classList.remove("hidden");
    els.projectSearch.value = "";
    if (els.projectNewName) els.projectNewName.value = "";
    await refreshProjects();
    if (els.projectNewName) els.projectNewName.focus();
    else els.projectSearch.focus();
  }

  function closeNewModal() {
    els.modalNew.classList.add("hidden");
  }

  async function refreshProjects() {
    try {
      const res = await fetch(apiUrl("/api/projects"));
      const data = await res.json();
      state.projects = data.items || [];
    } catch {
      state.projects = [];
    }
    renderProjects();
  }

  function renderProjects() {
    const q = els.projectSearch.value.trim().toLowerCase();
    const items = state.projects.filter(
      (p) => !q || (p.name || "").toLowerCase().includes(q) || (p.path || "").toLowerCase().includes(q)
    );
    els.projectList.innerHTML = "";
    els.projectEmpty.classList.toggle("hidden", items.length > 0);
    for (const p of items) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "project-row";
      btn.setAttribute("role", "listitem");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = p.name;
      const path = document.createElement("span");
      path.className = "path";
      path.textContent = p.path;
      btn.append(name, path);
      btn.addEventListener("click", () => createSession(p.path));
      els.projectList.appendChild(btn);
    }
  }

  async function createProjectFolder() {
    if (!els.projectNewName || !els.btnCreateProject) return;
    const name = els.projectNewName.value.trim();
    if (!name) {
      toast("Enter a folder name", "danger");
      els.projectNewName.focus();
      return;
    }
    els.btnCreateProject.disabled = true;
    try {
      const res = await fetch(apiUrl("/api/projects"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Failed to create folder", "danger");
        return;
      }
      const label = data.name || name;
      toast(data.created ? `Created ${label}` : `Using existing ${label}`);
      els.projectNewName.value = "";
      await refreshProjects();
      if (data.path) {
        await createSession(data.path);
      }
    } catch (err) {
      toast("Failed to create folder: " + err, "danger");
    } finally {
      els.btnCreateProject.disabled = false;
    }
  }

  async function createSession(cwd) {
    closeNewModal();
    try {
      const res = await fetch(apiUrl("/api/sessions"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cwd }),
      });
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "Failed to create session", "danger");
        return;
      }
      const session = {
        sessionId: data.sessionId,
        title: "Untitled session",
        cwd: data.cwd || cwd,
        updatedAt: new Date().toISOString(),
        modelId: "",
        path: "",
      };
      // Hub-created via POST /api/sessions: live remote immediately
      if (data.sessionId && state.hubSessionIds.indexOf(data.sessionId) < 0) {
        state.hubSessionIds = [data.sessionId, ...state.hubSessionIds].slice(0, 50);
      }
      try {
        const r2 = await fetch(apiUrl("/api/sessions"));
        const d2 = await r2.json();
        state.sessions = d2.items || [];
      } catch (_) {
        state.sessions = [session, ...state.sessions];
      }
      renderSessions();
      const found = state.sessions.find((s) => s.sessionId === data.sessionId) || session;
      await openSession(found);
    } catch (err) {
      toast("Failed to create session: " + err, "danger");
    }
  }

  function setupViewport() {
    const apply = () => {
      const vv = window.visualViewport;
      if (vv) {
        const offset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
        document.documentElement.style.setProperty("--vv-offset", offset + "px");
      }
      autoGrow();
      if (state.stickToBottom) scrollIfSticky();
    };
    const vv = window.visualViewport;
    if (vv) {
      vv.addEventListener("resize", apply);
      vv.addEventListener("scroll", apply);
    }
    window.addEventListener("resize", apply);
    apply();
  }

  function bindEvents() {
    els.sessionSearch.addEventListener("input", () => {
      state.filter = els.sessionSearch.value;
      renderSessions();
    });

    if (els.tabSessions) {
      els.tabSessions.addEventListener("click", () => setRailTab("sessions"));
    }
    if (els.tabFiles) {
      els.tabFiles.addEventListener("click", () => setRailTab("files"));
    }
    if (els.fileFilter) {
      els.fileFilter.addEventListener("input", () => {
        state.fs.filter = els.fileFilter.value;
        renderFileTree();
      });
    }
    if (els.btnFileBack) {
      els.btnFileBack.addEventListener("click", () => {
        closeFileMode({ force: false });
        if (state.mainMode === "chat" && els.input && !els.input.disabled) {
          els.input.focus();
        }
      });
    }
    if (els.btnFileSave) {
      els.btnFileSave.addEventListener("click", () => {
        saveOpenFile();
      });
    }
    if (els.btnFileInsert) {
      els.btnFileInsert.addEventListener("click", () => {
        insertOpenPath();
      });
    }
    if (els.btnFileEdit) {
      els.btnFileEdit.addEventListener("click", () => {
        setFileViewMode("edit");
      });
    }
    if (els.btnFilePreview) {
      els.btnFilePreview.addEventListener("click", () => {
        setFileViewMode("preview");
      });
    }
    if (els.fileEditor) {
      els.fileEditor.addEventListener("input", onFileEditorInput);
    }

    els.btnMenu.addEventListener("click", openRail);
    els.backdrop.addEventListener("click", closeRail);
    els.btnNew.addEventListener("click", openNewModal);
    if (els.btnEmptyNew) els.btnEmptyNew.addEventListener("click", openNewModal);
    if (els.btnEmptySessions) els.btnEmptySessions.addEventListener("click", openRail);

    $$("[data-close]").forEach((el) => {
      el.addEventListener("click", () => {
        const id = el.getAttribute("data-close");
        if (id === "modal-new") closeNewModal();
      });
    });

    els.projectSearch.addEventListener("input", renderProjects);
    if (els.btnCreateProject) {
      els.btnCreateProject.addEventListener("click", () => {
        createProjectFolder();
      });
    }
    if (els.projectNewName) {
      els.projectNewName.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          createProjectFolder();
        }
      });
    }

    els.form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (state.slashOpen && state.slashItems[state.slashIndex]) {
        selectSlash(state.slashItems[state.slashIndex]);
        return;
      }
      submitPrompt();
    });

    els.input.addEventListener("input", onComposerInput);
    els.input.addEventListener("focus", () => {
      autoGrow();
      // prevent iOS scroll-jump centering the field mid-screen too aggressively
      setTimeout(() => {
        if (state.stickToBottom) scrollIfSticky();
      }, 50);
    });
    els.input.addEventListener("keydown", (e) => {
      if (state.slashOpen) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          state.slashIndex = Math.min(state.slashIndex + 1, state.slashItems.length - 1);
          renderSlash();
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          state.slashIndex = Math.max(state.slashIndex - 1, 0);
          renderSlash();
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          closeSlash();
          return;
        }
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          if (state.slashItems[state.slashIndex]) selectSlash(state.slashItems[state.slashIndex]);
          return;
        }
      }
      if (e.key === "Enter" && !e.shiftKey && !isMobile()) {
        e.preventDefault();
        submitPrompt();
      }
    });

    els.btnStop.addEventListener("click", () => {
      if (!state.selectedId) return;
      sendWs({ type: "cancel", sessionId: state.selectedId });
    });

    els.transcript.addEventListener("scroll", () => {
      updateJumpLatest();
    });

    if (els.btnJumpLatest) {
      els.btnJumpLatest.addEventListener("click", jumpToLatest);
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
        connectWs();
      } else if (state.selectedId) {
        refreshHistory(state.selectedId);
      }
    });
  }

  async function refreshHistory(sessionId) {
    if (!sessionId || sessionId !== state.selectedId) return;
    // Live stream owns the transcript while a turn is running
    if (state.turnRunning) return;
    try {
      const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/history`));
      if (!res.ok) return;
      const data = await res.json();
      if (sessionId !== state.selectedId) return;
      if (state.turnRunning) return;
      applyHistoryMessages(data.messages || []);
    } catch (_) {
      // keep current transcript on transient failures
    }
  }

  function startHistoryPoll() {
    if (state.historyPollTimer) return;
    state.historyPollTimer = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (!state.selectedId) return;
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      refreshHistory(state.selectedId);
    }, 4000);
  }

  async function bootstrap() {
    bindEvents();
    setupViewport();
    updateStatusPill();
    updateVersionBadge();
    updateSessionBanner();
    setComposerEnabled(false);
    updateTurnStrip();
    startHistoryPoll();

    let savedTab = "sessions";
    try {
      const t = sessionStorage.getItem("grh.railTab");
      if (t === "files" || t === "sessions") savedTab = t;
    } catch (_) {}
    setRailTab(savedTab);

    try {
      const res = await fetch(apiUrl("/api/sessions"));
      if (res.ok) {
        const data = await res.json();
        state.sessions = data.items || [];
        renderSessions();
      }
    } catch (_) {}

    connectWs();
  }

  bootstrap();
})();
