(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* Terminal format helpers (mirror hub/ui_format.py) */
  function formatTermPrefix(role) {
    const r = String(role || "").trim().toLowerCase();
    if (r === "user") return "You:";
    if (r === "assistant") return "Grok:";
    // thought / tool / plan / system: status lives in thought-summary-label (or body)
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

  const PLACEHOLDER_SHORT = "Message…";
  const PLACEHOLDER_FULL = "Message… · / for commands";

  function shouldShowToolLine() {
    return true;
  }

  /* UX helpers (mirror hub/ui_ux.py) */
  function topbarBubbleLines(project, model, path) {
    const p = String(project || "").trim() || "—";
    const m = String(model || "").trim() || "—";
    const pathS = String(path || "").trim() || "—";
    return [`Project: ${p}`, `Model: ${m}`, `Path: ${pathS}`];
  }

  function topbarBubbleText(project, model, path) {
    return topbarBubbleLines(project, model, path).join("\n");
  }

  function shouldScrollToBottom(stickToBottom, force) {
    return !!(force || stickToBottom);
  }

  function residualStatusParts(opts) {
    const o = opts || {};
    const pp = Math.max(0, Number(o.plan_pending) || 0);
    const pr = Math.max(0, Number(o.plan_running) || 0);
    const pf = Math.max(0, Number(o.plan_failed) || 0);
    const tp = Math.max(0, Number(o.tool_pending) || 0);
    const tr = Math.max(0, Number(o.tool_running) || 0);
    const tf = Math.max(0, Number(o.tool_failed) || 0);
    const parts = [];
    const planOpen = pp + pr;
    const toolOpen = tp + tr;
    if (planOpen) parts.push("plan " + planOpen + " open");
    if (pf) parts.push("plan " + pf + " failed");
    if (toolOpen) parts.push("tool " + toolOpen + " open");
    if (tf) parts.push("tool " + tf + " failed");
    return parts;
  }

  function idleTurnLabel(opts) {
    const o = opts || {};
    const parts = ["idle"];
    const residual = residualStatusParts(o);
    for (const r of residual) parts.push(r);
    const model = String(o.model || "").trim();
    if (model && !residual.length) parts.push(model);
    return parts.join(" · ");
  }

  function turnProgressLabel(opts) {
    const o = opts || {};
    const running = !!o.running;
    const model = String(o.model || "").trim();
    if (!running) {
      return idleTurnLabel(o);
    }
    // Open tools/plan: prefer "running" over bare "quiet" (mid-tool wait).
    const parts = o.quiet && !o.tool_open ? ["quiet"] : ["running"];
    if (o.elapsed_s != null && o.elapsed_s >= 0) {
      parts.push(`${Math.floor(o.elapsed_s)}s`);
    }
    const q = Number(o.queue) || 0;
    if (q > 0) parts.push("queue " + q);
    const tool = String(o.tool || "").trim();
    if (tool) parts.push(tool);
    if (model) parts.push(model);
    return parts.join(" · ");
  }

  function sessionListProgressHint(opts) {
    const o = opts || {};
    if (!o.is_live_turn) return "";
    const t = String(o.tool || "").trim();
    return t || "running";
  }

  function shouldMarkPlanStale(opts) {
    const o = opts || {};
    return !o.turn_running && !!o.has_open_or_failed;
  }

  const BUILTIN_SLASH = [
    { name: "new", description: "Start a new session" },
    { name: "compact", description: "Compact conversation history" },
    { name: "skills", description: "List or inject a skill" },
    { name: "help", description: "Show help / available commands" },
    { name: "clear", description: "Clear context / start fresh if supported" },
    { name: "model", description: "Show or change model if supported" },
  ];

  function applyCommands(cmds) {
    if (!Array.isArray(cmds)) return;
    // Keep non-empty agent lists; empty array would wipe a good cache.
    if (cmds.length === 0) return;
    state.commands = cmds;
    setComposerEnabled(composerConnected());
  }

  function slashCommandSource() {
    // Merge priority: agent > skill > builtin (first name wins).
    const names = new Set();
    const merged = [];
    const push = (c) => {
      const n = (c.name || "").toLowerCase();
      if (!n || names.has(n)) return;
      names.add(n);
      merged.push(c);
    };
    const agent = Array.isArray(state.commands) ? state.commands : [];
    for (const c of agent) push(c);
    for (const s of state.skills || []) {
      push({
        name: s.name,
        description: s.description ? `Skill: ${s.description}` : "Skill",
        _skill: true,
      });
    }
    for (const b of BUILTIN_SLASH) push(b);
    return merged;
  }

  /** Name-first rank for slash filter. Desc-only is weak (never auto-pick over typed name). */
  function rankSlashMatch(c, q) {
    if (!q) return 1;
    const name = (c.name || "").toLowerCase();
    const desc = (c.description || "").toLowerCase();
    if (name === q) return 100;
    if (name.startsWith(q)) return 80;
    if (name.includes(q)) return 60;
    if (desc.includes(q)) return 10;
    return 0;
  }

  /**
   * On Enter/Submit with palette open:
   * - exact typed name → send prompt as-is (never rewrite /handoff → /doc-sync)
   * - strong prefix completion only → selectSlash
   * - desc-only / no strong match → send typed text as-is
   */
  function resolveSlashOnSubmit() {
    if (!state.slashOpen) return "prompt";
    const raw = (els.input.value || "").trim();
    const m = raw.match(/^\/([^\s/]+)/);
    const typed = m ? m[1].toLowerCase() : "";
    if (!typed) return "prompt";

    const source = slashCommandSource();
    const exact = source.find((c) => (c.name || "").toLowerCase() === typed);
    if (exact) return "prompt";

    const active = state.slashItems[state.slashIndex];
    const activeName = active ? (active.name || "").toLowerCase() : "";
    if (
      active &&
      state.slashStrongMatch &&
      activeName.startsWith(typed) &&
      activeName !== typed
    ) {
      return "select";
    }
    return "prompt";
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
    healthProbeTimer: null,
    hubReachable: null, // null | true | false (from /health while reconnecting)
    bootId: null,
    startedAt: null,
    /** Set when noteBootId sees a new process bootId (hub process restarted). */
    _hubProcessRestarted: false,
    /** True if client had live work when bootId changed (before clear). */
    _pendingRestartInterrupt: false,
    /** Freeze sticky scrolls during reconnect resume (one jump at end). */
    _reconnectScrollFreeze: false,
    _resumeAfterReconnect: false,
    /** Durable client error log (toasts still auto-dismiss; strip does not). */
    /** @type {{at: string, message: string, sessionId: string|null, source: string}[]} */
    errorLog: [],
    _lastStripError: null,

    status: {
      agent: "down",
      agentProcess: null,
      agentDetail: null,
      acpQuality: null,
      acpConnected: false,
      acpHealAttempts: 0,
      acpHealError: null,
      bind: "local",
      tailscaleIp: null,
      loadedSessionId: null,
      turnRunning: false,
      turnSessionId: null,
    },
    /** True while POST /api/admin/restart-agent is in flight. */
    restartingAgent: false,
    hubVersion: null,
    cliVersion: null,
    compatOk: null,
    compatIssues: [],
    hubSessionIds: [],
    /** @type {Record<string, "working"|"question"|"idle">} */
    sessionFlags: {},
    /** @type {{sessionId: string, state: string, ageSeconds?: number|null, silenceSeconds?: number|null, sawUpdate?: boolean, ttfbSeconds?: number|null}[]} */
    liveTurns: [],
    /** @type {string[]} */
    pendingQuestionSessions: [],
    maxConcurrentTurns: 3,
    /** @type {{activeTurnCount: number, maxConcurrentTurns: number, busySessionIds: string[]}|null} */
    capacity: null,
    /** @type {{level: string, updatesBytes: number|null, message: string}|null} */
    contextBudget: null,
    turnAgeSeconds: null,
    turnSilenceSeconds: null,
    /** Wall clock when last status/health capacity fields arrived (for live silence drift). */
    _capacityStatusAt: null,
    sessionMode: "none", // none | history | live-remote
    attachSwitched: false, // true when live id != viewed foreign history id
    /** Hub-owned session id for prompts when viewing a different history id */
    livePromptSessionId: null,
    sessions: [],
    filter: "",
    sessionKindFilter: "working", // working | subagent | all
    pinnedSessions: [],
    selectedId: null,
    selectedMeta: null,
    /** Last plan API payload for selected session (or null). */
    sessionPlan: null,
    /** Session ids for which plan-await modal was auto-opened once. */
    _planAwaitOpened: new Set(),
    commands: [],
    turnRunning: false,
    promptQueueLength: 0,
    stickToBottom: true,
    _ignoreScroll: false,
    /** Nested history rebuild depth; >0 suppresses per-line scroll/turn-strip thrash. */
    _historyBatchDepth: 0,
    _suppressStickyScroll: false,
    /** Session ids already WS-subscribed this connection (skip redundant history dumps). */
    subscribedSessions: new Set(),
    /** @type {Map<string, string>} sessionId -> composer draft text */
    composerDrafts: new Map(),
    historyLoadedFor: null,
    historyFingerprint: null,
    historyPollTimer: null,
    usagePollTimer: null,
    usage: null,
    usageTitles: { context: "", plan: "" },
    usagePopoverSeg: null,
    usagePopoverPinned: false,
    usageHideTimer: null,
    /** @type {Map<string, {pane: HTMLElement, stickToBottom: boolean, historyFingerprint: string|null, historyLoaded: boolean, streamBuffers: object, lastToolTitle: string}>} */
    sessionViews: new Map(),
    liveTurnSessionId: null,
    activePane: null,
    metaPopoverPinned: false,
    metaHideTimer: null,
    streamBuffers: {
      assistantEl: null,
      thoughtEl: null,
      thoughtOpen: false,
      planEl: null,
      activityEl: null,
      tools: new Map(),
      terminals: new Map(),
      lastToolTitle: "",
      activeUserEl: null,
    },
    slashOpen: false,
    slashIndex: 0,
    slashItems: [],
    slashStrongMatch: false,
    _slashListSig: null,
    _slashSig: null,
    _slashTouching: false,
    skills: [],
    _skillsLoaded: false,
    _skillsFetching: false,
    projects: [],
    /** New-session modal: "list" | "browse" | "entry" */
    projectModalMode: "list",
    entryChoiceCwd: null,
    reloadingSession: false,
    /** Current folder browser payload from /api/projects/browse */
    projectBrowse: null,
    pendingUserQuestion: null, // { requestId, sessionId, questions, toolCallId }
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
    fileImageWrap: $("#file-image-wrap"),
    fileImage: $("#file-image"),
    fileVideoWrap: $("#file-video-wrap"),
    fileVideo: $("#file-video"),
    fileMdModes: $("#file-md-modes"),
    fileStatus: $("#file-status"),
    btnFileBack: $("#btn-file-back"),
    btnFileEdit: $("#btn-file-edit"),
    btnFilePreview: $("#btn-file-preview"),
    btnFileInsert: $("#btn-file-insert"),
    btnFileShare: $("#btn-file-share"),
    btnFileSave: $("#btn-file-save"),
    imageLightbox: $("#image-lightbox"),
    lightboxImg: $("#lightbox-img"),
    btnLightboxClose: $("#btn-lightbox-close"),
    transcript: $("#transcript"),
    btnJumpLatest: $("#btn-jump-latest"),
    emptyMain: $("#empty-main"),
    chatTitle: $("#chat-title"),
    btnRenameSession: $("#btn-rename-session"),
    btnViewPlan: $("#btn-view-plan"),
    modalPlan: $("#modal-plan"),
    planBody: $("#plan-body"),
    planStatusChip: $("#plan-status-chip"),
    btnPlanApprove: $("#btn-plan-approve"),
    btnPlanRequestChanges: $("#btn-plan-request-changes"),
    btnPlanQuit: $("#btn-plan-quit"),
    btnPlanClose: $("#btn-plan-close"),
    planAwaitBanner: $("#plan-await-banner"),
    btnPlanAwaitOpen: $("#btn-plan-await-open"),
    chatProject: $("#chat-project"),
    chatModel: $("#chat-model"),
    chatCwd: $("#chat-cwd"),
    chatSessionId: $("#chat-session-id"),
    statusPill: $("#status-pill"),
    statusLabel: $("#status-label"),
    turnStrip: $("#turn-strip"),
    turnStripText: $("#turn-strip-text"),
    turnStripCursor: $("#turn-strip-cursor"),
    capacityBanner: $("#capacity-banner"),
    capacityBannerText: $("#capacity-banner-text"),
    contextBudgetBanner: $("#context-budget-banner"),
    contextBudgetBannerText: $("#context-budget-banner-text"),
    form: $("#composer-form"),
    input: $("#composer-input"),
    composerFileInput: $("#composer-file-input"),
    btnAttach: $("#btn-attach"),
    btnFsUpload: $("#btn-fs-upload"),
    btnSend: $("#btn-send"),
    btnStop: $("#btn-stop"),
    btnReload: $("#btn-reload"),
    composerHint: $("#composer-hint"),
    slash: $("#slash-palette"),
    btnMenu: $("#btn-menu"),
    btnRailCollapse: $("#btn-rail-collapse"),
    btnNew: $("#btn-new"),
    btnEmptySessions: $("#btn-empty-sessions"),
    btnEmptyNew: $("#btn-empty-new"),
    app: $("#app"),
    modalNew: $("#modal-new"),
    modalAskUser: $("#modal-ask-user"),
    askUserBody: $("#ask-user-body"),
    btnAskUserSubmit: $("#btn-ask-user-submit"),
    btnAskUserCancel: $("#btn-ask-user-cancel"),
    modalSitePreview: $("#modal-site-preview"),
    sitePreviewPath: $("#site-preview-path"),
    sitePreviewFrame: $("#site-preview-frame"),
    sitePreviewFrameWrap: $("#site-preview-frame-wrap"),
    sitePreviewScaleShell: $("#site-preview-scale-shell"),
    sitePreviewScale: $("#site-preview-scale"),
    btnSitePreviewOpen: $("#btn-site-preview-open"),
    projectListView: $("#project-list-view"),
    projectList: $("#project-list"),
    projectSearch: $("#project-search"),
    projectEmpty: $("#project-empty"),
    projectNewName: $("#project-new-name"),
    btnCreateProject: $("#btn-create-project"),
    btnBrowseFolders: $("#btn-browse-folders"),
    projectBrowser: $("#project-browser"),
    projectBrowserPath: $("#project-browser-path"),
    projectBrowserList: $("#project-browser-list"),
    btnBrowseUp: $("#btn-browse-up"),
    btnBrowseStart: $("#btn-browse-start"),
    btnBrowseBack: $("#btn-browse-back"),
    projectEntryChoice: $("#project-entry-choice"),
    projectEntryPriors: $("#project-entry-priors"),
    projectEntryCwd: $("#project-entry-cwd"),
    projectEntryChoiceHelp: $("#project-entry-choice-help"),
    btnEntryStartNew: $("#btn-entry-start-new"),
    btnEntryBack: $("#btn-entry-back"),
    modalNewTitle: $("#modal-new-title"),
    modalNewHelp: $("#modal-new-help"),
    toastHost: $("#toast-host"),
    errorStrip: $("#error-strip"),
    errorStripTime: $("#error-strip-time"),
    errorStripMsg: $("#error-strip-msg"),
    btnErrorCopy: $("#btn-error-copy"),
    btnErrorDismiss: $("#btn-error-dismiss"),
    btnErrorResend: $("#btn-error-resend"),
    versionBadge: $("#version-badge"),
    versionLabel: $("#version-label"),
    compatDot: $("#compat-dot"),
    sessionBanner: $("#session-banner"),
    sessionBannerText: $("#session-banner-text"),
    usageBar: $("#usage-bar"),
    usageBarFill: $("#usage-bar-fill"),
    usageBarFillPlan: $("#usage-bar-fill-plan"),
    usageBarLabel: $("#usage-bar-label"),
    usageBarTokens: $("#usage-bar-tokens"),
    usageBarPlan: $("#usage-bar-plan"),
    usageBarReset: $("#usage-bar-reset"),
    usagePopover: $("#usage-popover"),
    usageSegContext: document.querySelector('[data-usage-seg="context"]'),
    usageSegPlan: document.querySelector('[data-usage-seg="plan"]'),
    metaPopover: $("#meta-popover"),
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

  function toast(message, kind = "", durationMs, opts) {
    if (durationMs && typeof durationMs === "object") {
      opts = durationMs;
      durationMs = opts.durationMs;
    }
    opts = opts || {};
    const el = document.createElement("div");
    el.className = `toast${kind ? " " + kind : ""}`;
    if (opts.actionLabel && typeof opts.onAction === "function") {
      el.classList.add("toast-with-action");
      const msgSpan = document.createElement("span");
      msgSpan.className = "toast-msg";
      msgSpan.textContent = message;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-ghost btn-sm toast-action";
      btn.textContent = opts.actionLabel;
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        try {
          opts.onAction();
        } catch (_) {}
        el.remove();
      });
      el.append(msgSpan, btn);
    } else {
      el.textContent = message;
    }
    els.toastHost.appendChild(el);
    const ms =
      typeof durationMs === "number"
        ? durationMs
        : kind === "danger"
          ? 8000
          : 4200;
    setTimeout(() => {
      if (el.parentNode) el.remove();
    }, ms);
  }

  /** Recoverable turn-clear notices (stall / max duration / no output / auto-retry) — not hard failures. */
  function isRecoverableTurnClear(msg) {
    return /send again|try again|turn cleared|stalled mid-turn|no activity|max duration|no output|recovering|retrying|still not responding/i.test(
      String(msg || "")
    );
  }

  /**
   * Durable hub error: console + state.errorLog + toast + persistent strip.
   * Toasts still auto-dismiss; the strip stays until Dismiss.
   */
  function reportError(message, meta = {}) {
    const msg = String(message || "Error");
    const entry = {
      at: new Date().toISOString(),
      message: msg,
      sessionId: meta.sessionId || null,
      source: meta.source || "hub",
      level: "danger",
    };
    if (!state.errorLog) state.errorLog = [];
    state.errorLog.unshift(entry);
    if (state.errorLog.length > 40) state.errorLog.length = 40;
    try {
      console.error("[hub]", msg, meta || {});
    } catch (_) {}
    toast(msg, "danger", 8000);
    updateErrorStrip(entry);
    return entry;
  }

  /**
   * Soft recoverable notice (turn cleared, etc.): toast + info strip (auto-dismiss 12s).
   */
  function reportInfo(message, meta = {}) {
    const msg = String(message || "");
    if (!msg) return null;
    const entry = {
      at: new Date().toISOString(),
      message: msg,
      sessionId: meta.sessionId || null,
      source: meta.source || "hub",
      level: "info",
    };
    if (!state.errorLog) state.errorLog = [];
    state.errorLog.unshift(entry);
    if (state.errorLog.length > 40) state.errorLog.length = 40;
    try {
      console.info("[hub]", msg, meta || {});
    } catch (_) {}
    toast(msg, "", 6000);
    updateErrorStrip(entry);
    return entry;
  }

  function setStripResendVisible(show) {
    if (!els.btnErrorResend) return;
    if (show) els.btnErrorResend.classList.remove("hidden");
    else els.btnErrorResend.classList.add("hidden");
  }

  function updateErrorStrip(entry) {
    if (!els.errorStrip) return;
    if (state._infoStripTimer) {
      clearTimeout(state._infoStripTimer);
      state._infoStripTimer = null;
    }
    if (!entry) {
      els.errorStrip.classList.add("hidden");
      els.errorStrip.classList.remove("info", "danger");
      state._lastStripError = null;
      setStripResendVisible(false);
      return;
    }
    state._lastStripError = entry;
    els.errorStrip.classList.remove("hidden");
    const isInfo = entry.level === "info";
    els.errorStrip.classList.toggle("info", isInfo);
    els.errorStrip.classList.toggle("danger", !isInfo);
    if (els.errorStripMsg) els.errorStripMsg.textContent = entry.message || "";
    if (els.errorStripTime) {
      let label = "";
      try {
        label = new Date(entry.at).toLocaleTimeString();
      } catch (_) {
        label = "";
      }
      els.errorStripTime.textContent = label;
    }
    setStripResendVisible(!!entry.resend);
    // Info strip auto-dismisses; danger stays until Dismiss.
    if (isInfo) {
      state._infoStripTimer = setTimeout(() => {
        state._infoStripTimer = null;
        if (state._lastStripError === entry) updateErrorStrip(null);
      }, 12000);
    }
  }

  function dismissErrorStrip() {
    updateErrorStrip(null);
  }

  function copyErrorStrip() {
    const e = state._lastStripError || (state.errorLog && state.errorLog[0]);
    if (!e || !e.message) return;
    const text = e.message;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(() => {});
    }
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

  function shortSessionId(id) {
    const s = String(id || "").trim();
    if (!s) return "";
    if (s.length <= 12) return s;
    return s.slice(0, 8) + "…";
  }

  function buildTopbarBubbleText(meta) {
    meta = meta || {};
    const cwd = meta.cwd || "";
    const project = basename(cwd) || (meta.project || "");
    const model = meta.modelId || meta.model || "";
    const sid = meta.sessionId || state.selectedId || "";
    const base = topbarBubbleText(project, model, cwd);
    return sid ? base + "\nSession: " + sid : base;
  }

  function setTopbarSessionMeta(meta) {
    meta = meta || {};
    const title = meta.title || "Remote session";
    if (els.chatTitle) els.chatTitle.textContent = title;
    if (els.btnRenameSession) {
      if (state.selectedId) els.btnRenameSession.classList.remove("hidden");
      else els.btnRenameSession.classList.add("hidden");
    }
    // View plan visibility is driven by refreshSessionPlan (plan.md exists)

    const cwd = meta.cwd || "";
    const project = basename(cwd);
    const sid = String(meta.sessionId || state.selectedId || "").trim();
    if (els.chatProject) {
      if (project) {
        els.chatProject.textContent = project;
        els.chatProject.classList.remove("hidden");
      } else {
        els.chatProject.textContent = "";
        els.chatProject.classList.add("hidden");
      }
    }
    if (els.chatCwd) {
      els.chatCwd.textContent = cwd;
    }
    if (els.chatModel) {
      if (meta.modelId) {
        els.chatModel.textContent = meta.modelId;
        els.chatModel.classList.remove("hidden");
      } else {
        els.chatModel.textContent = "";
        els.chatModel.classList.add("hidden");
      }
    }
    if (els.chatSessionId) {
      if (sid) {
        els.chatSessionId.textContent = shortSessionId(sid);
        els.chatSessionId.dataset.sessionId = sid;
        els.chatSessionId.title = "Click to copy session id\n" + sid;
        els.chatSessionId.classList.remove("hidden");
      } else {
        els.chatSessionId.textContent = "";
        els.chatSessionId.dataset.sessionId = "";
        els.chatSessionId.classList.add("hidden");
      }
    }
    if (state.metaPopoverPinned || (els.metaPopover && !els.metaPopover.classList.contains("hidden"))) {
      refreshMetaPopoverContent();
    }
  }

  function clearTopbarSessionMeta() {
    if (els.chatTitle) els.chatTitle.textContent = "Select a session";
    if (els.btnRenameSession) els.btnRenameSession.classList.add("hidden");
    setViewPlanVisible(false);
    state.sessionPlan = null;
    if (els.chatProject) {
      els.chatProject.textContent = "";
      els.chatProject.classList.add("hidden");
    }
    if (els.chatCwd) els.chatCwd.textContent = "";
    if (els.chatModel) {
      els.chatModel.textContent = "";
      els.chatModel.classList.add("hidden");
    }
    if (els.chatSessionId) {
      els.chatSessionId.textContent = "";
      els.chatSessionId.dataset.sessionId = "";
      els.chatSessionId.classList.add("hidden");
    }
    hideMetaPopover();
  }

  function copySessionId(sessionId) {
    const sid = String(sessionId || state.selectedId || "").trim();
    if (!sid) return;
    const done = () => toast("Session id copied", "");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(sid).then(done).catch(() => {
        // Fallback
        try {
          const ta = document.createElement("textarea");
          ta.value = sid;
          ta.style.position = "fixed";
          ta.style.left = "-9999px";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          ta.remove();
          done();
        } catch (_) {
          toast("Could not copy session id", "danger");
        }
      });
    }
  }

  function clearMetaHideTimer() {
    if (state.metaHideTimer) {
      clearTimeout(state.metaHideTimer);
      state.metaHideTimer = null;
    }
  }

  function setMetaChipExpanded(on) {
    for (const el of [els.chatProject, els.chatModel, els.chatCwd]) {
      if (!el) continue;
      el.setAttribute("aria-expanded", on ? "true" : "false");
    }
  }

  function refreshMetaPopoverContent() {
    if (!els.metaPopover) return;
    const meta = state.selectedMeta || {};
    const cwd = meta.cwd || "";
    const project = basename(cwd) || meta.project || "—";
    const model = meta.modelId || meta.model || "—";
    const path = cwd || "—";
    const sid = String(meta.sessionId || state.selectedId || "").trim() || "—";
    // Structured HTML for clearer bubble (still plain text-safe via textContent)
    els.metaPopover.innerHTML = "";
    const rows = [
      ["Project", project],
      ["Model", model],
      ["Path", path],
      ["Session", sid],
    ];
    for (const [k, v] of rows) {
      const line = document.createElement("div");
      line.className = "meta-pop-line";
      const keyEl = document.createElement("span");
      keyEl.className = "meta-pop-k";
      keyEl.textContent = k + ":";
      const valEl = document.createElement("span");
      valEl.className = "meta-pop-v";
      valEl.textContent = v;
      if (k === "Session" && v && v !== "—") {
        valEl.classList.add("meta-pop-v-copy");
        valEl.title = "Click to copy";
        valEl.tabIndex = 0;
        valEl.setAttribute("role", "button");
        valEl.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          copySessionId(v);
        });
        valEl.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            copySessionId(v);
          }
        });
      }
      line.append(keyEl, valEl);
      els.metaPopover.appendChild(line);
    }
  }

  function positionMetaPopover(anchorEl) {
    if (!els.metaPopover || !anchorEl) return;
    const pad = 8;
    const maxW = Math.min(380, window.innerWidth * 0.92);
    els.metaPopover.style.maxWidth = `${maxW}px`;
    els.metaPopover.classList.remove("hidden");
    // Force layout so size is real (was 0 while display:none)
    const popW = Math.min(Math.max(els.metaPopover.offsetWidth || 0, 160), maxW);
    const popH = els.metaPopover.offsetHeight || 48;
    const anchorRect = anchorEl.getBoundingClientRect();
    let left = anchorRect.left;
    if (left + popW > window.innerWidth - pad) left = window.innerWidth - pad - popW;
    if (left < pad) left = pad;
    let top = anchorRect.bottom + 6;
    if (top + popH > window.innerHeight - pad && anchorRect.top > popH + pad) {
      top = anchorRect.top - popH - 6;
    }
    els.metaPopover.style.left = `${Math.round(left)}px`;
    els.metaPopover.style.top = `${Math.round(top)}px`;
  }

  function showMetaPopover(anchorEl) {
    if (!els.metaPopover) {
      els.metaPopover = document.getElementById("meta-popover");
    }
    if (!els.metaPopover) return;
    const meta = state.selectedMeta || {};
    if (!state.selectedId && !(meta.cwd || meta.modelId)) return;
    clearMetaHideTimer();
    refreshMetaPopoverContent();
    setMetaChipExpanded(true);
    const anchor =
      anchorEl ||
      (els.chatProject && !els.chatProject.classList.contains("hidden") && els.chatProject) ||
      (els.chatModel && !els.chatModel.classList.contains("hidden") && els.chatModel) ||
      els.chatCwd;
    if (!anchor) return;
    positionMetaPopover(anchor);
  }

  function hideMetaPopover() {
    clearMetaHideTimer();
    state.metaPopoverPinned = false;
    setMetaChipExpanded(false);
    if (els.metaPopover) {
      els.metaPopover.classList.add("hidden");
      els.metaPopover.innerHTML = "";
    }
  }

  function toggleMetaPopover(anchorEl) {
    const open =
      state.metaPopoverPinned &&
      els.metaPopover &&
      !els.metaPopover.classList.contains("hidden");
    if (open) {
      hideMetaPopover();
      return;
    }
    state.metaPopoverPinned = true;
    showMetaPopover(anchorEl);
  }

  function scheduleHideMetaPopover() {
    clearMetaHideTimer();
    if (state.metaPopoverPinned) return;
    state.metaHideTimer = setTimeout(() => {
      state.metaHideTimer = null;
      if (!state.metaPopoverPinned) hideMetaPopover();
    }, 220);
  }

  function bindMetaPopoverEvents() {
    // Re-resolve in case DOM moved
    if (!els.metaPopover) els.metaPopover = document.getElementById("meta-popover");
    const chips = [els.chatProject, els.chatModel, els.chatCwd].filter(Boolean);
    for (const el of chips) {
      el.addEventListener("mouseenter", () => {
        if (state.metaPopoverPinned) return;
        showMetaPopover(el);
      });
      el.addEventListener("mouseleave", () => {
        scheduleHideMetaPopover();
      });
      el.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        toggleMetaPopover(el);
      });
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggleMetaPopover(el);
        }
      });
      el.addEventListener("focus", () => {
        if (state.metaPopoverPinned) return;
        showMetaPopover(el);
      });
    }
    // Session id chip: copy on click (does not open meta popover).
    if (els.chatSessionId) {
      els.chatSessionId.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        copySessionId(els.chatSessionId.dataset.sessionId || state.selectedId);
      });
    }
    if (els.metaPopover) {
      els.metaPopover.addEventListener("mouseenter", () => {
        clearMetaHideTimer();
      });
      els.metaPopover.addEventListener("mouseleave", () => {
        scheduleHideMetaPopover();
      });
    }
    document.addEventListener("click", (e) => {
      if (!els.metaPopover || els.metaPopover.classList.contains("hidden")) return;
      const t = e.target;
      if (chips.some((c) => c && c.contains(t))) return;
      if (els.metaPopover.contains(t)) return;
      hideMetaPopover();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && els.metaPopover && !els.metaPopover.classList.contains("hidden")) {
        hideMetaPopover();
      }
    });
    window.addEventListener("resize", () => {
      if (!els.metaPopover || els.metaPopover.classList.contains("hidden")) return;
      const anchor =
        (els.chatProject && !els.chatProject.classList.contains("hidden") && els.chatProject) ||
        (els.chatModel && !els.chatModel.classList.contains("hidden") && els.chatModel) ||
        els.chatCwd;
      if (anchor) positionMetaPopover(anchor);
    });
  }

  function sessionTooltip(s) {
    const lines = [
      `Project: ${basename(s.cwd) || "—"}`,
      `Model: ${s.modelId || "—"}`,
      `Path: ${s.cwd || "—"}`,
    ];
    if (s.agentName) {
      lines.splice(1, 0, `Agent: ${s.agentName}`);
    }
    return lines.join("\n");
  }

  let renameInFlight = false;

  function applyLocalRename(sessionId, item) {
    const title = (item && item.title) || "";
    const idx = state.sessions.findIndex((s) => s.sessionId === sessionId);
    if (idx >= 0) {
      state.sessions[idx] = { ...state.sessions[idx], ...item, title };
    }
    if (state.selectedId === sessionId) {
      if (state.selectedMeta) {
        state.selectedMeta = { ...state.selectedMeta, ...item, title };
      }
      if (els.chatTitle && !els.chatTitle.querySelector("input")) {
        els.chatTitle.textContent = title || "Untitled session";
      }
    }
    renderSessions();
  }

  async function commitRenameSession(sessionId, nextTitle, prevTitle) {
    const cleaned = String(nextTitle || "").trim();
    if (!cleaned) {
      toast("Title cannot be empty", "danger");
      return false;
    }
    if (cleaned === String(prevTitle || "").trim()) return true;
    if (renameInFlight) return false;
    renameInFlight = true;
    try {
      const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: cleaned }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Rename failed", "danger");
        return false;
      }
      if (data.item) applyLocalRename(sessionId, data.item);
      else applyLocalRename(sessionId, { title: cleaned });
      return true;
    } catch (err) {
      toast("Rename failed: " + err, "danger");
      return false;
    } finally {
      renameInFlight = false;
    }
  }

  function startRenameSession(sessionId, currentTitle, anchorEl) {
    if (!sessionId || !anchorEl) return;
    if (anchorEl.querySelector && anchorEl.querySelector(".session-rename-input")) return;
    if (anchorEl.classList && anchorEl.classList.contains("session-rename-input")) return;

    const isTitleNode =
      anchorEl.classList &&
      (anchorEl.classList.contains("title") || anchorEl.id === "chat-title");
    const host = isTitleNode ? anchorEl : anchorEl;
    const prev = String(currentTitle || "").trim() || "Untitled session";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "session-rename-input";
    input.value = prev;
    input.setAttribute("aria-label", "Rename session");
    input.maxLength = 200;

    let finished = false;
    const row = host.closest ? host.closest(".session-row") : null;
    if (row) row.classList.add("renaming");

    const restore = (text) => {
      if (host.id === "chat-title") {
        host.textContent = text;
        if (els.btnRenameSession) els.btnRenameSession.classList.remove("hidden");
      } else if (host.classList && host.classList.contains("title")) {
        host.textContent = text;
      } else if (input.parentNode) {
        input.replaceWith(document.createTextNode(text));
      }
      if (row) row.classList.remove("renaming");
    };

    const finish = async (save) => {
      if (finished) return;
      finished = true;
      const next = input.value;
      input.removeEventListener("keydown", onKey);
      input.removeEventListener("blur", onBlur);
      if (!save) {
        restore(prev);
        return;
      }
      const cleaned = String(next || "").trim();
      if (!cleaned) {
        restore(prev);
        toast("Title cannot be empty", "danger");
        return;
      }
      restore(cleaned);
      const ok = await commitRenameSession(sessionId, cleaned, prev);
      if (!ok) restore(prev);
    };

    const onKey = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
        finish(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        finish(false);
      }
      e.stopPropagation();
    };
    const onBlur = () => {
      finish(true);
    };

    input.addEventListener("keydown", onKey);
    input.addEventListener("blur", onBlur);
    input.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
    });

    if (host.id === "chat-title" || (host.classList && host.classList.contains("title"))) {
      host.textContent = "";
      host.appendChild(input);
      if (host.id === "chat-title" && els.btnRenameSession) {
        els.btnRenameSession.classList.add("hidden");
      }
    } else {
      host.replaceWith(input);
    }
    input.focus();
    input.select();
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

  function snippetFromToolValue(value, limit = 120) {
    if (value == null || value === false) return "";
    if (typeof value === "string") return truncate(value, limit);
    if (typeof value === "number" || typeof value === "boolean") {
      return truncate(String(value), limit);
    }
    if (Array.isArray(value)) {
      const parts = [];
      for (const item of value) {
        const t = snippetFromToolValue(item, limit);
        if (t) parts.push(t);
        if (parts.join(" ").length >= limit) break;
      }
      return truncate(parts.join(" "), limit);
    }
    if (typeof value === "object") {
      // Prefer readable text fields over dumping whole objects.
      for (const key of [
        "text",
        "content",
        "output",
        "result",
        "message",
        "stdout",
        "stderr",
        "error",
      ]) {
        if (value[key] != null) {
          const t = snippetFromToolValue(value[key], limit);
          if (t) return t;
        }
      }
      const fromText = extractText(value);
      if (fromText && fromText.trim()) return truncate(fromText, limit);
      try {
        const json = JSON.stringify(value);
        if (json && json !== "{}" && json !== "[]" && json !== "null") {
          return truncate(json, limit);
        }
      } catch (_) {
        /* ignore circular / non-serializable */
      }
    }
    return "";
  }

  function extractToolContentSnippet(update, limit = 120) {
    if (!update || typeof update !== "object") return "";
    // Primary ACP shape
    const fromContent = snippetFromToolValue(update.content, limit);
    if (fromContent) return fromContent;
    // Broader ACP / stream shapes (output payloads on tool_call_update)
    for (const key of ["rawOutput", "raw_output", "output", "result"]) {
      if (update[key] == null) continue;
      const snip = snippetFromToolValue(update[key], limit);
      if (snip) return snip;
    }
    return "";
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
      if (
        state.attachSwitched &&
        state.livePromptSessionId &&
        state.livePromptSessionId !== state.selectedId
      ) {
        els.sessionBannerText.textContent =
          "Viewing this session’s history. Chat sends to the project’s live hub session (TUI stays separate).";
      } else if (state.attachSwitched) {
        els.sessionBannerText.textContent =
          "Live remote session for this project. Desktop TUI history is separate.";
      } else {
        els.sessionBannerText.textContent = "Live remote session";
      }
    } else {
      els.sessionBannerText.textContent =
        "Viewing saved history. Sending a message uses a live hub session for this project.";
    }
  }

  /** Session id used for prompts (may differ from selected when attach remapped). */
  function promptSessionId() {
    if (
      state.livePromptSessionId &&
      state.selectedId &&
      state.livePromptSessionId !== state.selectedId
    ) {
      return state.livePromptSessionId;
    }
    return state.selectedId;
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
      // After several failed WS opens (or /health says down), stop saying only "Reconnecting".
      const hubDown =
        state.wsState === "reconnecting" &&
        (state.reconnectAttempt >= 3 || state.hubReachable === false);
      if (hubDown) {
        stateKey = "hub-down";
        text =
          state.hubReachable === false
            ? "Hub unreachable: run start-hub.ps1"
            : "Hub down";
      } else {
        stateKey = state.wsState === "reconnecting" ? "reconnecting" : "connecting";
        text = state.wsState === "reconnecting" ? "Reconnecting" : "Connecting";
      }
    } else if (state.wsState === "open") {
      // Honest split: process down vs ACP quality (zombie/stale/disconnected).
      const ap = state.status.agentProcess;
      const acp = state.status.acpConnected;
      const detail = state.status.agentDetail;
      const quality = state.status.acpQuality;
      const processDown =
        ap === "down" ||
        detail === "process-down" ||
        (ap == null && state.status.agent !== "up");
      const acpZombie =
        detail === "acp-zombie" || quality === "zombie";
      const acpStale =
        detail === "acp-stale" || quality === "stale";
      const acpOnlyDown =
        ap === "up" &&
        !acpZombie &&
        !acpStale &&
        (acp === false ||
          detail === "acp-disconnected" ||
          state.status.agent !== "up");
      if (processDown) {
        stateKey = "agent-down";
        text = "Agent down";
      } else if (acpZombie) {
        stateKey = "acp-hung";
        text = "Agent hung — restart";
      } else if (acpStale) {
        stateKey = "acp-down";
        text = "ACP stale";
      } else if (acpOnlyDown) {
        // After heal exhaustion, stop implying a silent reconnect is still running.
        const healAttempts = Number(state.status.acpHealAttempts) || 0;
        const healExhausted =
          !!state.status.acpHealError || healAttempts >= 3;
        if (healExhausted) {
          stateKey = "acp-hung";
          text = "Agent hung — restart";
        } else {
          stateKey = "acp-down";
          text = "Agent reconnecting…";
        }
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

    if (state.restartingAgent) {
      text = "Restarting agent…";
    }

    pill.dataset.state = stateKey;
    label.textContent = text;

    // Hung / process-down: pill is a KillAgent-style restart control (click only).
    const restartable =
      !state.restartingAgent &&
      (stateKey === "acp-hung" || stateKey === "agent-down");
    if (restartable) {
      pill.setAttribute("role", "button");
      pill.tabIndex = 0;
      pill.title = "Restart agent (KillAgent-style)";
      pill.classList.add("status-pill-action");
      pill.classList.remove("is-restarting");
      pill.setAttribute("aria-disabled", "false");
    } else if (state.restartingAgent) {
      pill.setAttribute("role", "button");
      pill.tabIndex = -1;
      pill.title = "Restarting agent…";
      pill.classList.add("status-pill-action", "is-restarting");
      pill.setAttribute("aria-disabled", "true");
    } else {
      pill.removeAttribute("role");
      pill.removeAttribute("tabindex");
      pill.title = "Connection status";
      pill.classList.remove("status-pill-action", "is-restarting");
      pill.removeAttribute("aria-disabled");
    }
    updateTurnStrip();
  }

  /**
   * POST /api/admin/restart-agent — KillAgent-style serve kill + respawn + ACP reconnect.
   * Does not restart the hub process.
   */
  async function restartAgentFromPill() {
    if (state.restartingAgent) return;
    const st = els.statusPill && els.statusPill.dataset.state;
    if (st !== "acp-hung" && st !== "agent-down") return;
    if (
      !window.confirm(
        "Restart agent process? In-flight turns will stop."
      )
    ) {
      return;
    }
    state.restartingAgent = true;
    updateStatusPill();
    toast("Restarting agent…");
    try {
      const res = await fetch(apiUrl("/api/admin/restart-agent"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      let data = {};
      try {
        data = await res.json();
      } catch (_) {
        data = {};
      }
      if (!res.ok || !data.ok) {
        toast(
          (data && data.error) || "Agent restart failed",
          "danger",
          10000
        );
        return;
      }
      toast("Agent restarted");
    } catch (err) {
      toast("Agent restart failed: " + err, "danger", 10000);
    } finally {
      state.restartingAgent = false;
      updateStatusPill();
    }
  }

  // Client soft-warn only (matches hub.session_policy CLIENT_STALL_WARN_SECONDS).
  // Auto unlock/reset-turn is disabled (CLIENT_STALL_UNLOCK_SECONDS = 0).
  const CLIENT_STALL_WARN_MS = 120000;

  function liveTurnId() {
    return (
      state.liveTurnSessionId ||
      (state.status && state.status.turnSessionId) ||
      null
    );
  }

  function sessionLiveStatus(sessionId) {
    if (!sessionId) return "idle";
    // Order of truth: pending questions always win over working/idle flags.
    // Status broadcasts can replace sessionFlags with idle/working and would
    // otherwise mask a just-set question until the next user_question event.
    const pending = state.pendingQuestionSessions || [];
    if (pending.indexOf(sessionId) >= 0) return "question";
    const flags = state.sessionFlags || {};
    if (flags[sessionId] === "question") return "question";
    const turns = state.liveTurns || [];
    for (let i = 0; i < turns.length; i++) {
      if (turns[i] && turns[i].sessionId === sessionId) return "working";
    }
    // Legacy single-turn fallback
    if (
      state.turnRunning &&
      (sessionId === liveTurnId() ||
        sessionId === (state.status && state.status.turnSessionId))
    ) {
      return "working";
    }
    if (flags[sessionId] === "working") return "working";
    // Explicit idle flag wins over stale sessions-list liveStatus (stall clear).
    if (flags[sessionId] === "idle") return "idle";
    // Session payload liveStatus from list scan
    const row = (state.sessions || []).find((s) => s.sessionId === sessionId);
    if (row && (row.liveStatus === "working" || row.liveStatus === "question" || row.liveStatus === "idle")) {
      return row.liveStatus;
    }
    return "idle";
  }

  function sessionStatusRank(st) {
    if (st === "question") return 2;
    if (st === "working") return 1;
    return 0;
  }

  /** @type {Record<string, number>} */
  let _sessionPillRanks = {};
  let _sessionPillsRaf = 0;
  let _sessionPillsTimer = 0;

  /**
   * Near-streaming pill updates: set live flags without thrashing full list rebuild.
   * @param {string} sessionId
   * @param {"working"|"question"|"idle"} mode
   */
  function markSessionActivity(sessionId, mode) {
    if (!sessionId) return;
    if (mode !== "working" && mode !== "question" && mode !== "idle") return;
    if (!state.sessionFlags) state.sessionFlags = {};

    if (mode === "working") {
      // Never overwrite question with working (question wins until resolved/idle).
      if (state.sessionFlags[sessionId] !== "question") {
        const pending = state.pendingQuestionSessions || [];
        if (pending.indexOf(sessionId) < 0) {
          state.sessionFlags[sessionId] = "working";
        }
      }
      if (!state.liveTurns) state.liveTurns = [];
      if (!state.liveTurns.some((t) => t && t.sessionId === sessionId)) {
        state.liveTurns.push({ sessionId: sessionId, state: "running" });
      }
    } else if (mode === "question") {
      state.sessionFlags[sessionId] = "question";
    } else {
      state.sessionFlags[sessionId] = "idle";
      state.liveTurns = (state.liveTurns || []).filter(
        (t) => t && t.sessionId !== sessionId
      );
      // Idle from turn end: drop pending question for this session.
      state.pendingQuestionSessions = (state.pendingQuestionSessions || []).filter(
        (s) => s !== sessionId
      );
    }

    const row = (state.sessions || []).find((s) => s && s.sessionId === sessionId);
    if (row) row.liveStatus = sessionLiveStatus(sessionId);

    scheduleSessionPills();
  }

  /** Coalesce pill DOM updates to rAF (or max ~50ms). */
  function scheduleSessionPills() {
    if (_sessionPillsRaf) return;
    const flush = () => {
      _sessionPillsRaf = 0;
      if (_sessionPillsTimer) {
        clearTimeout(_sessionPillsTimer);
        _sessionPillsTimer = 0;
      }
      flushSessionPills();
    };
    _sessionPillsRaf = requestAnimationFrame(flush);
    if (!_sessionPillsTimer) {
      _sessionPillsTimer = setTimeout(() => {
        _sessionPillsTimer = 0;
        if (_sessionPillsRaf) {
          cancelAnimationFrame(_sessionPillsRaf);
          _sessionPillsRaf = 0;
        }
        flushSessionPills();
      }, 50);
    }
  }

  function flushSessionPills() {
    if (!els.sessionList) return;
    const rows = els.sessionList.querySelectorAll(".session-row[data-session-id]");
    let rankChanged = false;
    const nextRanks = {};
    for (let i = 0; i < rows.length; i++) {
      const id = rows[i].getAttribute("data-session-id");
      if (!id) continue;
      const rank = sessionStatusRank(sessionLiveStatus(id));
      nextRanks[id] = rank;
      const prev = Object.prototype.hasOwnProperty.call(_sessionPillRanks, id)
        ? _sessionPillRanks[id]
        : 0;
      if (prev !== rank) rankChanged = true;
    }
    // Full rebuild only when status rank changes (sort order may change).
    if (rankChanged) {
      renderSessions();
      return;
    }
    syncVisibleSessionPills();
    _sessionPillRanks = nextRanks;
  }

  /** In-place Working / Needs reply pill updates (no list rebuild). */
  function syncVisibleSessionPills() {
    if (!els.sessionList) return;
    const rows = els.sessionList.querySelectorAll(".session-row[data-session-id]");
    for (let i = 0; i < rows.length; i++) {
      const btn = rows[i];
      const id = btn.getAttribute("data-session-id");
      if (!id) continue;
      const liveStatus = sessionLiveStatus(id);
      btn.classList.remove("turn-live", "status-working", "status-question");
      if (liveStatus === "working") btn.classList.add("turn-live", "status-working");
      if (liveStatus === "question") btn.classList.add("turn-live", "status-question");

      const titleRow = btn.querySelector(".title-row");
      if (titleRow) {
        const stale = titleRow.querySelectorAll(
          ".session-pill.status-working, .session-pill.status-question"
        );
        for (let j = 0; j < stale.length; j++) stale[j].remove();
        if (liveStatus === "working" || liveStatus === "question") {
          const pill = document.createElement("span");
          pill.className =
            liveStatus === "working"
              ? "session-pill status-working"
              : "session-pill status-question";
          pill.textContent = liveStatus === "working" ? "Working" : "Needs reply";
          const title = titleRow.querySelector(".title");
          if (title && title.nextSibling) {
            titleRow.insertBefore(pill, title.nextSibling);
          } else if (title) {
            titleRow.appendChild(pill);
          } else {
            titleRow.insertBefore(pill, titleRow.firstChild);
          }
        }
      }

      const meta = btn.querySelector(".meta");
      if (meta) {
        let turnHint = meta.querySelector(".turn-hint");
        const isLiveTurn = liveStatus === "working" || liveStatus === "question";
        const hint = sessionListProgressHint({
          is_live_turn: isLiveTurn,
          tool: toolTitleForSession(id),
        });
        if (hint) {
          if (!turnHint) {
            turnHint = document.createElement("span");
            turnHint.className = "turn-hint";
            const last = meta.lastElementChild;
            if (last) meta.insertBefore(turnHint, last);
            else meta.appendChild(turnHint);
          }
          turnHint.textContent = hint;
        } else if (turnHint) {
          turnHint.remove();
        }
      }
    }
  }

  function turnRunningOnSelected() {
    if (!state.selectedId) return false;
    const st = sessionLiveStatus(state.selectedId);
    return st === "working" || st === "question";
  }

  function hasOtherProjectTurn() {
    if (!state.selectedId) return !!state.turnRunning || (state.liveTurns || []).length > 0;
    const turns = state.liveTurns || [];
    if (turns.length) {
      return turns.some((t) => t && t.sessionId && t.sessionId !== state.selectedId);
    }
    const live = liveTurnId();
    return !!(state.turnRunning && live && live !== state.selectedId);
  }

  function toolTitleForSession(sessionId) {
    if (!sessionId) return "";
    if (sessionId === state.selectedId && state.streamBuffers) {
      return state.streamBuffers.lastToolTitle || "";
    }
    const v = state.sessionViews.get(sessionId);
    if (v && v.streamBuffers) return v.streamBuffers.lastToolTitle || "";
    if (v) return v.lastToolTitle || "";
    return "";
  }

  function countResidualInPane(pane) {
    const counts = {
      plan_pending: 0,
      plan_running: 0,
      plan_failed: 0,
      tool_pending: 0,
      tool_running: 0,
      tool_failed: 0,
    };
    if (!pane) return counts;
    // Only last-turn leftovers: history often leaves tools/plans open or failed.
    const users = pane.querySelectorAll(".term-line.user");
    const lastUser = users.length ? users[users.length - 1] : null;
    if (!lastUser) return counts;
    function afterLastUser(el) {
      return !!(lastUser.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
    }
    const planItems = pane.querySelectorAll(".plan-item");
    for (const li of planItems) {
      if (!afterLastUser(li)) continue;
      const st = normalizeStatus(li.dataset.status || "");
      if (st === "pending" || st === "in_progress") counts.plan_pending += 1;
      else if (st === "running") counts.plan_running += 1;
      else if (st === "failed" || st === "error") counts.plan_failed += 1;
    }
    const tools = pane.querySelectorAll("details.term-line.tool, .term-line.tool");
    for (const row of tools) {
      if (!afterLastUser(row)) continue;
      const st = normalizeStatus(row.dataset.status || "");
      if (st === "pending" || st === "in_progress") counts.tool_pending += 1;
      else if (st === "running") counts.tool_running += 1;
      else if (st === "failed" || st === "error") counts.tool_failed += 1;
    }
    return counts;
  }

  function markStalePlanItems(pane, stale) {
    if (!pane) return;
    const items = pane.querySelectorAll(".plan-item");
    for (const li of items) {
      const st = normalizeStatus(li.dataset.status || "");
      const open =
        st === "pending" ||
        st === "running" ||
        st === "in_progress" ||
        st === "failed" ||
        st === "error";
      if (stale && open) li.classList.add("stale");
      else li.classList.remove("stale");
    }
    const tools = pane.querySelectorAll("details.term-line.tool, .term-line.tool");
    for (const row of tools) {
      const st = normalizeStatus(row.dataset.status || "");
      const open =
        st === "pending" ||
        st === "running" ||
        st === "in_progress" ||
        st === "failed" ||
        st === "error";
      if (stale && open) row.classList.add("stale");
      else row.classList.remove("stale");
    }
  }

  let _turnStripRaf = 0;
  function scheduleTurnStrip() {
    if (state._historyBatchDepth > 0) return;
    if (_turnStripRaf) return;
    _turnStripRaf = requestAnimationFrame(() => {
      _turnStripRaf = 0;
      updateTurnStrip();
    });
  }

  /** ~5s client-only cue while open tools wait with no new ACP detail. */
  const OPEN_TOOL_HEARTBEAT_MS = 5000;
  let _openToolHeartbeat = null;

  function clearOpenToolHeartbeat() {
    if (_openToolHeartbeat) {
      clearInterval(_openToolHeartbeat);
      _openToolHeartbeat = null;
    }
  }

  function ensureOpenToolHeartbeat() {
    if (_openToolHeartbeat) return;
    _openToolHeartbeat = setInterval(tickOpenToolHeartbeat, OPEN_TOOL_HEARTBEAT_MS);
  }

  function syncOpenToolHeartbeat(running, hasOpenTools) {
    if (running && hasOpenTools) ensureOpenToolHeartbeat();
    else clearOpenToolHeartbeat();
  }

  function tickOpenToolHeartbeat() {
    if (!state.turnRunning || !turnRunningOnSelected()) {
      clearOpenToolHeartbeat();
      scheduleTurnStrip();
      return;
    }
    const pane =
      (state.activePane && !state.activePane.hidden && state.activePane) ||
      (state.selectedId && state.sessionViews.get(state.selectedId)
        ? state.sessionViews.get(state.selectedId).pane
        : null);
    if (!pane) {
      clearOpenToolHeartbeat();
      return;
    }
    const residual = countResidualInPane(pane);
    const hasOpenTools =
      residual.tool_pending + residual.tool_running > 0 ||
      residual.plan_pending + residual.plan_running > 0;
    if (!hasOpenTools) {
      clearOpenToolHeartbeat();
      scheduleTurnStrip();
      return;
    }
    const users = pane.querySelectorAll(".term-line.user");
    const lastUser = users.length ? users[users.length - 1] : null;
    const tools = pane.querySelectorAll("details.term-line.tool, .term-line.tool");
    for (const row of tools) {
      if (
        lastUser &&
        !(lastUser.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING)
      ) {
        continue;
      }
      const st = normalizeStatus(row.dataset.status || "");
      if (st !== "pending" && st !== "running" && st !== "in_progress") continue;
      if (!row.dataset.openedAt) row.dataset.openedAt = String(Date.now());
      const openedAt = Number(row.dataset.openedAt) || Date.now();
      const waitS = Math.max(0, Math.floor((Date.now() - openedAt) / 1000));
      const oneLiner = row.querySelector(".tool-one-liner");
      if (!oneLiner) continue;
      // Only fill waiting cue when there is no real detail yet (do not fake stream).
      const stored = String(row._toolSummary || "").trim();
      const nameText = String(row._toolTitle || "").trim();
      if (!stored || toolOneLinerRedundant(nameText, stored)) {
        oneLiner.textContent = "waiting · " + waitS + "s";
        oneLiner.hidden = false;
      }
    }
    // Repaint strip elapsed/tool; never noteTermLineActivity (no fake server activity).
    scheduleTurnStrip();
  }

  function updateTurnStrip() {
    if (!els.turnStrip || !els.turnStripText) return;
    const running = turnRunningOnSelected();
    const model =
      (state.selectedMeta && state.selectedMeta.modelId) ||
      (els.chatModel && !els.chatModel.classList.contains("hidden") ? els.chatModel.textContent : "") ||
      "";
    // Always label from the selected session (not a temporary offscreen target).
    const tool = toolTitleForSession(state.selectedId);
    const idleMs = running
      ? Date.now() - (state.lastTermLineAt || state.turnStartedAt || Date.now())
      : 0;
    // Visual quiet cue only — never unlocks or ends the turn.
    const quietVisual = running && idleMs >= CLIENT_STALL_WARN_MS;
    const elapsedS = running
      ? Math.floor((Date.now() - (state.turnStartedAt || Date.now())) / 1000)
      : 0;

    const pane =
      (state.activePane && !state.activePane.hidden && state.activePane) ||
      (state.selectedId && state.sessionViews.get(state.selectedId)
        ? state.sessionViews.get(state.selectedId).pane
        : null);
    const residual = countResidualInPane(pane);
    const hasResidual =
      residual.plan_pending +
        residual.plan_running +
        residual.plan_failed +
        residual.tool_pending +
        residual.tool_running +
        residual.tool_failed >
      0;
    // Mid-tool / open plan: do not paint bare quiet hang while work is still open.
    const hasOpenTools =
      residual.tool_pending + residual.tool_running > 0 ||
      residual.plan_pending + residual.plan_running > 0;
    const quietForLabel = quietVisual && !hasOpenTools;

    if (running) {
      els.turnStrip.dataset.state = quietForLabel ? "stalled" : "running";
      els.turnStripText.textContent = turnProgressLabel({
        running: true,
        tool,
        queue: state.promptQueueLength || 0,
        model,
        quiet: quietForLabel,
        tool_open: hasOpenTools,
        elapsed_s: elapsedS,
      });
      if (els.turnStripCursor) els.turnStripCursor.classList.remove("hidden");
      markStalePlanItems(pane, false);
    } else {
      els.turnStrip.dataset.state = hasResidual ? "residual" : "idle";
      els.turnStripText.textContent = idleTurnLabel({
        model: state.selectedId ? model : "",
        ...residual,
      });
      if (els.turnStripCursor) els.turnStripCursor.classList.add("hidden");
      markStalePlanItems(
        pane,
        shouldMarkPlanStale({ turn_running: false, has_open_or_failed: hasResidual })
      );
    }
    syncOpenToolHeartbeat(running, hasOpenTools);
    updateCapacityBanner();
  }

  /**
   * Compact multi-turn capacity strip from status.liveTurns / capacity.
   * Soft language: "quiet" (not stuck) until silence >= 120s; warn tint then.
   */
  function updateCapacityBanner() {
    if (!els.capacityBanner || !els.capacityBannerText) return;
    const capacity = state.capacity || null;
    const turns = state.liveTurns || [];
    const activeCount =
      capacity && capacity.activeTurnCount != null
        ? Number(capacity.activeTurnCount) || 0
        : turns.length;
    if (activeCount <= 0) {
      els.capacityBanner.classList.add("hidden");
      els.capacityBanner.dataset.state = "idle";
      els.capacityBannerText.textContent = "";
      return;
    }

    const busyIds =
      (capacity && Array.isArray(capacity.busySessionIds) && capacity.busySessionIds) ||
      turns.map((t) => (t && t.sessionId) || null).filter(Boolean);
    const selected = state.selectedId;
    let turn = null;
    for (let i = 0; i < turns.length; i++) {
      if (turns[i] && turns[i].sessionId === selected) {
        turn = turns[i];
        break;
      }
    }
    const selectedBusy =
      !!turn || (selected && busyIds.indexOf(selected) >= 0);
    const onOther = !!selected && !selectedBusy && busyIds.length > 0;

    // Prefer selected turn metrics; else primary / first live turn.
    if (!turn && turns.length) turn = turns[0];

    let silence = turn && turn.silenceSeconds != null ? Number(turn.silenceSeconds) : null;
    let sawUpdate = turn ? !!turn.sawUpdate : false;
    if (silence == null && state.turnSilenceSeconds != null) {
      silence = Number(state.turnSilenceSeconds);
    }
    if (turn == null && state.liveTurnSessionId) {
      // top-level primary turn may still have sawUpdate via liveTurns only
      sawUpdate = false;
    }
    // Drift silence from last status so banner ticks between WS broadcasts.
    if (silence != null && state._capacityStatusAt) {
      silence = silence + (Date.now() - state._capacityStatusAt) / 1000;
    }
    if (silence == null || Number.isNaN(silence) || silence < 0) silence = 0;
    const silenceS = Math.floor(silence);
    const warn = silenceS >= 120;
    // Selected pane open tools/plan: mid-tool wait, not bare hang.
    const pane =
      (state.activePane && !state.activePane.hidden && state.activePane) ||
      (state.selectedId && state.sessionViews.get(state.selectedId)
        ? state.sessionViews.get(state.selectedId).pane
        : null);
    const residual = countResidualInPane(pane);
    const hasOpenTools =
      residual.tool_pending + residual.tool_running > 0 ||
      residual.plan_pending + residual.plan_running > 0;
    // Soft language until warn threshold; still "quiet" (tint carries severity).
    // When silence high but tools open: "Working · tool open · quiet Ns".
    let detail;
    if (hasOpenTools && selectedBusy) {
      detail = `tool open · quiet ${silenceS}s`;
    } else if (sawUpdate) {
      detail = `quiet ${silenceS}s`;
    } else {
      detail = `waiting first token ${silenceS}s`;
    }
    const text = onOther
      ? `Busy on other session · ${detail}`
      : `Working · ${detail}`;

    els.capacityBannerText.textContent = text;
    els.capacityBanner.classList.remove("hidden");
    els.capacityBanner.dataset.state = warn ? "warn" : "working";
  }

  /**
   * Soft same-id context budget banner. Advisory only; never blocks Send.
   * Server: status.contextBudget { level, updatesBytes, message }.
   */
  function updateContextBudgetBanner() {
    if (!els.contextBudgetBanner || !els.contextBudgetBannerText) return;
    const budget = state.contextBudget;
    const level = budget && budget.level ? String(budget.level) : "ok";
    const soft = level === "soft";
    if (!soft) {
      els.contextBudgetBanner.classList.add("hidden");
      els.contextBudgetBanner.dataset.level = "ok";
      els.contextBudgetBannerText.textContent = "";
      return;
    }
    const msg =
      (budget && budget.message) ||
      "Heavy session — responses may be slow. Prefer compact/continue same thread; use New only to fork.";
    els.contextBudgetBannerText.textContent = msg;
    els.contextBudgetBanner.dataset.level = "soft";
    els.contextBudgetBanner.classList.remove("hidden");
  }

  function composerConnected() {
    return state.wsState === "open" && state.status.agent === "up";
  }

  function forceComposerUnlocked() {
    // Hard unlock typing/send when a session is selected and hub is connected.
    // Never gate on turnRunning — queue path requires Send during turns.
    if (!state.selectedId || !composerConnected()) return;
    if (els.input) {
      els.input.disabled = false;
      els.input.readOnly = false;
      els.input.removeAttribute("disabled");
      els.input.removeAttribute("readonly");
    }
    if (els.btnSend) {
      els.btnSend.disabled = false;
      els.btnSend.removeAttribute("disabled");
    }
  }

  function setComposerEnabled(on) {
    // `on` means hub/agent connectivity only — never use turnRunning to disable typing.
    const allowType = !!on && !!state.selectedId;
    if (els.input) {
      els.input.disabled = !allowType;
      els.input.readOnly = false;
      if (allowType) {
        els.input.removeAttribute("disabled");
        els.input.removeAttribute("readonly");
      }
    }
    if (els.btnSend) {
      els.btnSend.disabled = !allowType;
      if (allowType) els.btnSend.removeAttribute("disabled");
    }
    if (els.btnStop) {
      els.btnStop.classList.toggle("hidden", !turnRunningOnSelected());
    }
    // Reload: Stop + re-attach same id. Visible whenever a session is open (not empty main).
    if (els.btnReload) {
      const showReload = !!state.selectedId && !state.reloadingSession;
      els.btnReload.classList.toggle("hidden", !showReload);
      els.btnReload.disabled = !!state.reloadingSession;
    }
    if (!state.selectedId) {
      els.composerHint.textContent =
        "Remote agent stream. Load a session to chat; desktop TUI stays separate.";
    } else if (turnRunningOnSelected()) {
      const q = state.promptQueueLength || 0;
      const st = sessionLiveStatus(state.selectedId);
      if (st === "question") {
        els.composerHint.textContent = "Agent is asking a question — answer in the dialog.";
      } else {
        els.composerHint.textContent = q
          ? `Turn running · ${q} queued. Send adds to queue.`
          : "Turn running · Send queues your next message.";
      }
    } else if (hasOtherProjectTurn()) {
      els.composerHint.textContent =
        "Other projects may be working. You can send here anytime.";
    } else if (state.sessionMode === "history") {
      els.composerHint.textContent =
        "History view. Opening attaches a live remote session (not the desktop TUI).";
    } else if (!state.commands.length) {
      els.composerHint.textContent =
        "Live remote stream. Type / for built-in commands (agent list loads when available).";
    } else {
      els.composerHint.textContent = `${state.commands.length} slash commands available. Type / to open palette.`;
    }
    if (allowType) forceComposerUnlocked();
    updateTurnStrip();
    updateComposerPlaceholder();
  }

  function clearStallWatch() {
    if (state.stallTimer) {
      clearInterval(state.stallTimer);
      state.stallTimer = null;
    }
    state.stallWarned = false;
    clearOpenToolHeartbeat();
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
      if (!state.stallWarned && idleMs >= CLIENT_STALL_WARN_MS && turnRunningOnSelected()) {
        state.stallWarned = true;
        toast(
          "Still working (like desktop TUI). Use Stop to cancel.",
          ""
        );
        setComposerEnabled(composerConnected());
      }
    }, 1000);
  }

  function markThoughtComplete(buffers) {
    if (!buffers || !buffers.thoughtEl) return;
    const el = buffers.thoughtEl;
    // Leave open so user can still read; only update the label.
    const label = el.querySelector && el.querySelector(".thought-summary-label");
    if (label) label.textContent = "Thinking";
  }

  /**
   * Drop stale client mid-turn / queue state when the server has no live turns
   * (soft reconnect) or the hub process restarted.
   * Does not clear composer drafts.
   * @param {{ clearQuestions?: boolean }} opts
   */
  function clearStaleLiveTurns(opts = {}) {
    const clearQuestions = !!opts.clearQuestions;
    state.liveTurns = [];
    state.turnRunning = false;
    state.liveTurnSessionId = null;
    if (state.status) {
      state.status.turnRunning = false;
      state.status.turnSessionId = null;
    }
    state.turnStartedAt = null;
    state.lastTermLineAt = null;
    state.promptQueueLength = 0;
    clearStallWatch();

    const flags = state.sessionFlags || {};
    const next = {};
    for (const k of Object.keys(flags)) {
      next[k] = "idle";
    }
    if (!clearQuestions) {
      const pending = state.pendingQuestionSessions || [];
      for (let i = 0; i < pending.length; i++) {
        const pid = pending[i];
        if (pid) next[pid] = "question";
      }
    }
    state.sessionFlags = next;

    if (clearQuestions) {
      state.pendingQuestionSessions = [];
      closeAskUserModal();
    }

    if (state.sessionViews) {
      for (const v of state.sessionViews.values()) {
        if (v && v.pane) {
          markStalePlanItems(v.pane, true);
          clearActiveUserPrompt(v.pane);
          finalizeAssistantTables(v.pane);
        }
      }
    }
    if (state.activePane) markStalePlanItems(state.activePane, true);
    clearActiveUserPrompt();
    finalizeAssistantTables(transcriptRoot());

    setComposerEnabled(composerConnected());
    forceComposerUnlocked();
    updateTurnStrip();
    renderSessions();
  }

  /** Full wipe of live client state after hub process restart (pending Qs die with process). */
  function clearLiveClientStateAfterProcessRestart(reason) {
    clearStaleLiveTurns({ clearQuestions: true });
    try {
      console.info(
        "[hub] cleared live client state after process restart:",
        reason || ""
      );
    } catch (_) {}
  }

  /** Snapshot client live flags before noteBootId/mergeHealth can wipe them. */
  function snapshotLiveClientForRestart() {
    return {
      turnRunning: !!state.turnRunning,
      liveTurnSessionId: state.liveTurnSessionId || null,
      liveTurns: (state.liveTurns || []).slice(),
      workingSessionIds: Object.keys(state.sessionFlags || {}).filter(
        (k) =>
          state.sessionFlags[k] === "working" ||
          state.sessionFlags[k] === "question"
      ),
      promptQueueLength: state.promptQueueLength || 0,
      selectedId: state.selectedId || null,
    };
  }

  function snapshotHadLive(snap) {
    if (!snap) return false;
    return !!(
      snap.turnRunning ||
      snap.liveTurnSessionId ||
      (snap.liveTurns && snap.liveTurns.length) ||
      (snap.workingSessionIds && snap.workingSessionIds.length) ||
      snap.promptQueueLength > 0
    );
  }

  const LAST_PROMPT_KEY = "grh.lastPrompt.v1";

  function saveLastPrompt({ sessionId, text, cwd }) {
    const t = String(text || "").trim();
    if (!t) return;
    try {
      sessionStorage.setItem(
        LAST_PROMPT_KEY,
        JSON.stringify({
          sessionId: sessionId || null,
          text: t,
          cwd: cwd || "",
          at: Date.now(),
          pending: true,
        })
      );
    } catch (_) {}
  }

  function loadLastPrompt() {
    try {
      const raw = sessionStorage.getItem(LAST_PROMPT_KEY);
      if (!raw) return null;
      const j = JSON.parse(raw);
      if (!j || typeof j !== "object") return null;
      if (!j.text || !String(j.text).trim()) return null;
      return j;
    } catch (_) {
      return null;
    }
  }

  function markLastPromptPending(pending) {
    try {
      const lp = loadLastPrompt();
      if (!lp) return;
      lp.pending = !!pending;
      sessionStorage.setItem(LAST_PROMPT_KEY, JSON.stringify(lp));
    } catch (_) {}
  }

  function clearLastPromptPending() {
    markLastPromptPending(false);
  }

  /**
   * One-tap Resend after hub process restart interrupted a live turn.
   * Toast (with action) + durable error strip Resend button.
   */
  function offerInterruptedResend(lastPrompt, meta = {}) {
    const stripMsg = "Hub restarted: live turn interrupted.";
    const entry = {
      at: new Date().toISOString(),
      message: stripMsg,
      sessionId: (lastPrompt && lastPrompt.sessionId) || null,
      source: (meta && meta.source) || "reconnect",
      level: "danger",
      resend: true,
    };
    if (!state.errorLog) state.errorLog = [];
    state.errorLog.unshift(entry);
    if (state.errorLog.length > 40) state.errorLog.length = 40;
    try {
      console.error("[hub]", stripMsg, meta || {});
    } catch (_) {}
    toast("Hub restarted — turn interrupted", "danger", 30000, {
      actionLabel: "Resend",
      onAction: () => {
        resendLastPrompt();
        dismissErrorStrip();
      },
    });
    updateErrorStrip(entry);
  }

  async function resendLastPrompt() {
    const lp = loadLastPrompt();
    if (!lp || !lp.text || !String(lp.text).trim()) {
      toast("No stored prompt to resend", "danger");
      return;
    }
    const text = String(lp.text).trim();
    // Prefer the session that owned the last prompt when still in the list.
    if (lp.sessionId && lp.sessionId !== state.selectedId) {
      const row = (state.sessions || []).find(
        (s) => s && s.sessionId === lp.sessionId
      );
      if (row) {
        try {
          await openSession(row);
        } catch (_) {}
      }
    }
    let cwd =
      (state.selectedMeta && state.selectedMeta.cwd) ||
      lp.cwd ||
      "";
    if (!cwd && state.selectedId && Array.isArray(state.sessions)) {
      const row = state.sessions.find(
        (s) => s && s.sessionId === state.selectedId
      );
      if (row && row.cwd) cwd = row.cwd;
    }
    if (state.selectedId && cwd) {
      try {
        await attachSessionLive(state.selectedId, cwd, {
          showFailToast: false,
          focusOnFail: false,
        });
      } catch (_) {}
    }
    if (!state.selectedId) {
      toast("No session selected for resend", "danger");
      return;
    }
    if (els.input) {
      els.input.value = text;
      autoGrow();
    }
    submitPrompt();
  }

  function setTurnRunning(running, sessionId, opts) {
    if (!running && opts && opts.all) {
      clearStaleLiveTurns({ clearQuestions: !!opts.clearQuestions });
      return;
    }
    state.turnRunning = !!running;
    if (running) {
      if (sessionId) state.liveTurnSessionId = sessionId;
      startStallWatch();
    } else {
      clearStallWatch();
      const clearId = sessionId || state.liveTurnSessionId;
      if (state.streamBuffers && (!clearId || clearId === state.selectedId)) {
        state.streamBuffers.lastToolTitle = "";
        markThoughtComplete(state.streamBuffers);
      }
      if (clearId) {
        const v = state.sessionViews.get(clearId);
        if (v) {
          v.lastToolTitle = "";
          if (v.streamBuffers) {
            v.streamBuffers.lastToolTitle = "";
            markThoughtComplete(v.streamBuffers);
          }
          // Drop sticky active prompt for the session whose turn ended
          if (v.pane) clearActiveUserPrompt(v.pane);
        }
        // Selected pane without a cached view entry
        if (!v && (!clearId || clearId === state.selectedId)) {
          clearActiveUserPrompt();
        }
        state.liveTurns = (state.liveTurns || []).filter(
          (t) => t && t.sessionId !== clearId
        );
        if (state.sessionFlags) state.sessionFlags[clearId] = "idle";
      } else {
        clearActiveUserPrompt();
      }
      // Keep liveTurnSessionId if other turns remain; full idle when none left
      if (!(state.liveTurns && state.liveTurns.length)) {
        state.liveTurnSessionId = null;
        state.turnRunning = false;
        state.turnStartedAt = null;
        state.lastTermLineAt = null;
        clearActiveUserPrompt();
        // Best-effort: clear Resend pending once the client is fully idle.
        try {
          const lp = loadLastPrompt();
          if (lp && lp.pending) {
            if (!lp.sessionId || lp.sessionId === clearId || lp.sessionId === state.selectedId) {
              clearLastPromptPending();
            }
          }
        } catch (_) {}
        // Server reported no lives: force remaining working flags idle
        if (opts && opts.forceIdleFlags && state.sessionFlags) {
          for (const k of Object.keys(state.sessionFlags)) {
            if (state.sessionFlags[k] === "working") state.sessionFlags[k] = "idle";
          }
        }
      } else {
        state.turnRunning = true;
        const still = state.liveTurns[state.liveTurns.length - 1];
        state.liveTurnSessionId = still ? still.sessionId : null;
      }
    }
    setComposerEnabled(composerConnected());
    forceComposerUnlocked();
    updateTurnStrip();
    renderSessions();
    // Turn ended: re-parse any assistant tables that finished after last stream chunk.
    if (!running) {
      try {
        const planSid = sessionId || state.selectedId;
        if (planSid && planSid === state.selectedId) {
          refreshSessionPlan(planSid);
        }
      } catch (_) {}
      const idleSid = sessionId || state.selectedId;
      let idleRoot = null;
      if (idleSid && state.sessionViews) {
        const v = state.sessionViews.get(idleSid);
        if (v && v.pane) idleRoot = v.pane;
      }
      if (!idleRoot && (!idleSid || idleSid === state.selectedId)) {
        idleRoot = transcriptRoot();
      }
      if (idleRoot) finalizeAssistantTables(idleRoot);
    }
  }

  async function applySessionSwitch(fromId, toId, reason, message) {
    if (!toId) return;
    const from = fromId || state.selectedId;
    if (state.hubSessionIds.indexOf(toId) < 0) {
      state.hubSessionIds = [toId, ...state.hubSessionIds].slice(0, 50);
    }

    // User is viewing a session they chose: do not steal focus to empty live id.
    // Map prompts to the live hub session while keeping history on screen.
    if (
      state.selectedId &&
      from &&
      from !== toId &&
      state.selectedId === from &&
      reason !== "force_ui_switch"
    ) {
      state.livePromptSessionId = toId;
      setSessionMode("live-remote", { attachSwitched: true });
      subscribeSessionIds(state.selectedId, toId, liveTurnId());
      toast(message || "Live hub session ready — still showing this session’s history", "");
      updateSessionBanner();
      renderSessions();
      return;
    }
    // Already on a different session than the switch source — only record live id.
    if (state.selectedId && from && state.selectedId !== from && state.selectedId !== toId) {
      state.livePromptSessionId = toId;
      subscribeSessionIds(state.selectedId, toId, liveTurnId());
      return;
    }

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
        isWorking: true,
      };
      if (!meta.sessionId) meta.sessionId = toId;
      state.sessions = [meta, ...state.sessions.filter((s) => s.sessionId !== toId)];
    }
    if (from && from !== toId) {
      cacheSessionView(from);
    }
    state.selectedId = toId;
    if (meta && !meta.sessionId) meta.sessionId = toId;
    state.selectedMeta = meta;
    state.livePromptSessionId = toId;
    // Keep prior commands until agent sends a fresh list; builtins fill gaps.
    const v = showSessionPane(toId);
    v.streamBuffers = emptyStreamBuffers();
    state.streamBuffers = v.streamBuffers;
    state.historyLoadedFor = null;
    state.historyFingerprint = null;
    v.historyFingerprint = null;
    v.historyLoaded = false;
    setSessionMode("live-remote", {
      attachSwitched: !!(from && from !== toId),
    });

    setTopbarSessionMeta(meta);
    refreshUsage();
    renderSessions();

    // Fresh stream view for the hub-owned session (system line may arrive via type:system)
    clearTranscript();
    showEmptyMain(false);
    if (message) {
      appendMessage({ role: "system", text: message });
    }
    noteTermLineActivity();

    subscribeSessionIds(toId, { force: true });
    if (from && from !== toId) {
      if (state.subscribedSessions) state.subscribedSessions.delete(from);
      sendWs({ type: "unsubscribe", sessionId: from });
    }
    setComposerEnabled(composerConnected());
    forceComposerUnlocked();
    updateTurnStrip();
  }

  function isRailVisible() {
    if (isMobile()) return els.rail.classList.contains("open");
    return !(els.app || document.getElementById("app")).classList.contains("rail-collapsed");
  }

  function updateMenuButton() {
    if (!els.btnMenu) return;
    if (isMobile()) {
      els.btnMenu.classList.remove("force-show");
    } else {
      els.btnMenu.classList.toggle("force-show", !isRailVisible());
    }
  }

  function syncBrowseSessionsVisibility() {
    $$("#btn-empty-sessions").forEach((btn) => {
      btn.classList.toggle("hidden", isRailVisible());
    });
  }

  function setRailCollapsed(collapsed) {
    const app = els.app || document.getElementById("app");
    if (!app) return;
    app.classList.toggle("rail-collapsed", !!collapsed);
    if (els.rail) {
      els.rail.setAttribute("aria-hidden", collapsed ? "true" : "false");
    }
    try {
      localStorage.setItem("grh.railCollapsed", collapsed ? "1" : "0");
    } catch (_) {}
  }

  function openRail() {
    if (isMobile()) {
      els.rail.classList.add("open");
      els.backdrop.hidden = false;
      if (els.rail) els.rail.setAttribute("aria-hidden", "false");
    } else {
      setRailCollapsed(false);
    }
    syncBrowseSessionsVisibility();
    updateMenuButton();
  }

  function closeRail() {
    if (isMobile()) {
      els.rail.classList.remove("open");
      els.backdrop.hidden = true;
      if (els.rail) els.rail.setAttribute("aria-hidden", "true");
    } else {
      setRailCollapsed(true);
    }
    syncBrowseSessionsVisibility();
    updateMenuButton();
  }

  function loadPins() {
    try {
      const raw = JSON.parse(localStorage.getItem("grh.pinnedSessions") || "[]");
      return Array.isArray(raw) ? raw.filter((x) => typeof x === "string") : [];
    } catch (_) {
      return [];
    }
  }

  function savePins(ids) {
    localStorage.setItem("grh.pinnedSessions", JSON.stringify(ids));
  }

  function isPinned(id) {
    return (state.pinnedSessions || []).includes(id);
  }

  function togglePin(id) {
    const pins = (state.pinnedSessions || []).slice();
    const i = pins.indexOf(id);
    if (i >= 0) pins.splice(i, 1);
    else pins.push(id);
    state.pinnedSessions = pins;
    savePins(pins);
    renderSessions();
  }

  function unpinSession(id) {
    const pins = (state.pinnedSessions || []).slice();
    const i = pins.indexOf(id);
    if (i < 0) return;
    pins.splice(i, 1);
    state.pinnedSessions = pins;
    savePins(pins);
  }

  async function deleteSessionFromList(session) {
    if (!session || !session.sessionId) return;
    const id = session.sessionId;
    const title = session.title || "Untitled session";
    if (
      !window.confirm(
        `Delete session "${title}"? This removes it from disk and cannot be undone.`
      )
    ) {
      return;
    }
    try {
      const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(id)}`), {
        method: "DELETE",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Delete failed", "danger");
        return;
      }
      state.sessions = (state.sessions || []).filter((s) => s.sessionId !== id);
      unpinSession(id);
      const view = state.sessionViews.get(id);
      if (view && view.pane && view.pane.parentNode) {
        view.pane.parentNode.removeChild(view.pane);
      }
      state.sessionViews.delete(id);
      if (state.selectedId === id) {
        state.selectedId = null;
        state.selectedMeta = null;
        state.activePane = null;
        state.historyLoadedFor = null;
        state.historyFingerprint = null;
        setSessionMode("none");
        clearTopbarSessionMeta();
        showEmptyMain(true);
      }
      renderSessions();
      toast("Session deleted", "");
    } catch (err) {
      toast("Delete failed: " + err, "danger");
    }
  }

  function isWorkingSession(s) {
    if (s.isWorking === false) return false;
    if (s.isWorking === true) return true;
    // Old payloads without isWorking: treat as working when not subagent
    return !s.isSubagent;
  }

  function renderSessions() {
    const q = state.filter.trim().toLowerCase();
    const items = state.sessions
      .filter((s) => {
        if (state.sessionKindFilter === "subagent" && !s.isSubagent) return false;
        if (state.sessionKindFilter === "working" && !isWorkingSession(s)) return false;
        // "standard" legacy storage maps to working in init
        if (state.sessionKindFilter === "standard" && !isWorkingSession(s)) return false;
        if (!q) return true;
        return (
          (s.title || "").toLowerCase().includes(q) ||
          (s.cwd || "").toLowerCase().includes(q) ||
          (s.sessionId || "").toLowerCase().startsWith(q) ||
          basename(s.cwd).toLowerCase().includes(q) ||
          (s.agentName || "").toLowerCase().includes(q)
        );
      })
      .slice()
      .sort((a, b) => {
        const ap = isPinned(a.sessionId) ? 1 : 0;
        const bp = isPinned(b.sessionId) ? 1 : 0;
        if (ap !== bp) return bp - ap;
        // Question sessions first (agent waiting), then working, then rest.
        const rank = (s) => {
          const st = sessionLiveStatus(s.sessionId) || s.liveStatus || "idle";
          if (st === "question") return 2;
          if (st === "working") return 1;
          return 0;
        };
        const ar = rank(a);
        const br = rank(b);
        if (ar !== br) return br - ar;
        const at = a.updatedAt || "";
        const bt = b.updatedAt || "";
        if (at === bt) return 0;
        return at < bt ? 1 : -1;
      });

    els.sessionList.innerHTML = "";
    els.sessionEmpty.classList.toggle("hidden", items.length > 0);
    if (items.length === 0) {
      const emptyP = els.sessionEmpty.querySelector("p");
      if (emptyP) {
        if (!state.sessions.length) {
          emptyP.textContent = "No sessions yet. Tap New to start one in a project folder.";
        } else if (q || state.sessionKindFilter !== "all") {
          emptyP.textContent = "No sessions match that filter.";
        } else {
          emptyP.textContent = "No sessions to show.";
        }
      }
    }

    const nextPillRanks = {};
    for (const s of items) {
      const pinned = isPinned(s.sessionId);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "session-row" + (pinned ? " pinned" : "");
      btn.setAttribute("role", "listitem");
      btn.setAttribute("data-session-id", s.sessionId || "");
      btn.title = sessionTooltip(s);
      if (s.sessionId === state.selectedId) btn.classList.add("active");
      if (s.sessionId === state.status.loadedSessionId) btn.classList.add("live");
      const liveStatus = sessionLiveStatus(s.sessionId) || s.liveStatus || "idle";
      nextPillRanks[s.sessionId] = sessionStatusRank(liveStatus);
      const isLiveTurn = liveStatus === "working" || liveStatus === "question";
      if (liveStatus === "working") btn.classList.add("turn-live", "status-working");
      if (liveStatus === "question") btn.classList.add("turn-live", "status-question");

      const bar = document.createElement("span");
      bar.className = "live-bar";
      bar.setAttribute("aria-hidden", "true");

      const body = document.createElement("span");
      body.className = "session-body";

      const titleRow = document.createElement("span");
      titleRow.className = "title-row";
      const title = document.createElement("span");
      title.className = "title";
      title.textContent = s.title || "Untitled session";
      titleRow.appendChild(title);
      if (liveStatus === "working") {
        const pill = document.createElement("span");
        pill.className = "session-pill status-working";
        pill.textContent = "Working";
        titleRow.appendChild(pill);
      } else if (liveStatus === "question") {
        const pill = document.createElement("span");
        pill.className = "session-pill status-question";
        pill.textContent = "Needs reply";
        titleRow.appendChild(pill);
      }
      if (s.isSubagent) {
        const pill = document.createElement("span");
        pill.className = "session-pill subagent";
        pill.textContent = "subagent";
        titleRow.appendChild(pill);
      } else if (!s.isCli) {
        // Hub-owned: hub_origin user|attach or currently hub remote
        const pill = document.createElement("span");
        pill.className = "session-pill hub";
        pill.textContent = "Hub";
        titleRow.appendChild(pill);
      } else {
        // CLI-origin sessions get a source pill (preferred over noise)
        const pill = document.createElement("span");
        pill.className = "session-pill cli";
        pill.textContent = "CLI";
        titleRow.appendChild(pill);
      }

      const meta = document.createElement("span");
      meta.className = "meta";
      const proj = document.createElement("span");
      proj.textContent = basename(s.cwd) || "project";
      meta.appendChild(proj);
      if (s.agentName) {
        const agent = document.createElement("span");
        agent.textContent = s.agentName;
        meta.appendChild(agent);
      }
      const hint = sessionListProgressHint({
        is_live_turn: isLiveTurn,
        tool: toolTitleForSession(s.sessionId),
      });
      if (hint) {
        const turnHint = document.createElement("span");
        turnHint.className = "turn-hint";
        turnHint.textContent = hint;
        meta.appendChild(turnHint);
      }
      const time = document.createElement("span");
      time.textContent = relativeTime(s.updatedAt);
      meta.appendChild(time);

      body.append(titleRow, meta);

      const actions = document.createElement("span");
      actions.className = "session-actions";

      const renameBtn = document.createElement("span");
      renameBtn.className = "session-rename-btn";
      renameBtn.setAttribute("role", "button");
      renameBtn.tabIndex = 0;
      renameBtn.setAttribute("aria-label", "Rename session");
      renameBtn.title = "Rename";
      renameBtn.textContent = "✎";
      renameBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        startRenameSession(s.sessionId, s.title || "Untitled session", title);
      });
      renameBtn.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          startRenameSession(s.sessionId, s.title || "Untitled session", title);
        }
      });

      const pin = document.createElement("span");
      pin.className = "session-pin";
      pin.setAttribute("role", "button");
      pin.tabIndex = 0;
      pin.setAttribute("aria-label", pinned ? "Unpin session" : "Pin session");
      pin.title = pinned ? "Unpin from top" : "Pin to top";
      pin.textContent = pinned ? "★" : "☆";
      pin.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        togglePin(s.sessionId);
      });
      pin.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          togglePin(s.sessionId);
        }
      });

      const delBtn = document.createElement("span");
      delBtn.className = "session-delete-btn";
      delBtn.setAttribute("role", "button");
      delBtn.tabIndex = 0;
      delBtn.setAttribute("aria-label", "Delete session");
      delBtn.title = "Delete";
      delBtn.textContent = "🗑";
      delBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        deleteSessionFromList(s);
      });
      delBtn.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          deleteSessionFromList(s);
        }
      });

      actions.append(renameBtn, pin, delBtn);
      btn.append(bar, body, actions);
      btn.addEventListener("click", (e) => {
        if (btn.classList.contains("renaming")) {
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        if (e.target && e.target.closest && e.target.closest(".session-rename-input")) {
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        openSession(s);
      });
      els.sessionList.appendChild(btn);
    }
    _sessionPillRanks = nextPillRanks;
  }

  function emptyStreamBuffers() {
    return {
      assistantEl: null,
      thoughtEl: null,
      thoughtOpen: false,
      planEl: null,
      activityEl: null,
      tools: new Map(),
      /** @type {Map<string, HTMLElement>} terminalId → live terminal tool row */
      terminals: new Map(),
      lastToolTitle: "",
      activeUserEl: null,
    };
  }

  const TERM_OUT_MAX_CHARS = 200000;

  /**
   * Live hub-hosted terminal/* pump delta → CLI-like tool row.
   * @param {{ terminalId?: string, delta?: string, sessionId?: string|null }} msg
   */
  function onTerminalOut(msg) {
    const terminalId = String((msg && msg.terminalId) || "").trim();
    const delta = msg && msg.delta != null ? String(msg.delta) : "";
    if (!terminalId || !delta) return;
    const sessionId =
      (msg.sessionId && String(msg.sessionId)) ||
      liveTurnId() ||
      (state.status && state.status.turnSessionId) ||
      state.selectedId ||
      null;

    const apply = () => {
      if (!state.streamBuffers) state.streamBuffers = emptyStreamBuffers();
      if (!state.streamBuffers.terminals) {
        state.streamBuffers.terminals = new Map();
      }
      const toolKey = `term:${terminalId}`;
      let row =
        state.streamBuffers.terminals.get(terminalId) ||
        state.streamBuffers.tools.get(toolKey);
      if (!row || !row.isConnected) {
        row = appendToolLine(
          {
            label: "terminal",
            status: "running",
            toolCallId: toolKey,
            summary: "",
          },
          ""
        );
        if (!row) return;
        row.dataset.terminal = "1";
        row.dataset.terminalId = terminalId;
        row.open = true;
        state.streamBuffers.terminals.set(terminalId, row);
        if (toolKey) state.streamBuffers.tools.set(toolKey, row);
        state.streamBuffers.lastToolTitle = "terminal";
      }
      const detail =
        row.querySelector(".tool-detail") || row.querySelector(".term-body");
      if (!detail) return;
      let raw = detail._rawText != null ? String(detail._rawText) : detail.textContent || "";
      raw += delta;
      if (raw.length > TERM_OUT_MAX_CHARS) {
        raw = raw.slice(raw.length - TERM_OUT_MAX_CHARS);
      }
      detail._rawText = raw;
      detail.textContent = raw;
      detail.hidden = false;
      row.dataset.hasDetail = "1";
      // Keep open while streaming (CLI-like live output).
      if (!row.open) row.open = true;
      noteTermLineActivity();
      scheduleTurnStrip();
      scrollIfSticky();
    };

    if (sessionId && sessionId !== state.selectedId) {
      // Still apply when a session pane exists or is live (subscribe fanout).
      if (
        state.sessionViews.has(sessionId) ||
        sessionId === liveTurnId() ||
        shouldApplyAcpToSession(sessionId)
      ) {
        markSessionActivity(sessionId, "working");
        getSessionPane(sessionId);
        withSessionTarget(sessionId, apply);
      }
      return;
    }
    if (sessionId) markSessionActivity(sessionId, "working");
    apply();
  }

  function transcriptRoot() {
    return state.activePane || els.transcript;
  }

  function getSessionPane(sessionId) {
    let v = state.sessionViews.get(sessionId);
    if (!v) {
      const pane = document.createElement("div");
      pane.className = "session-pane";
      pane.dataset.sessionId = sessionId;
      v = {
        pane,
        stickToBottom: true,
        historyFingerprint: null,
        historyLoaded: false,
        streamBuffers: emptyStreamBuffers(),
        lastToolTitle: "",
      };
      state.sessionViews.set(sessionId, v);
    }
    return v;
  }

  function rebuildToolsFromPane(v) {
    if (!v || !v.pane || !v.streamBuffers) return;
    const tools = new Map();
    const terminals = new Map();
    const rows = v.pane.querySelectorAll("[data-tool-call-id]");
    for (const row of rows) {
      const id = row.getAttribute("data-tool-call-id");
      if (id) tools.set(id, row);
      const tid = row.getAttribute("data-terminal-id");
      if (tid) terminals.set(tid, row);
    }
    v.streamBuffers.tools = tools;
    v.streamBuffers.terminals = terminals;
    // Recover live stream refs when possible
    const lastAssistant = v.pane.querySelector(".term-line.assistant:last-of-type");
    if (lastAssistant) v.streamBuffers.assistantEl = lastAssistant;
    const lastThought = v.pane.querySelector("details.term-line.thought:last-of-type");
    if (lastThought) v.streamBuffers.thoughtEl = lastThought;
    const lastPlan = v.pane.querySelector("details.term-line.plan:last-of-type");
    if (lastPlan) v.streamBuffers.planEl = lastPlan;
  }

  function cacheSessionView(sessionId) {
    if (!sessionId) return;
    const v = getSessionPane(sessionId);
    if (state.activePane === v.pane || state.selectedId === sessionId) {
      v.stickToBottom = !!state.stickToBottom;
      v.historyFingerprint = state.historyFingerprint;
      v.historyLoaded = state.historyLoadedFor === sessionId;
      if (state.streamBuffers) {
        v.streamBuffers = state.streamBuffers;
        v.lastToolTitle = state.streamBuffers.lastToolTitle || "";
      }
    } else if (v.streamBuffers) {
      v.lastToolTitle = v.streamBuffers.lastToolTitle || v.lastToolTitle || "";
    }
  }

  function showSessionPane(sessionId) {
    const wrap = els.transcript;
    if (!wrap) return getSessionPane(sessionId);
    const em = $("#empty-main", wrap);
    if (em) em.hidden = true;
    for (const child of [...wrap.querySelectorAll(".session-pane")]) {
      child.hidden = child.dataset.sessionId !== sessionId;
    }
    const v = getSessionPane(sessionId);
    if (!v.pane.parentNode) wrap.appendChild(v.pane);
    v.pane.hidden = false;
    rebuildToolsFromPane(v);
    state.streamBuffers = v.streamBuffers;
    state.activePane = v.pane;
    state.stickToBottom = v.stickToBottom !== false;
    // Pane swap changes layout width; pin X so the view does not lurch sideways.
    wrap.scrollLeft = 0;
    clampHorizontalScroll();
    return v;
  }

  function withSessionTarget(sessionId, fn) {
    if (!sessionId) return fn(null);
    if (sessionId === state.selectedId && state.activePane) {
      return fn(getSessionPane(sessionId));
    }
    const v = getSessionPane(sessionId);
    if (!v.pane.parentNode && els.transcript) {
      els.transcript.appendChild(v.pane);
      v.pane.hidden = sessionId !== state.selectedId;
    }
    const prevPane = state.activePane;
    const prevBuf = state.streamBuffers;
    const prevStick = state.stickToBottom;
    state.activePane = v.pane;
    state.streamBuffers = v.streamBuffers;
    // Never scroll the visible transcript while writing an offscreen pane.
    state.stickToBottom = false;
    try {
      return fn(v);
    } finally {
      v.lastToolTitle =
        (v.streamBuffers && v.streamBuffers.lastToolTitle) || v.lastToolTitle || "";
      state.activePane = prevPane;
      state.streamBuffers = prevBuf;
      state.stickToBottom = prevStick;
    }
  }

  function clearTranscript() {
    const root = transcriptRoot();
    if (root && root.classList && root.classList.contains("session-pane")) {
      root.innerHTML = "";
    } else if (els.transcript) {
      // Preserve session panes; clear only non-pane nodes.
      for (const child of [...els.transcript.childNodes]) {
        if (child.nodeType === 1 && child.classList && child.classList.contains("session-pane")) {
          continue;
        }
        child.remove();
      }
    }
    if (state.selectedId) {
      const v = getSessionPane(state.selectedId);
      v.streamBuffers = emptyStreamBuffers();
      state.streamBuffers = v.streamBuffers;
      v.historyFingerprint = null;
      v.historyLoaded = false;
    } else {
      state.streamBuffers = emptyStreamBuffers();
    }
  }

  function showEmptyMain(show) {
    if (show) {
      // Hide panes; show empty card in transcript shell
      if (els.transcript) {
        for (const pane of els.transcript.querySelectorAll(".session-pane")) {
          pane.hidden = true;
        }
      }
      state.activePane = null;
      let wrap = $("#empty-main", els.transcript);
      if (!wrap) {
        wrap = document.createElement("div");
        wrap.id = "empty-main";
        wrap.className = "empty-main";
        wrap.innerHTML = `
          <div class="empty-card">
            <h2>No session selected</h2>
            <p class="empty-sub">Pick a chat from the sidebar, or start a new one.</p>
            <p>Your project sessions appear under Working. Subagent runs are under Subagent.</p>
            <div class="empty-actions">
              <button type="button" id="btn-empty-sessions" class="btn btn-ghost">Browse sessions</button>
              <button type="button" id="btn-empty-new" class="btn btn-accent">New session</button>
            </div>
          </div>`;
        els.transcript.appendChild(wrap);
        $("#btn-empty-sessions", wrap).addEventListener("click", openRail);
        $("#btn-empty-new", wrap).addEventListener("click", openNewModal);
      }
      wrap.hidden = false;
      setSessionMode("none");
      syncBrowseSessionsVisibility();
    } else {
      const em = $("#empty-main", els.transcript);
      if (em) em.hidden = true;
    }
  }

  function distanceFromBottom() {
    const el = els.transcript;
    return el.scrollHeight - el.scrollTop - el.clientHeight;
  }

  /** Kill horizontal scroll jump when swapping session panes / focusing composer. */
  function clampHorizontalScroll() {
    const nodes = [
      els.transcript,
      els.sessionList,
      document.scrollingElement,
      document.documentElement,
      document.body,
      $("#app"),
      $(".main"),
      $(".chat-panel"),
      $(".transcript-wrap"),
    ];
    for (const el of nodes) {
      if (!el) continue;
      try {
        if (el.scrollLeft) el.scrollLeft = 0;
      } catch (_) {
        /* ignore */
      }
    }
    try {
      if (window.scrollX) window.scrollTo(0, window.scrollY || 0);
    } catch (_) {
      /* ignore */
    }
  }

  let _scrollRaf = 0;
  function scrollTranscriptToBottom() {
    if (_scrollRaf) return;
    _scrollRaf = requestAnimationFrame(() => {
      _scrollRaf = 0;
      const el = els.transcript;
      if (!el) return;
      // Hold _ignoreScroll across the paint so layout/scroll events do not
      // flip stickToBottom mid-frame (composer grow / history batch).
      state._ignoreScroll = true;
      el.scrollLeft = 0;
      el.scrollTop = el.scrollHeight;
      requestAnimationFrame(() => {
        el.scrollLeft = 0;
        el.scrollTop = el.scrollHeight;
        clampHorizontalScroll();
        state._ignoreScroll = false;
        updateJumpLatestUiOnly();
        scheduleStickyUserFromScroll();
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
    // Sticky re-sync runs after scroll settles (scrollTranscriptToBottom rAF).
    scheduleStickyUserFromScroll();
  }

  function beginHistoryBatch() {
    state._historyBatchDepth = (state._historyBatchDepth || 0) + 1;
    state._suppressStickyScroll = true;
  }

  function endHistoryBatch({ jump } = {}) {
    state._historyBatchDepth = Math.max(0, (state._historyBatchDepth || 1) - 1);
    if (state._historyBatchDepth === 0) {
      // Keep suppress during reconnect freeze until resumeAfterReconnect finishes.
      if (!state._reconnectScrollFreeze) {
        state._suppressStickyScroll = false;
      }
      // Cheap: ensure GFM tables in loaded history are rendered (uses _rawText).
      finalizeAssistantTables(transcriptRoot());
      updateTurnStrip();
      if (jump && state.stickToBottom && !state._reconnectScrollFreeze) jumpToLatest();
      else {
        updateJumpLatestUiOnly();
        scheduleStickyUserFromScroll();
      }
    }
  }

  function scrollIfSticky(force) {
    // Reconnect resume freezes intermediate sticky scrolls (final jump is intentional).
    if (state._reconnectScrollFreeze && !force) return;
    // Batch history rebuilds suppress intermediate scrolls.
    if ((state._suppressStickyScroll || state._historyBatchDepth > 0) && !force) return;
    // Offscreen session panes must not move the visible scroll position.
    if (state.activePane && state.activePane.hidden) return;
    if (!shouldScrollToBottom(state.stickToBottom, !!force)) {
      updateJumpLatestUiOnly();
      return;
    }
    scrollTranscriptToBottom();
  }

  /** Clear sticky active-prompt markers from a transcript root (or selected pane). */
  function clearActiveUserPrompt(root) {
    const r = root || transcriptRoot();
    if (!r) return;
    r.querySelectorAll(".term-line.user.active-prompt").forEach((el) => {
      el.classList.remove("active-prompt");
    });
    if (state.streamBuffers) state.streamBuffers.activeUserEl = null;
    if (state.sessionViews) {
      for (const v of state.sessionViews.values()) {
        if (v && v.pane === r && v.streamBuffers) v.streamBuffers.activeUserEl = null;
      }
    }
  }

  /**
   * Pin the active user "You:" line (CLI turn focus).
   * CSS sticky freezes it at the top while stickToBottom keeps the stream in view.
   */
  function activateUserPrompt(el, { scrollToTop = true } = {}) {
    if (!el) return;
    clearActiveUserPrompt(transcriptRoot());
    el.classList.add("active-prompt");
    if (state.streamBuffers) state.streamBuffers.activeUserEl = el;
    if (scrollToTop) scrollActivePromptToTop(el);
  }

  /** Scroll so the active prompt sits near the top of #transcript (nested pane-safe). */
  function scrollActivePromptToTop(el) {
    const scroller = els.transcript;
    if (!scroller || !el) return;
    // Offscreen panes must not move the visible scroller.
    if (state.activePane && state.activePane.hidden) return;
    const setTop = () => {
      const elRect = el.getBoundingClientRect();
      const scRect = scroller.getBoundingClientRect();
      const top = scroller.scrollTop + (elRect.top - scRect.top) - 8;
      scroller.scrollTop = Math.max(0, top);
    };
    state._ignoreScroll = true;
    setTop();
    requestAnimationFrame(() => {
      setTop();
      state._ignoreScroll = false;
      updateJumpLatestUiOnly();
    });
  }

  /**
   * Pick sticky user prompt index from content tops (ascending).
   * Returns last index where tops[i] <= anchorY, or -1 if none.
   */
  function pickUserPromptIndex(tops, anchorY) {
    let idx = -1;
    for (let i = 0; i < tops.length; i++) {
      if (tops[i] <= anchorY) idx = i;
    }
    return idx;
  }

  /**
   * Pin sticky active-prompt to the user line for the section under review
   * (last user line at or above top anchor). Live stick-to-bottom pins latest.
   */
  function syncStickyUserFromScroll() {
    if (state._ignoreScroll || state._resumeAfterReconnect) return;
    const scroller = els.transcript;
    const root = transcriptRoot();
    if (!scroller || !root) return;
    // Live at bottom: pin latest user for this pane
    if (state.stickToBottom && turnRunningOnSelected()) {
      const users = root.querySelectorAll(".term-line.user");
      const last = users[users.length - 1];
      if (last && !last.classList.contains("active-prompt")) {
        activateUserPrompt(last, { scrollToTop: false });
      }
      return;
    }
    const users = Array.from(root.querySelectorAll(".term-line.user"));
    if (!users.length) {
      clearActiveUserPrompt(root);
      return;
    }
    const scRect = scroller.getBoundingClientRect();
    // top inset under sticky zone
    const anchorY = scroller.scrollTop + 56;
    // Compute each user's offset relative to scroller content
    const tops = users.map((el) => {
      const elRect = el.getBoundingClientRect();
      return scroller.scrollTop + (elRect.top - scRect.top);
    });
    let idx = pickUserPromptIndex(tops, anchorY);
    if (idx < 0) idx = 0; // pin first when above all
    const target = users[idx];
    if (!target.classList.contains("active-prompt")) {
      activateUserPrompt(target, { scrollToTop: false });
    }
  }

  let _stickyScrollRaf = 0;
  function scheduleStickyUserFromScroll() {
    if (_stickyScrollRaf) return;
    _stickyScrollRaf = requestAnimationFrame(() => {
      _stickyScrollRaf = 0;
      syncStickyUserFromScroll();
    });
  }

  function setTermBodyContent(bodyEl, text) {
    const raw = text == null ? "" : String(text);
    // Always keep markdown source; after a table render, textContent has no pipes.
    bodyEl._rawText = raw;
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

  /**
   * Re-parse assistant bodies that still have table markdown in _rawText.
   * Mid-stream may miss a complete table; never re-parse from textContent (no pipes).
   */
  function finalizeAssistantTables(root) {
    const r = root || transcriptRoot();
    if (!r) return;
    r.querySelectorAll(".term-line.assistant .term-body").forEach((body) => {
      const raw = body._rawText != null ? String(body._rawText) : "";
      if (!raw.includes("|")) return;
      // Incomplete mid-stream then complete: re-run even if a partial .term-table exists.
      if (!body.querySelector(".term-table") || raw.includes("|")) {
        setTermBodyContent(body, raw);
      }
    });
  }

  function planHasActiveWork(entries) {
    return (entries || []).some((e) => {
      const st = normalizeStatus(e && e.status);
      return st === "running" || st === "pending" || st === "in_progress";
    });
  }

  function planHasRunning(entries) {
    return (entries || []).some((e) => normalizeStatus(e && e.status) === "running");
  }

  function renderPlanBody(body, entries) {
    body.innerHTML = "";
    const ul = document.createElement("ul");
    ul.className = "plan-list";
    let activeLi = null;
    for (const e of entries || []) {
      const li = document.createElement("li");
      li.className = "plan-item";
      const st = normalizeStatus(e.status);
      li.dataset.status = st;
      if (st === "running") {
        li.classList.add("active", "current");
        activeLi = li;
      }
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
    if (activeLi) {
      const planEl = body.closest("details.term-line.plan");
      if (planEl && planEl.open) {
        try {
          // block only — inline scroll shifts the whole transcript horizontally
          activeLi.scrollIntoView({ block: "nearest", inline: "start" });
          clampHorizontalScroll();
        } catch (_) {
          /* ignore */
        }
      }
    }
  }

  function appendPlanMessage(msg) {
    const entries = (msg.meta && msg.meta.entries) || [];
    const details = document.createElement("details");
    details.className = "term-line plan";
    details.open = planHasActiveWork(entries);
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
    transcriptRoot().appendChild(details);
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
      // Auto-open while work is in progress; leave open as-is when all done.
      if (planHasActiveWork(entries)) el.open = true;
    }
    scrollIfSticky();
    return el;
  }

  function toolOneLinerRedundant(nameText, summary) {
    const name = String(nameText || "").trim();
    const snip = String(summary || "").trim();
    if (!snip) return true;
    if (!name) return false;
    // Hide when equal or already baked into the name (avoids double path/title).
    if (snip === name) return true;
    if (name.includes(snip)) return true;
    return false;
  }

  function setToolDetailBody(row, text) {
    const detail = row.querySelector(".tool-detail") || row.querySelector(".term-body");
    if (!detail) return;
    const full = String(text || "").trim();
    if (full) {
      detail.textContent = full;
      detail.hidden = false;
      row.dataset.hasDetail = "1";
    } else {
      detail.textContent = "";
      detail.hidden = true;
      delete row.dataset.hasDetail;
    }
  }

  function createToolLine({ title, status, summary, toolCallId }) {
    const row = document.createElement("details");
    row.className = "term-line tool";
    const st = normalizeStatus(status || "pending");
    // Always start collapsed; user expands manually.
    const snip = (summary || "").trim();
    row.open = false;
    row.dataset.status = st;
    if (st === "running" || st === "pending") row.classList.add("running");
    if (toolCallId) row.dataset.toolCallId = toolCallId;
    // Heartbeat waiting elapsed: time since row opened (client-only cue).
    if (!row.dataset.openedAt) row.dataset.openedAt = String(Date.now());

    const sum = document.createElement("summary");
    const prefix = document.createElement("span");
    prefix.className = "term-prefix";
    prefix.textContent = formatTermPrefix("tool");

    const displayTitle = (title || "tool").trim() || "tool";
    const name = document.createElement("span");
    name.className = "tool-name";
    name.textContent = displayTitle;

    const pill = document.createElement("span");
    pill.className = `term-status ${statusClass(st)}`;
    pill.textContent = st;

    const oneLiner = document.createElement("span");
    oneLiner.className = "tool-one-liner muted";
    const hideOne = toolOneLinerRedundant(displayTitle, snip);
    oneLiner.textContent = snip && !hideOne ? truncate(snip, 80) : "";
    oneLiner.hidden = hideOne;

    sum.append(prefix, name, pill, oneLiner);

    const detail = document.createElement("div");
    detail.className = "term-body tool-detail";
    row.append(sum, detail);
    setToolDetailBody(row, snip);
    row._toolTitle = displayTitle;
    row._toolSummary = snip;
    refreshToolPreviewAction(row);
    return row;
  }

  function updateToolLine(row, { title, status, summary }) {
    if (!row) return;
    let st = row.dataset.status || "pending";
    if (status != null) {
      st = normalizeStatus(status);
      row.dataset.status = st;
      row.classList.toggle("running", st === "running" || st === "pending");
      const pill = row.querySelector(".term-status");
      if (pill) {
        pill.textContent = st;
        pill.className = `term-status ${statusClass(st)}`;
      }
      // Status-only / re-open: ensure opened-at for waiting heartbeat.
      if (
        (st === "running" || st === "pending" || st === "in_progress") &&
        !row.dataset.openedAt
      ) {
        row.dataset.openedAt = String(Date.now());
      }
    }
    if (title) {
      const name = row.querySelector(".tool-name");
      if (name) name.textContent = title;
      row._toolTitle = title;
    }
    if (summary != null) {
      const full = String(summary).trim();
      // Empty summary on status-only update: keep prior detail/one-liner.
      if (full) {
        row._toolSummary = full;
        const nameText = String(row._toolTitle || "").trim();
        const oneLiner = row.querySelector(".tool-one-liner");
        if (oneLiner) {
          const hideOne = toolOneLinerRedundant(nameText, full);
          oneLiner.textContent = !hideOne ? truncate(full, 80) : "";
          oneLiner.hidden = hideOne;
        }
        setToolDetailBody(row, full);
      }
    } else if (title) {
      // Title-only update: re-evaluate one-liner redundancy against stored summary.
      const full = String(row._toolSummary || "").trim();
      const oneLiner = row.querySelector(".tool-one-liner");
      if (oneLiner) {
        const hideOne = toolOneLinerRedundant(title, full);
        oneLiner.textContent = full && !hideOne ? truncate(full, 80) : "";
        oneLiner.hidden = hideOne;
      }
    }
    // Never auto-open on update; user expands manually.
    refreshToolPreviewAction(row);
  }

  function appendToolLine(meta, text) {
    if (!shouldShowToolLine()) return null;
    noteTermLineActivity();
    // Prefer short label for the name; path/command lives in the one-liner.
    const label = String(meta.label || "").trim();
    const textStr = String(text || "").trim();
    const title = label || textStr || "tool";
    let summary = String(meta.summary || meta.detail || "").trim();
    if (!summary && textStr && textStr !== title) {
      summary = textStr;
    }
    const st = meta.status || "pending";
    const id = meta.toolCallId || "";

    // Update existing by id if present
    if (id && state.streamBuffers.tools.has(id)) {
      const existing = state.streamBuffers.tools.get(id);
      if (existing && existing.isConnected) {
        updateToolLine(existing, { title, status: st, summary });
        state.streamBuffers.lastToolTitle = title;
        if (!(state._historyBatchDepth > 0)) {
          scheduleTurnStrip();
          scrollIfSticky();
        }
        return existing;
      }
    }

    const row = createToolLine({
      title,
      status: st,
      summary,
      toolCallId: id,
    });
    transcriptRoot().appendChild(row);
    if (id) state.streamBuffers.tools.set(id, row);
    state.streamBuffers.lastToolTitle = title;
    if (!(state._historyBatchDepth > 0)) {
      scheduleTurnStrip();
      scrollIfSticky();
    }
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
      // Always open by default (CLI-like readable transcript); allow explicit close.
      details.open = opts.open !== false;
      const summary = document.createElement("summary");
      const prefix = document.createElement("span");
      prefix.className = "term-prefix";
      prefix.textContent = formatTermPrefix("thought");
      const label = document.createElement("span");
      label.className = "thought-summary-label";
      label.textContent = opts.stream ? "Thinking…" : "Thinking";
      summary.append(prefix, label);
      const body = document.createElement("div");
      body.className = "term-body";
      body.textContent = text;
      body._rawText = text;
      details.append(summary, body);
      transcriptRoot().appendChild(details);
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
    transcriptRoot().appendChild(div);
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
    // Live stream owns the transcript while a turn is running on this session
    if (state.turnRunning && !opts.force && turnRunningOnSelected()) return false;
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
    beginHistoryBatch();
    try {
      renderHistory(list, { skipScroll: true });
      state.historyLoadedFor = sessionId;
      state.historyFingerprint = fp;
      if (sessionId) {
        const v = getSessionPane(sessionId);
        v.historyFingerprint = fp;
        v.historyLoaded = true;
      }
    } finally {
      // During reconnect freeze, resumeAfterReconnect does a single final jump.
      const shouldJump =
        !state._reconnectScrollFreeze && (wasNearBottom || !!opts.jump);
      endHistoryBatch({ jump: shouldJump });
      scheduleStickyUserFromScroll();
    }
    return true;
  }

  function renderHistory(messages, opts = {}) {
    // Suppress per-line scroll/turn-strip during rebuild; one pass after batch.
    beginHistoryBatch();
    try {
      clearTranscript();
      showEmptyMain(false);
      if (state.selectedId) showSessionPane(state.selectedId);
      if (!messages || !messages.length) {
        appendMessage({ role: "system", text: "No prior transcript on disk for this session." });
      } else {
        for (const m of messages) {
          appendMessage(m);
        }
      }
      state.streamBuffers.assistantEl = null;
      state.streamBuffers.thoughtEl = null;
      state.streamBuffers.planEl = null;
      state.streamBuffers.activityEl = null;
      state.streamBuffers.tools = new Map();
      state.streamBuffers.terminals = new Map();
      state.streamBuffers.lastToolTitle = "";
      // Do not force stick-to-bottom during reconnect if user scrolled up.
      if (!state._reconnectScrollFreeze) {
        state.stickToBottom = true;
      } else if (state.stickToBottom !== false) {
        // Keep existing stick preference (only default true when already true).
      }
    } finally {
      endHistoryBatch({
        jump: !opts.skipScroll && !state._reconnectScrollFreeze,
      });
    }
  }

  function extractText(content) {
    if (content == null || content === false) return "";
    if (typeof content === "string") return content;
    if (typeof content === "number" || typeof content === "boolean") return String(content);
    if (Array.isArray(content)) {
      return content.map(extractText).join("");
    }
    if (typeof content === "object") {
      if (content.text != null && content.text !== "") return String(content.text);
      if (content.content != null) return extractText(content.content);
      // Nested ACP: {type:"content", content:{type:"text", text:"..."}} already handled above.
      // Fall through for plain {type, ...} with no extractable payload.
    }
    return "";
  }

  function beginNewUserTurn() {
    clearActiveUserPrompt();
    state.streamBuffers.assistantEl = null;
    state.streamBuffers.thoughtEl = null;
    state.streamBuffers.planEl = null;
    state.streamBuffers.activityEl = null;
    state.streamBuffers.tools = new Map();
    state.streamBuffers.terminals = new Map();
    state.streamBuffers.lastToolTitle = "";
    state.streamBuffers.activeUserEl = null;
    updateTurnStrip();
  }

  // Merge live stream chunks: cumulative snapshots replace, pure deltas append.
  // Mirrors hub/history.py _merge_messages text rules.
  function mergeStreamText(prev, chunk) {
    const p = prev == null ? "" : String(prev);
    const c = chunk == null ? "" : String(chunk);
    if (!c) return p;
    if (!p) return c;
    if (p === c || p.startsWith(c)) return p;
    if (c.startsWith(p)) return c;
    return p + c;
  }

  function appendToBody(el, text) {
    if (!el) return;
    noteTermLineActivity();
    const body = el.querySelector(".term-body");
    if (!body) return;
    // Always accumulate from _rawText (source of truth). After a table render,
    // body.textContent is cell text without pipes — never re-parse from it.
    const prev = body._rawText != null ? String(body._rawText) : String(body.textContent || "");
    const next = mergeStreamText(prev, text);
    body._rawText = next;
    if (next.includes("|") && next.includes("\n")) {
      setTermBodyContent(body, next);
    } else {
      body.textContent = next;
    }
  }

  function shouldApplyAcpToSession(sessionId) {
    if (!sessionId) return !!state.selectedId;
    if (sessionId === state.selectedId) return true;
    const live = liveTurnId();
    if (sessionId === live) return true;
    if (state.sessionViews.has(sessionId)) return true;
    if (state.turnRunning && sessionId === state.status.loadedSessionId) return true;
    return false;
  }

  function processAcpSessionUpdate(kind, update) {
    if (kind === "agent_message_chunk") {
      // Finalize thinking so the reply starts clean after a thought phase.
      markThoughtComplete(state.streamBuffers);
      state.streamBuffers.thoughtEl = null;
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
        const body0 = el.querySelector(".term-body");
        if (body0) body0._rawText = "";
      } else if (!el.open) {
        // Force re-open if user closed while still streaming.
        el.open = true;
        state.streamBuffers.thoughtOpen = true;
      }
      const body = el.querySelector(".term-body");
      if (body) {
        const prev = body._rawText || body.textContent || "";
        body._rawText = mergeStreamText(prev, text);
        body.textContent = body._rawText;
      }
      const label = el.querySelector(".thought-summary-label");
      if (label) label.textContent = "Thinking…";
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
      // New tool ends the current thought "screen"; next thought is a new block.
      markThoughtComplete(state.streamBuffers);
      state.streamBuffers.thoughtEl = null;
      const id = update.toolCallId || "";
      const label = toolLabelFromUpdate(update);
      const summary = toolSummaryFromUpdate(update);
      // Name = short label only; path/command goes in one-liner (not label+summary title).
      const status = update.status != null ? normalizeStatus(update.status) : "pending";
      const row = appendToolLine(
        {
          toolCallId: id,
          status,
          summary,
          detail: summary,
          label,
        },
        label
      );
      if (id && row) state.streamBuffers.tools.set(id, row);
      state.streamBuffers.assistantEl = null;
      state.streamBuffers.lastToolTitle = label;
      scheduleTurnStrip();
      return;
    }

    if (kind === "tool_call_update") {
      const id = update.toolCallId || "";
      const status = normalizeStatus(update.status);
      // Prefer short tool label for the name; keep full stream text in detail body.
      const label = toolLabelFromUpdate(update);
      const detailText =
        extractToolContentSnippet(update, 8000) || toolSummaryFromUpdate(update);
      // Status-only updates still count as activity and must repaint strip/status.
      noteTermLineActivity();
      let row = id ? state.streamBuffers.tools.get(id) : null;
      if (!row || !row.isConnected) {
        row = appendToolLine(
          {
            toolCallId: id,
            status,
            summary: detailText,
            detail: detailText,
            label,
          },
          label
        );
        if (id && row) state.streamBuffers.tools.set(id, row);
      } else {
        // Always patch status; only push title/summary when non-empty (status-only safe).
        const patch = { status };
        if (label) patch.title = label;
        if (detailText) patch.summary = detailText;
        updateToolLine(row, patch);
      }
      if (label) state.streamBuffers.lastToolTitle = label;
      scheduleTurnStrip();
      scrollIfSticky();
      return;
    }

    if (kind === "subagent_spawned" || kind === "subagent_finished") {
      const sid =
        update.sessionId ||
        update.agentId ||
        update.subagentId ||
        update.agentSessionId ||
        "";
      const label = kind.replace(/_/g, " ");
      appendMessage({
        role: "system",
        text: sid ? `${label} (${sid})` : label,
      });
      return;
    }
  }

  function processUserMessageChunk(update) {
    const text = extractText(update.content);
    if (!text) return;
    const root = transcriptRoot();
    const last = root && root.lastElementChild;
    if (last && last.classList.contains("user") && last.classList.contains("term-line")) {
      const body = last.querySelector(".term-body");
      if (!body) return;
      const existing = body._rawText != null ? String(body._rawText) : String(body.textContent || "");
      // Exact duplicate (hub echo + ACP full message)
      if (existing === text) {
        // Keep the in-flight turn's You: line marked while running
        if (state.turnRunning) activateUserPrompt(last, { scrollToTop: false });
        scrollIfSticky();
        return;
      }
      // Already have this text as prefix (ACP re-sends shorter/same)
      if (existing.startsWith(text)) {
        if (state.turnRunning) activateUserPrompt(last, { scrollToTop: false });
        scrollIfSticky();
        return;
      }
      // Replacement with longer full message that extends existing stream
      if (text.startsWith(existing)) {
        setTermBodyContent(body, text);
        body._rawText = text;
        if (state.turnRunning) activateUserPrompt(last, { scrollToTop: false });
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
      // New user line for the live turn: pin as active prompt
      activateUserPrompt(el, { scrollToTop: true });
    }
    scrollIfSticky();
  }

  function handleAcpMessage(sessionId, message) {
    const method = message.method || "";
    if (method !== "session/update" && method !== "_x.ai/session/update") {
      return;
    }
    const update = (message.params && message.params.update) || {};
    const kind = update.sessionUpdate || "";

    // Agent command lists are session-global cache; apply even if selectedId lags
    // (view id vs live id during attach) or message is for another session.
    if (kind === "available_commands_update") {
      const cmds = update.availableCommands || update.available_commands || [];
      applyCommands(Array.isArray(cmds) ? cmds : []);
      return;
    }

    const targetId = sessionId || state.selectedId;
    if (!targetId) return;

    // Stream activity → Working pill immediately (including offscreen sessions).
    // markSessionActivity never overwrites question with working.
    if (
      kind === "user_message_chunk" ||
      kind === "agent_message_chunk" ||
      kind === "agent_thought_chunk" ||
      kind === "plan" ||
      kind === "tool_call" ||
      kind === "tool_call_update" ||
      kind
    ) {
      markSessionActivity(targetId, "working");
    }

    // Queued user echoes may use view id while selection is live (or vice versa).
    if (kind === "user_message_chunk") {
      const acceptUserEcho =
        !!state.selectedId &&
        (!sessionId ||
          sessionId === state.selectedId ||
          state.turnRunning ||
          isHubCreatedSession(sessionId) ||
          shouldApplyAcpToSession(sessionId));
      if (!acceptUserEcho) return;
      if (targetId !== state.selectedId) {
        withSessionTarget(targetId, () => processUserMessageChunk(update));
      } else {
        processUserMessageChunk(update);
      }
      return;
    }

    if (!shouldApplyAcpToSession(targetId)) return;

    const after = () => {
      if (kind === "tool_call" || kind === "tool_call_update") {
        const v = state.sessionViews.get(targetId);
        if (v && v.streamBuffers) v.lastToolTitle = v.streamBuffers.lastToolTitle || "";
        scheduleSessionPills();
        scheduleTurnStrip();
      }
    };

    if (targetId !== state.selectedId) {
      // Ensure pane exists for live/offscreen streaming
      getSessionPane(targetId);
      withSessionTarget(targetId, () => {
        processAcpSessionUpdate(kind, update);
        after();
      });
      return;
    }

    processAcpSessionUpdate(kind, update);
    after();
  }

  function subscribeSessionIds(...args) {
    let force = false;
    const ids = [];
    for (let i = 0; i < args.length; i++) {
      const a = args[i];
      if (a && typeof a === "object" && !Array.isArray(a) && "force" in a) {
        force = !!a.force;
        continue;
      }
      if (a) ids.push(a);
    }
    if (!state.subscribedSessions) state.subscribedSessions = new Set();
    const seen = new Set();
    for (const id of ids) {
      if (!id || seen.has(id)) continue;
      seen.add(id);
      if (!force && state.subscribedSessions.has(id)) continue;
      state.subscribedSessions.add(id);
      sendWs({ type: "subscribe", sessionId: id });
    }
  }

  /**
   * POST /api/sessions/{id}/attach — ensure live hub session (session/load or new).
   * Shared by openSession and resumeAfterReconnect after process restart.
   * @returns {{liveId: string, switched: boolean, message: string, cwd: string}|null}
   */
  async function attachSessionLive(viewId, cwd, opts = {}) {
    const showFailToast = opts.showFailToast !== false;
    const focusOnFail = !!opts.focusOnFail;
    try {
      const res = await fetch(
        apiUrl(`/api/sessions/${encodeURIComponent(viewId)}/attach`),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cwd: cwd || "" }),
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (showFailToast) {
          toast(data.error || "Attach failed — history only until agent is up", "danger");
        }
        subscribeSessionIds(viewId);
        if (focusOnFail && els.input) els.input.focus();
        return null;
      }
      const liveId = data.liveSessionId || viewId;
      const switched = !!data.switched && liveId !== viewId;
      if (Array.isArray(data.commands) && data.commands.length) {
        applyCommands(data.commands);
      }
      if (state.hubSessionIds.indexOf(liveId) < 0) {
        state.hubSessionIds = [liveId, ...state.hubSessionIds].slice(0, 50);
      }
      if (state.hubSessionIds.indexOf(viewId) < 0) {
        state.hubSessionIds = [viewId, ...state.hubSessionIds].slice(0, 50);
      }
      state.livePromptSessionId = liveId;
      if (!switched) {
        setSessionMode("live-remote", { attachSwitched: false });
      }
      return {
        liveId,
        switched,
        message: data.message || "",
        cwd: data.cwd || cwd || "",
      };
    } catch (err) {
      if (showFailToast) toast("Attach failed: " + err, "danger");
      subscribeSessionIds(viewId);
      if (focusOnFail && els.input) els.input.focus();
      return null;
    }
  }

  /**
   * Open a session for viewing and attach a live hub session when possible.
   * @returns {Promise<{ok:boolean, reason?:string, viewId?:string, liveId?:string, switched?:boolean, historyOnly?:boolean}>}
   */
  async function openSession(session) {
    // Mid-turn switch is allowed; live turn keeps streaming into its session pane.
    if (
      state.fs.dirty &&
      state.selectedId &&
      state.selectedId !== session.sessionId
    ) {
      if (!window.confirm("Discard unsaved changes?")) {
        return { ok: false, reason: "cancelled" };
      }
      state.fs.dirty = false;
    }

    const prevId = state.selectedId;
    const viewId = session.sessionId;
    const liveKeep = liveTurnId();

    if (prevId && prevId !== viewId) {
      saveComposerDraft(prevId);
      cacheSessionView(prevId);
      // Keep WS subscription on the live turn session while it runs
      if (!(state.turnRunning && prevId === liveKeep)) {
        if (state.subscribedSessions) state.subscribedSessions.delete(prevId);
        sendWs({ type: "unsubscribe", sessionId: prevId });
      }
    }

    state.selectedId = viewId;
    state.selectedMeta = session;
    // Reset live prompt target until attach reports (or same-id hub session).
    state.livePromptSessionId = isHubCreatedSession(viewId) ? viewId : null;
    if (!(state.turnRunning && viewId === liveKeep)) {
      state.promptQueueLength = 0;
    }
    state.attachSwitched = false;

    const existing = state.sessionViews.get(viewId);
    const isLiveTurnHere =
      !!state.turnRunning &&
      (viewId === liveKeep || viewId === state.status.turnSessionId);
    // Reuse when pane already has content or history was loaded (avoid HTTP + WS double rebuild).
    const hasCachedContent =
      !!(existing && existing.pane && existing.pane.childElementCount > 0) ||
      !!(existing && existing.historyLoaded);

    showEmptyMain(false);
    const paneView = showSessionPane(viewId);
    state.historyLoadedFor = paneView.historyLoaded ? viewId : null;
    state.historyFingerprint = paneView.historyFingerprint;
    state.stickToBottom = paneView.stickToBottom !== false;

    // Disk open = history until attach promotes to live-remote
    if (isHubCreatedSession(viewId) || isLiveTurnHere) {
      setSessionMode("live-remote", { attachSwitched: false });
    } else {
      setSessionMode("history", { attachSwitched: false });
    }

    setTopbarSessionMeta({
      title: session.title || "Untitled session",
      cwd: session.cwd || "",
      modelId: session.modelId || "",
      sessionId: session.sessionId || viewId,
    });
    state.sessionPlan = null;
    setViewPlanVisible(false);
    refreshSessionPlan(viewId);
    renderSessions();
    syncFsForSession();
    if (isMobile()) closeRail();
    setComposerEnabled(composerConnected());
    forceComposerUnlocked();
    updateTurnStrip();
    refreshUsage();
    restoreComposerDraft(viewId);

    // Restore live/cached pane without wiping the in-flight stream / replaying history.
    const reusePane = hasCachedContent;
    if (reusePane) {
      rebuildToolsFromPane(paneView);
      state.streamBuffers = paneView.streamBuffers;
      if (state.stickToBottom) jumpToLatest();
    } else {
      // HTTP history only when pane empty / not historyLoaded
      try {
        const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(viewId)}/history`));
        const data = await res.json();
        if (state.selectedId !== viewId) {
          return { ok: false, reason: "cancelled", viewId };
        }
        applyHistoryMessages(data.messages || [], {
          jump: !!state.stickToBottom,
        });
      } catch (err) {
        if (state.selectedId !== viewId) {
          return { ok: false, reason: "cancelled", viewId };
        }
        clearTranscript();
        showEmptyMain(false);
        showSessionPane(viewId);
        appendMessage({ role: "system", text: "Failed to load history: " + err });
        state.historyLoadedFor = null;
        state.historyFingerprint = null;
      }
    }
    // Transcript visible: pin sticky user for section under view / live turn.
    scheduleStickyUserFromScroll();

    // Attach-on-open: ensure live hub session for cwd (no foreign session/load).
    // While a turn is running on another session, attach takes the ACP lock and
    // hangs openSession for the whole tool run — skip it (history/pane only).
    let liveId = viewId;
    let switched = false;
    const skipAttachMidTurn =
      !!state.turnRunning &&
      !isLiveTurnHere &&
      !!liveKeep &&
      liveKeep !== viewId;
    if (skipAttachMidTurn) {
      setSessionMode("history", { attachSwitched: false });
      subscribeSessionIds(viewId, liveKeep);
      if (state.stickToBottom) scrollIfSticky();
      // Do not focus-steal from long turn; still unlock composer for queue/view.
      forceComposerUnlocked();
      return { ok: true, viewId, liveId: viewId, switched: false, historyOnly: true };
    }
    if (!(isLiveTurnHere && reusePane)) {
      try {
        const attached = await attachSessionLive(viewId, session.cwd || "", {
          focusOnFail: true,
          showFailToast: true,
        });
        if (!attached) {
          subscribeSessionIds(viewId, liveKeep);
          return { ok: false, reason: "attach_failed", viewId };
        }
        if (
          state.selectedId !== viewId &&
          state.selectedId !== attached.liveId
        ) {
          return { ok: false, reason: "cancelled", viewId };
        }
        liveId = attached.liveId;
        switched = !!attached.switched && liveId !== viewId;

        // Always remember where prompts should go
        state.livePromptSessionId = liveId;

        if (switched) {
          // Keep the session the user clicked (history on screen). Do not jump to
          // an empty/new hub remote id — that felt like "back to main".
          if (state.selectedId !== viewId) {
            // User navigated away during attach; do not steal focus back.
          } else {
            setSessionMode("live-remote", { attachSwitched: true });
            if (attached.message) {
              appendMessage({
                role: "system",
                text:
                  attached.message +
                  " (Still showing this session’s transcript; sends use the live hub session.)",
              });
            }
            updateSessionBanner();
            renderSessions();
          }
        } else {
          setSessionMode("live-remote", { attachSwitched: false });
          state.livePromptSessionId = liveId;
        }
      } catch (err) {
        toast("Attach failed: " + err, "danger");
        subscribeSessionIds(viewId, liveKeep);
        els.input.focus();
        return { ok: false, reason: "attach_failed", viewId };
      }
    }

    // Always keep selected + live prompt + turn sessions subscribed
    const turnId = liveTurnId();
    subscribeSessionIds(liveId, state.selectedId, turnId, state.livePromptSessionId);
    // Do not unsubscribe the session the user clicked — they are viewing it.

    setComposerEnabled(composerConnected());
    forceComposerUnlocked();
    updateTurnStrip();
    refreshUsage();
    restoreComposerDraft(viewId);
    if (state.stickToBottom) jumpToLatest();
    clampHorizontalScroll();
    els.input.focus({ preventScroll: true });
    // Focus can still nudge layout on some mobile browsers
    requestAnimationFrame(() => clampHorizontalScroll());
    return { ok: true, viewId, liveId, switched };
  }

  function sendWs(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
    }
  }

  const DRAFT_STORAGE_KEY = "grh.composerDrafts";
  const DRAFT_MAX_SESSIONS = 100;
  const DRAFT_MAX_CHARS = 50000;

  function loadComposerDrafts() {
    try {
      const raw = JSON.parse(localStorage.getItem(DRAFT_STORAGE_KEY) || "{}");
      if (!raw || typeof raw !== "object") {
        state.composerDrafts = new Map();
        return;
      }
      const map = new Map();
      const keys = Object.keys(raw);
      for (let i = 0; i < keys.length && map.size < DRAFT_MAX_SESSIONS; i++) {
        const k = keys[i];
        const v = raw[k];
        if (typeof v === "string" && v) {
          map.set(k, v.length > DRAFT_MAX_CHARS ? v.slice(0, DRAFT_MAX_CHARS) : v);
        }
      }
      state.composerDrafts = map;
    } catch (_) {
      state.composerDrafts = new Map();
    }
  }

  function persistComposerDrafts() {
    try {
      const obj = {};
      let n = 0;
      for (const [k, v] of state.composerDrafts) {
        if (!v) continue;
        obj[k] = String(v).slice(0, DRAFT_MAX_CHARS);
        n += 1;
        if (n >= DRAFT_MAX_SESSIONS) break;
      }
      localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify(obj));
    } catch (_) {}
  }

  function saveComposerDraft(sessionId) {
    if (!sessionId || !els.input) return;
    if (!state.composerDrafts) state.composerDrafts = new Map();
    const text = els.input.value || "";
    if (text) state.composerDrafts.set(sessionId, text.slice(0, DRAFT_MAX_CHARS));
    else state.composerDrafts.delete(sessionId);
    persistComposerDrafts();
  }

  function restoreComposerDraft(sessionId) {
    if (!els.input) return;
    const text =
      sessionId && state.composerDrafts && state.composerDrafts.has(sessionId)
        ? state.composerDrafts.get(sessionId) || ""
        : "";
    els.input.value = text || "";
    autoGrow();
  }

  function clearComposerDraft(sessionId) {
    if (sessionId && state.composerDrafts) state.composerDrafts.delete(sessionId);
    persistComposerDrafts();
    if (els.input && sessionId === state.selectedId) {
      els.input.value = "";
      autoGrow();
    }
  }

  function noteBootId(bootId, startedAt) {
    if (bootId) {
      if (state.bootId && state.bootId !== bootId) {
        // Hub process restarted while page stayed open — drop stale mid-turn UI.
        // Snapshot before clear so resume can detect interrupted live work.
        const snap = snapshotLiveClientForRestart();
        state._pendingRestartInterrupt = snapshotHadLive(snap);
        clearLiveClientStateAfterProcessRestart("bootId changed");
        state._hubProcessRestarted = true;
        state._resumeAfterReconnect = true;
      }
      state.bootId = bootId;
    }
    if (startedAt != null) state.startedAt = startedAt;
  }

  function probeHubHealth() {
    fetch("/health", { method: "GET", cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error("health " + r.status);
        return r.json();
      })
      .then((j) => {
        state.hubReachable = !!(j && j.ok === true);
        if (j) noteBootId(j.bootId, j.startedAt);
        if (state.wsState === "reconnecting" || state.wsState === "connecting") {
          updateStatusPill();
        }
      })
      .catch(() => {
        state.hubReachable = false;
        if (state.wsState === "reconnecting" || state.wsState === "connecting") {
          updateStatusPill();
        }
      });
  }

  function startHealthProbe() {
    if (state.healthProbeTimer) return;
    probeHubHealth();
    state.healthProbeTimer = setInterval(probeHubHealth, 4000);
  }

  function stopHealthProbe() {
    if (state.healthProbeTimer) {
      clearInterval(state.healthProbeTimer);
      state.healthProbeTimer = null;
    }
    state.hubReachable = null;
  }

  /**
   * Session ids that need resume after reconnect (selected + all mid-turn / question).
   */
  function collectLiveSessionIds() {
    const ids = [];
    const seen = new Set();
    function add(id) {
      if (!id || seen.has(id)) return;
      seen.add(id);
      ids.push(id);
    }
    add(state.selectedId);
    const turns = state.liveTurns || [];
    for (let i = 0; i < turns.length; i++) {
      if (turns[i] && turns[i].sessionId) add(turns[i].sessionId);
    }
    const flags = state.sessionFlags || {};
    for (const sid of Object.keys(flags)) {
      if (flags[sid] === "working" || flags[sid] === "question") add(sid);
    }
    const pending = state.pendingQuestionSessions || [];
    for (let i = 0; i < pending.length; i++) add(pending[i]);
    add(liveTurnId());
    add(state.livePromptSessionId);
    return ids;
  }

  /**
   * Light catch-up for live sessions discovered after reconnect status arrives.
   * Subscribe + ensure panes only (idempotent; no history wipe).
   */
  function ensureLiveSessionsResumed(ids) {
    if (!ids || !ids.length) return;
    for (let i = 0; i < ids.length; i++) {
      const sid = ids[i];
      if (!sid) continue;
      subscribeSessionIds(sid);
      getSessionPane(sid);
    }
  }

  /**
   * Render history into a session pane without thrashing the selected transcript.
   * Offscreen panes use withSessionTarget so selected scroll stays put.
   */
  function hydrateSessionPane(sessionId, messages) {
    if (!sessionId) return false;
    const list = messages || [];
    const fp = historyFingerprint(list);
    const v = getSessionPane(sessionId);
    if (fp === v.historyFingerprint && v.historyLoaded) return false;

    const isSelected = sessionId === state.selectedId;
    withSessionTarget(sessionId, () => {
      if (!v.pane.parentNode && els.transcript) {
        els.transcript.appendChild(v.pane);
        v.pane.hidden = !isSelected;
      }
      beginHistoryBatch();
      try {
        v.pane.innerHTML = "";
        if (!list.length) {
          appendMessage({
            role: "system",
            text: "No prior transcript on disk for this session.",
          });
        } else {
          for (let i = 0; i < list.length; i++) appendMessage(list[i]);
        }
        v.streamBuffers.assistantEl = null;
        v.streamBuffers.thoughtEl = null;
        v.streamBuffers.planEl = null;
        v.streamBuffers.activityEl = null;
        v.streamBuffers.tools = new Map();
        v.streamBuffers.terminals = new Map();
        v.streamBuffers.lastToolTitle = "";
      } finally {
        endHistoryBatch({ jump: false });
      }
    });

    v.historyFingerprint = fp;
    v.historyLoaded = true;
    if (isSelected) {
      state.historyLoadedFor = sessionId;
      state.historyFingerprint = fp;
      if (state.activePane === v.pane) {
        state.streamBuffers = v.streamBuffers;
      }
    }
    return true;
  }

  async function hydrateSessionHistory(sessionId, opts = {}) {
    if (!sessionId) return;
    getSessionPane(sessionId);
    try {
      const res = await fetch(
        apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/history`)
      );
      if (!res.ok) return;
      const data = await res.json();
      hydrateSessionPane(sessionId, data.messages || []);
      if (
        opts.visibleJump &&
        sessionId === state.selectedId &&
        state.stickToBottom &&
        !state._reconnectScrollFreeze
      ) {
        jumpToLatest();
      }
    } catch (_) {
      // keep current transcript on transient failures
    }
  }

  /** Merge server-truth live state from /health before multi-session resume. */
  async function mergeHealthIntoState() {
    try {
      const r = await fetch("/health", { method: "GET", cache: "no-store" });
      if (!r.ok) return null;
      const j = await r.json();
      if (!j) return null;
      noteBootId(j.bootId, j.startedAt);
      if (Array.isArray(j.liveTurns)) state.liveTurns = j.liveTurns;
      if (j.capacity && typeof j.capacity === "object") {
        state.capacity = {
          activeTurnCount: Number(j.capacity.activeTurnCount) || 0,
          maxConcurrentTurns:
            Number(j.capacity.maxConcurrentTurns) ||
            state.maxConcurrentTurns ||
            3,
          busySessionIds: Array.isArray(j.capacity.busySessionIds)
            ? j.capacity.busySessionIds.slice()
            : [],
        };
      }
      if (j.turnAgeSeconds != null) state.turnAgeSeconds = j.turnAgeSeconds;
      if (j.turnSilenceSeconds != null) {
        state.turnSilenceSeconds = j.turnSilenceSeconds;
      }
      if (j.turnRunning || (Array.isArray(j.liveTurns) && j.liveTurns.length)) {
        state._capacityStatusAt = Date.now();
      }
      if (j.contextBudget && typeof j.contextBudget === "object") {
        state.contextBudget = {
          level: j.contextBudget.level || "ok",
          updatesBytes:
            j.contextBudget.updatesBytes != null
              ? Number(j.contextBudget.updatesBytes)
              : null,
          message: j.contextBudget.message || "",
        };
      } else {
        state.contextBudget = null;
      }
      if (Array.isArray(j.pendingQuestionSessions)) {
        state.pendingQuestionSessions = j.pendingQuestionSessions;
      }
      if (j.turnSessionId) state.liveTurnSessionId = j.turnSessionId;
      if (j.turnRunning != null) {
        state.turnRunning = !!j.turnRunning;
        if (state.status) state.status.turnRunning = !!j.turnRunning;
        if (state.status && j.turnSessionId) {
          state.status.turnSessionId = j.turnSessionId;
        }
      }
      state.hubReachable = j.ok === true;
      updateCapacityBanner();
      updateContextBudgetBanner();
      return j;
    } catch (_) {
      return null;
    }
  }

  /**
   * After WS open: re-subscribe ALL mid-turn sessions, hydrate offscreen panes,
   * optional selected history refresh, single jump to latest.
   * Freezes intermediate sticky scrolls so reconnect does not thrash the pane.
   * On hub process restart, clears stale client mid-turn UI (server has empty turns).
   */
  async function resumeAfterReconnect(opts = {}) {
    const showToast = !!opts.wasReconnect;
    state._reconnectScrollFreeze = true;
    state._suppressStickyScroll = true;
    state._resumeAfterReconnect = true;
    state.reconnectAttempt = 0;
    stopHealthProbe();

    // Snapshot live client state before health merge (noteBootId may wipe flags).
    const preSnap = snapshotLiveClientForRestart();
    const lastPrompt = loadLastPrompt();
    // Prefer server truth for concurrent mid-turn projects after reconnect.
    // mergeHealthIntoState -> noteBootId may set _hubProcessRestarted + clear.
    const health = await mergeHealthIntoState();
    const processRestart = !!state._hubProcessRestarted;
    const serverHasLive = !!(
      (health &&
        (health.turnRunning ||
          (health.liveTurns && health.liveTurns.length))) ||
      ((state.liveTurns || []).length > 0 && state.turnRunning)
    );

    let interruptedByRestart = false;
    if (opts.wasReconnect && processRestart) {
      // hadLive uses pre-merge snapshot (noteBootId may already have wiped state).
      const hadLiveBeforeClear =
        snapshotHadLive(preSnap) ||
        !!state._pendingRestartInterrupt ||
        !!(lastPrompt && lastPrompt.pending);
      state._pendingRestartInterrupt = false;
      // After process restart, server has no in-flight turns/queues.
      clearLiveClientStateAfterProcessRestart("hub process restart");
      state._hubProcessRestarted = false;
      interruptedByRestart = true;
      // Auto-attach selected session so session/load runs without re-open.
      if (state.selectedId) {
        let cwd =
          (state.selectedMeta && state.selectedMeta.cwd) ||
          "";
        if (!cwd && Array.isArray(state.sessions)) {
          const row = state.sessions.find(
            (s) => s && s.sessionId === state.selectedId
          );
          if (row && row.cwd) cwd = row.cwd;
        }
        if (cwd) {
          try {
            await attachSessionLive(state.selectedId, cwd, {
              showFailToast: false,
              focusOnFail: false,
            });
          } catch (_) {
            // attach best-effort; user can re-open session
          }
        }
      }
      if (
        hadLiveBeforeClear &&
        lastPrompt &&
        lastPrompt.text &&
        String(lastPrompt.text).trim()
      ) {
        offerInterruptedResend(lastPrompt, { reason: "hub process restart" });
      } else if (hadLiveBeforeClear) {
        reportError(
          "Hub restarted: live turn interrupted. No stored prompt — type again.",
          { source: "reconnect" }
        );
      } else {
        toast("Hub restarted · reconnected", "");
      }
    } else if (opts.wasReconnect && !serverHasLive) {
      // Soft reconnect: server idle but client may still show working · quiet · queue.
      const flags = state.sessionFlags || {};
      const clientStale =
        state.turnRunning ||
        !!(state.liveTurnSessionId) ||
        Object.keys(flags).some((k) => flags[k] === "working") ||
        (state.promptQueueLength || 0) > 0;
      if (clientStale) {
        clearStaleLiveTurns({ clearQuestions: false });
      }
    }

    // New WS connection: clear subscription tracking then re-subscribe once each.
    if (state.subscribedSessions) state.subscribedSessions.clear();
    else state.subscribedSessions = new Set();

    const resumeIds = collectLiveSessionIds();
    for (let i = 0; i < resumeIds.length; i++) {
      const sid = resumeIds[i];
      subscribeSessionIds(sid);
      getSessionPane(sid);
    }
    if (state.selectedId) subscribeSessionIds(state.selectedId);

    setComposerEnabled(true);
    forceComposerUnlocked();

    const hydrates = [];
    let selectedMidTurn = false;
    for (let i = 0; i < resumeIds.length; i++) {
      const sid = resumeIds[i];
      const isSelected = sid === state.selectedId;
      // After process-restart clear, flags are idle — do not treat as mid-turn.
      const st = sessionLiveStatus(sid);
      const midTurn =
        !interruptedByRestart && (st === "working" || st === "question");

      if (isSelected && midTurn) {
        // Keep live stream; only subscribe + pane (already done).
        selectedMidTurn = true;
        continue;
      }
      if (isSelected && !midTurn) {
        hydrates.push(Promise.resolve(refreshHistory(sid)));
        continue;
      }
      // Offscreen (or unselected live): hydrate pane without moving selected scroll.
      hydrates.push(hydrateSessionHistory(sid, { visibleJump: false }));
    }

    const liveCount = interruptedByRestart
      ? 0
      : (state.liveTurns || []).length;
    const finish = () => {
      if (state.selectedId && state.stickToBottom) {
        jumpToLatest();
      } else {
        updateJumpLatestUiOnly();
      }
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          state._reconnectScrollFreeze = false;
          state._suppressStickyScroll = false;
          state._resumeAfterReconnect = false;
        });
      });
      if (interruptedByRestart) {
        // Toast already shown (danger if had live turns, else info); skip "N live".
      } else if (showToast) {
        if (liveCount > 0) {
          toast(`Hub reconnected · ${liveCount} live project(s)`, "");
        } else {
          toast("Hub reconnected", "");
        }
      } else if (selectedMidTurn) {
        toast("Turn still running on server…", "");
      }
    };

    try {
      await Promise.all(hydrates);
    } catch (_) {
      // individual hydrates already swallow fetch errors
    }
    finish();
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
      const wasReconnect =
        state.reconnectAttempt > 0 || state.wsState === "reconnecting";
      state.wsState = "open";
      updateStatusPill();
      sendWs({ type: "hello" });
      resumeAfterReconnect({ wasReconnect });
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
      if (state.subscribedSessions) state.subscribedSessions.clear();
      startHealthProbe();
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
    if (state.reconnectAttempt >= 3) {
      updateStatusPill();
    }
    const delay = Math.min(1000 * Math.pow(1.6, state.reconnectAttempt), 12000);
    state.reconnectTimer = setTimeout(connectWs, delay);
  }

  function onHubMessage(msg) {
    const type = msg.type;
    if (type === "status") {
      state.status = {
        agent: msg.agent || "down",
        agentProcess: msg.agentProcess || null,
        agentDetail: msg.agentDetail || null,
        acpQuality: msg.acpQuality || null,
        acpConnected: msg.acpConnected === true,
        acpHealAttempts:
          msg.acpHealAttempts != null ? Number(msg.acpHealAttempts) || 0 : 0,
        acpHealError: msg.acpHealError || null,
        bind: msg.bind || "local",
        tailscaleIp: msg.tailscaleIp || null,
        loadedSessionId: msg.loadedSessionId || null,
        turnRunning: !!msg.turnRunning,
        turnSessionId: msg.turnSessionId || null,
      };
      if (msg.sessionFlags && typeof msg.sessionFlags === "object") {
        state.sessionFlags = msg.sessionFlags;
      }
      if (Array.isArray(msg.liveTurns)) {
        state.liveTurns = msg.liveTurns;
        // After reconnect, status may list live sessions client had not resumed yet.
        ensureLiveSessionsResumed(
          msg.liveTurns.map((t) => (t && t.sessionId) || null).filter(Boolean)
        );
      }
      if (Array.isArray(msg.pendingQuestionSessions)) {
        state.pendingQuestionSessions = msg.pendingQuestionSessions;
        ensureLiveSessionsResumed(msg.pendingQuestionSessions);
      }
      // Preserve local pending questions across status merges: server flags can
      // lag or overwrite a just-set question with idle/working.
      {
        const pendingIds = new Set(state.pendingQuestionSessions || []);
        if (Array.isArray(msg.pendingQuestionSessions)) {
          for (let i = 0; i < msg.pendingQuestionSessions.length; i++) {
            const pid = msg.pendingQuestionSessions[i];
            if (pid) pendingIds.add(pid);
          }
        }
        if (pendingIds.size) {
          if (!state.sessionFlags) state.sessionFlags = {};
          if (!state.pendingQuestionSessions) state.pendingQuestionSessions = [];
          pendingIds.forEach((pid) => {
            state.sessionFlags[pid] = "question";
            if (state.pendingQuestionSessions.indexOf(pid) < 0) {
              state.pendingQuestionSessions.push(pid);
            }
          });
        }
      }
      if (msg.maxConcurrentTurns != null) {
        state.maxConcurrentTurns = Number(msg.maxConcurrentTurns) || 3;
      }
      // Capacity + primary turn silence/age (for capacity banner).
      if (msg.capacity && typeof msg.capacity === "object") {
        state.capacity = {
          activeTurnCount: Number(msg.capacity.activeTurnCount) || 0,
          maxConcurrentTurns:
            Number(msg.capacity.maxConcurrentTurns) ||
            state.maxConcurrentTurns ||
            3,
          busySessionIds: Array.isArray(msg.capacity.busySessionIds)
            ? msg.capacity.busySessionIds.slice()
            : [],
        };
      } else if (Array.isArray(msg.liveTurns)) {
        state.capacity = {
          activeTurnCount:
            msg.activeTurnCount != null
              ? Number(msg.activeTurnCount) || 0
              : msg.liveTurns.length,
          maxConcurrentTurns: state.maxConcurrentTurns || 3,
          busySessionIds: msg.liveTurns
            .map((t) => (t && t.sessionId) || null)
            .filter(Boolean),
        };
      }
      if (msg.turnAgeSeconds != null) state.turnAgeSeconds = msg.turnAgeSeconds;
      else if (!msg.turnRunning) state.turnAgeSeconds = null;
      if (msg.turnSilenceSeconds != null) {
        state.turnSilenceSeconds = msg.turnSilenceSeconds;
      } else if (!msg.turnRunning) {
        state.turnSilenceSeconds = null;
      }
      if (msg.turnRunning || (Array.isArray(msg.liveTurns) && msg.liveTurns.length)) {
        state._capacityStatusAt = Date.now();
      } else {
        state._capacityStatusAt = null;
      }
      if (msg.turnSessionId) {
        state.liveTurnSessionId = msg.turnSessionId;
      } else if (!msg.turnRunning && !(state.liveTurns && state.liveTurns.length)) {
        state.liveTurnSessionId = null;
      }
      if (msg.hubVersion != null) state.hubVersion = msg.hubVersion;
      if (msg.cliVersion != null) state.cliVersion = msg.cliVersion;
      if (msg.compatOk != null) state.compatOk = !!msg.compatOk;
      if (Array.isArray(msg.compatIssues)) state.compatIssues = msg.compatIssues;
      noteBootId(msg.bootId, msg.startedAt);
      if (msg.promptQueueLength != null) {
        state.promptQueueLength = Number(msg.promptQueueLength) || 0;
      }
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
      // Server empty live set: trust server over stale client mid-turn / queue UI.
      // (Process restart or soft reconnect where client still shows working · quiet · queue.)
      if (
        Array.isArray(msg.liveTurns) &&
        msg.liveTurns.length === 0 &&
        !msg.turnRunning
      ) {
        if (msg.promptQueueLength != null) {
          state.promptQueueLength = Number(msg.promptQueueLength) || 0;
        }
        const flags = state.sessionFlags || {};
        const hasWorkingFlag = Object.keys(flags).some(
          (k) => flags[k] === "working"
        );
        const clientStale =
          state.turnRunning ||
          hasWorkingFlag ||
          !!state.liveTurnSessionId ||
          !!state.turnStartedAt ||
          (state.promptQueueLength || 0) > 0;
        if (clientStale) {
          const serverPendingEmpty =
            Array.isArray(msg.pendingQuestionSessions) &&
            msg.pendingQuestionSessions.length === 0;
          clearStaleLiveTurns({ clearQuestions: serverPendingEmpty });
          if (!serverPendingEmpty && Array.isArray(msg.pendingQuestionSessions)) {
            state.pendingQuestionSessions = msg.pendingQuestionSessions;
            for (let i = 0; i < msg.pendingQuestionSessions.length; i++) {
              const pid = msg.pendingQuestionSessions[i];
              if (pid) {
                if (!state.sessionFlags) state.sessionFlags = {};
                state.sessionFlags[pid] = "question";
              }
            }
          }
          if (msg.promptQueueLength != null) {
            state.promptQueueLength = Number(msg.promptQueueLength) || 0;
          }
        }
      }
      // Server is source of truth for multi-session turns.
      // Keep streaming continuity for any live turn sessions.
      if (msg.turnRunning != null || Array.isArray(msg.liveTurns)) {
        const serverRunning =
          !!msg.turnRunning ||
          (Array.isArray(msg.liveTurns) && msg.liveTurns.length > 0);
        const turns = state.liveTurns || [];
        for (let i = 0; i < turns.length; i++) {
          if (turns[i] && turns[i].sessionId) subscribeSessionIds(turns[i].sessionId);
        }
        if (serverRunning && msg.turnSessionId) {
          subscribeSessionIds(msg.turnSessionId);
        }
        if (serverRunning && !state.turnRunning) {
          setTurnRunning(true, msg.turnSessionId || null);
          if (turnRunningOnSelected()) {
            toast("Turn still running on server…", "");
          }
        } else if (
          !serverRunning &&
          (state.turnRunning ||
            !!state.liveTurnSessionId ||
            Object.keys(state.sessionFlags || {}).some(
              (k) => state.sessionFlags[k] === "working"
            ))
        ) {
          // Server has zero lives but client still mid-turn: force global idle.
          setTurnRunning(false, msg.turnSessionId || null, {
            all: true,
            forceIdleFlags: true,
          });
          // clearStaleLiveTurns zeros queue; restore server truth if present.
          if (msg.promptQueueLength != null) {
            state.promptQueueLength = Number(msg.promptQueueLength) || 0;
          }
        } else if (serverRunning !== state.turnRunning) {
          setTurnRunning(serverRunning, msg.turnSessionId || null);
        } else {
          state.turnRunning = serverRunning;
          updateTurnStrip();
        }
      }
      if (msg.contextBudget && typeof msg.contextBudget === "object") {
        state.contextBudget = {
          level: msg.contextBudget.level || "ok",
          updatesBytes:
            msg.contextBudget.updatesBytes != null
              ? Number(msg.contextBudget.updatesBytes)
              : null,
          message: msg.contextBudget.message || "",
        };
      } else {
        state.contextBudget = null;
      }
      updateVersionBadge();
      updateStatusPill();
      scheduleSessionPills();
      updateCapacityBanner();
      updateContextBudgetBanner();
      setComposerEnabled(composerConnected());
      forceComposerUnlocked();
      return;
    }
    if (type === "queued") {
      state.promptQueueLength = msg.queueLength || msg.position || 0;
      toast(`Queued (#${msg.position || state.promptQueueLength})`, "");
      setComposerEnabled(composerConnected());
      forceComposerUnlocked();
      updateTurnStrip();
      return;
    }
    if (type === "queue") {
      state.promptQueueLength = msg.queueLength || 0;
      setComposerEnabled(composerConnected());
      forceComposerUnlocked();
      updateTurnStrip();
      return;
    }
    if (type === "sessions") {
      state.sessions = msg.items || [];
      renderSessions();
      return;
    }
    if (type === "history") {
      if (msg.sessionId === state.selectedId) {
        // Never wipe a live stream with a disk dump mid-turn.
        if (turnRunningOnSelected()) return;
        applyHistoryMessages(msg.messages || [], { jump: false });
      } else if (msg.sessionId) {
        hydrateSessionPane(msg.sessionId, msg.messages || []);
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
      } else if (shouldApplyAcpToSession(msg.sessionId)) {
        withSessionTarget(msg.sessionId, () => {
          appendMessage({ role: "system", text: msg.text || "" });
        });
      }
      return;
    }
    if (type === "session_switch") {
      const fromId = msg.from || null;
      const toId = msg.to || null;
      if (!toId) return;
      // Prefer soft-map: keep UI on the session the user clicked.
      if (state.selectedId && state.selectedId === fromId && fromId !== toId) {
        applySessionSwitch(fromId, toId, msg.reason || "", msg.message || "");
        if (state.turnRunning || msg.reason === "cli_or_foreign_session") {
          setTurnRunning(true);
        }
        return;
      }
      if (state.selectedId === toId) {
        if (state.hubSessionIds.indexOf(toId) < 0) {
          state.hubSessionIds = [toId, ...state.hubSessionIds].slice(0, 50);
        }
        state.livePromptSessionId = toId;
        setSessionMode("live-remote", {
          attachSwitched: !!(fromId && fromId !== toId),
        });
        return;
      }
      // Selected something else: only remember live id for this project
      if (state.hubSessionIds.indexOf(toId) < 0) {
        state.hubSessionIds = [toId, ...state.hubSessionIds].slice(0, 50);
      }
      if (!state.selectedId) {
        applySessionSwitch(fromId, toId, msg.reason || "", msg.message || "");
      } else {
        state.livePromptSessionId = state.livePromptSessionId || toId;
        subscribeSessionIds(state.selectedId, toId, liveTurnId());
      }
      return;
    }
    if (type === "commands") {
      // Accept for selected session, any session, or null sessionId (global cache).
      applyCommands(msg.commands || []);
      return;
    }
    if (type === "turn") {
      const sid = msg.sessionId || null;
      if (msg.state === "running" && sid) {
        state.liveTurnSessionId = sid;
        subscribeSessionIds(sid);
        markSessionActivity(sid, "working");
      } else if (msg.state === "idle" && sid) {
        markSessionActivity(sid, "idle");
        if (sid === state.liveTurnSessionId) {
          state.liveTurnSessionId =
            state.liveTurns.length
              ? state.liveTurns[state.liveTurns.length - 1].sessionId
              : null;
        }
      }
      // Track turn globally so session list + offscreen pane stay live during switches.
      const busyIdle =
        msg.state === "idle" && msg.error && /busy/i.test(String(msg.error));
      // Old hub "busy" idle+error: keep turnRunning (server still mid-turn) and unlock composer.
      if (busyIdle) {
        if (msg.error) {
          reportError(msg.error, { sessionId: sid, source: "turn" });
        }
      } else if (msg.state === "running") {
        setTurnRunning(true, sid);
        // Soft notice during same-session no-output auto-recovery (not a dead-end error).
        if (msg.error && /recovering|retrying/i.test(String(msg.error))) {
          reportInfo(msg.error, { sessionId: sid, source: "turn" });
        }
      } else if (msg.state === "idle") {
        const anyLeft = (state.liveTurns || []).length > 0;
        const recoverable =
          !!msg.error && isRecoverableTurnClear(msg.error);
        if (recoverable) {
          // Force this session fully idle after stall/max-duration clear.
          setTurnRunning(false, sid, { forceIdleFlags: !anyLeft });
          if (anyLeft) {
            state.turnRunning = true;
            if (!state.liveTurnSessionId && state.liveTurns.length) {
              state.liveTurnSessionId =
                state.liveTurns[state.liveTurns.length - 1].sessionId;
            }
          }
        } else if (anyLeft) {
          // Other projects still running: do not treat as global idle.
          state.turnRunning = true;
          if (!state.liveTurnSessionId && state.liveTurns.length) {
            state.liveTurnSessionId =
              state.liveTurns[state.liveTurns.length - 1].sessionId;
          }
          // Selected session went idle: strip must show idle for it.
          updateTurnStrip();
          // Still finalize tables for the session that just finished.
          if (!sid || sid === state.selectedId) {
            finalizeAssistantTables(transcriptRoot());
          } else if (state.sessionViews) {
            const v = state.sessionViews.get(sid);
            if (v && v.pane) finalizeAssistantTables(v.pane);
          }
        } else {
          setTurnRunning(false, sid, { forceIdleFlags: true });
        }
        // Reset timers when THIS session is idle so strip can't show quiet 445s.
        if (!sid || sid === state.selectedId) {
          if (!turnRunningOnSelected()) {
            state.turnStartedAt = null;
            state.lastTermLineAt = null;
            clearStallWatch();
          }
        }
        if (msg.error) {
          if (recoverable) {
            reportInfo(msg.error, { sessionId: sid, source: "turn" });
            // Once: also land the notice in the session transcript.
            const noteKey = `${sid || "_"}:${msg.error}`;
            if (!state._turnClearNotes) state._turnClearNotes = {};
            if (!state._turnClearNotes[noteKey]) {
              state._turnClearNotes[noteKey] = true;
              const appendClearNote = () =>
                appendMessage({ role: "system", text: msg.error });
              if (!sid || sid === state.selectedId) {
                appendClearNote();
              } else {
                withSessionTarget(sid, appendClearNote);
              }
            }
          } else {
            reportError(msg.error, { sessionId: sid, source: "turn" });
          }
        }
        if (!sid || sid === state.selectedId) {
          refreshUsage();
        }
        updateTurnStrip();
      }
      updateStatusPill();
      scheduleSessionPills();
      // Always re-enable composer after turn events (never leave disabled).
      setComposerEnabled(composerConnected());
      forceComposerUnlocked();
      return;
    }
    if (type === "error") {
      const errText = msg.message || "Error";
      const queueFull = /queue full/i.test(errText);
      const busy = /busy|stuck/i.test(errText);
      const recovering = /recovering|retrying/i.test(errText);
      const soft = recovering || isRecoverableTurnClear(errText);
      // Queue-full / busy from old hub: keep turnRunning; do not unlock the turn.
      // Recovery in progress: keep turnRunning so composer stays in working state.
      if (!queueFull && !busy && !recovering) {
        setTurnRunning(false);
      }
      if (busy && !queueFull) {
        reportError(
          "Message not queued — restart hub to enable queue, or wait for turn to finish.",
          { sessionId: msg.sessionId || null, source: "error" }
        );
      } else if (soft) {
        reportInfo(errText, {
          sessionId: msg.sessionId || null,
          source: "error",
        });
      } else {
        reportError(errText, {
          sessionId: msg.sessionId || null,
          source: "error",
        });
      }
      setComposerEnabled(composerConnected());
      forceComposerUnlocked();
      updateStatusPill();
      return;
    }
    if (type === "user_question") {
      onUserQuestion(msg);
      return;
    }
    if (type === "user_question_resolved") {
      onUserQuestionResolved(msg);
      return;
    }
    if (type === "terminal_out") {
      onTerminalOut(msg);
      return;
    }
  }

  function onUserQuestion(msg) {
    const requestId = String(msg.requestId || "");
    if (!requestId) return;
    // Resolve sessionId with fallbacks so rail always gets a flag.
    const sessionId =
      (msg.sessionId && String(msg.sessionId)) ||
      liveTurnId() ||
      (state.status && state.status.turnSessionId) ||
      state.selectedId ||
      null;
    if (sessionId) {
      if (!state.pendingQuestionSessions) state.pendingQuestionSessions = [];
      if (state.pendingQuestionSessions.indexOf(sessionId) < 0) {
        state.pendingQuestionSessions.push(sessionId);
      }
      markSessionActivity(sessionId, "question");
    }
    state.pendingUserQuestion = {
      requestId,
      sessionId,
      questions: Array.isArray(msg.questions) ? msg.questions : [],
      toolCallId: msg.toolCallId || null,
    };
    // Always open modal so the question stays answerable. Rail "Needs reply"
    // flag remains the primary multi-session notification.
    openAskUserModal();
    let toastTitle = "";
    if (sessionId && Array.isArray(state.sessions)) {
      const row = state.sessions.find((s) => s && s.sessionId === sessionId);
      if (row && row.title) toastTitle = String(row.title);
    }
    toast(
      toastTitle
        ? "Waiting for your answer · " + toastTitle
        : "Waiting for your answer",
      ""
    );
  }

  function onUserQuestionResolved(msg) {
    const requestId = String(msg.requestId || "");
    const prevSid =
      state.pendingUserQuestion && state.pendingUserQuestion.sessionId
        ? String(state.pendingUserQuestion.sessionId)
        : null;
    if (
      state.pendingUserQuestion &&
      String(state.pendingUserQuestion.requestId) === requestId
    ) {
      closeAskUserModal();
    }
    if (prevSid) {
      state.pendingQuestionSessions = (state.pendingQuestionSessions || []).filter(
        (s) => s !== prevSid
      );
      // Back to working if turn still live, else idle
      const still =
        (state.liveTurns || []).some((t) => t && t.sessionId === prevSid) ||
        state.liveTurnSessionId === prevSid;
      markSessionActivity(prevSid, still ? "working" : "idle");
    }
  }

  function openAskUserModal() {
    if (!els.modalAskUser || !els.askUserBody) return;
    renderAskUserQuestions();
    els.modalAskUser.classList.remove("hidden");
  }

  function closeAskUserModal() {
    if (els.modalAskUser) els.modalAskUser.classList.add("hidden");
    if (els.askUserBody) els.askUserBody.innerHTML = "";
    state.pendingUserQuestion = null;
  }

  function renderAskUserQuestions() {
    const body = els.askUserBody;
    if (!body) return;
    body.innerHTML = "";
    const pq = state.pendingUserQuestion;
    const questions = (pq && pq.questions) || [];
    if (!questions.length) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "No questions provided.";
      body.appendChild(p);
      return;
    }
    for (const q of questions) {
      const qid = String(q.id || "");
      const multi = !!q.multiSelect;
      const fieldset = document.createElement("fieldset");
      fieldset.className = "ask-user-q";
      fieldset.dataset.qid = qid;
      fieldset.dataset.multi = multi ? "1" : "0";

      const legend = document.createElement("legend");
      legend.textContent = q.text || "Question";
      fieldset.appendChild(legend);

      const optsWrap = document.createElement("div");
      optsWrap.className = "ask-user-opts";

      const options = Array.isArray(q.options) ? q.options : [];
      for (const opt of options) {
        const oid = String(opt.id || "");
        const labelEl = document.createElement("label");
        labelEl.className = "ask-user-opt";

        const input = document.createElement("input");
        input.type = multi ? "checkbox" : "radio";
        input.name = multi ? `ask-${qid}-${oid}` : `ask-${qid}`;
        input.value = oid;
        input.dataset.optionId = oid;
        input.dataset.optionLabel = opt.label || oid;
        if (!multi) {
          input.addEventListener("change", () => {
            // Clear "Other" selection visual when picking a listed option
            const otherCheck = fieldset.querySelector(".ask-user-other-toggle");
            if (otherCheck) otherCheck.checked = false;
          });
        }
        const bodyCol = document.createElement("span");
        bodyCol.className = "ask-user-opt-body";
        const lab = document.createElement("span");
        lab.className = "ask-user-opt-label";
        lab.textContent = opt.label || oid;
        bodyCol.appendChild(lab);
        if (opt.description) {
          const desc = document.createElement("span");
          desc.className = "ask-user-opt-desc";
          desc.textContent = opt.description;
          bodyCol.appendChild(desc);
        }
        if (opt.preview) {
          const prev = document.createElement("span");
          prev.className = "ask-user-opt-desc";
          prev.textContent = opt.preview;
          bodyCol.appendChild(prev);
        }
        labelEl.append(input, bodyCol);
        optsWrap.appendChild(labelEl);
      }

      // Always include Other with free-text input
      const otherWrap = document.createElement("div");
      otherWrap.className = "ask-user-other";
      const otherLabel = document.createElement("label");
      otherLabel.className = "ask-user-opt";
      const otherToggle = document.createElement("input");
      otherToggle.type = multi ? "checkbox" : "radio";
      otherToggle.name = multi ? `ask-${qid}-other` : `ask-${qid}`;
      otherToggle.value = "__other__";
      otherToggle.className = "ask-user-other-toggle";
      const otherBody = document.createElement("span");
      otherBody.className = "ask-user-opt-body";
      const otherLab = document.createElement("span");
      otherLab.className = "ask-user-opt-label";
      otherLab.textContent = "Other";
      otherBody.appendChild(otherLab);
      otherLabel.append(otherToggle, otherBody);
      const otherInput = document.createElement("input");
      otherInput.type = "text";
      otherInput.className = "input ask-user-other-input";
      otherInput.placeholder = "Type your answer…";
      otherInput.autocomplete = "off";
      otherInput.addEventListener("focus", () => {
        otherToggle.checked = true;
        if (!multi) {
          fieldset.querySelectorAll('input[type="radio"]').forEach((r) => {
            if (r !== otherToggle) r.checked = false;
          });
          otherToggle.checked = true;
        }
      });
      otherInput.addEventListener("input", () => {
        if ((otherInput.value || "").trim()) otherToggle.checked = true;
      });
      otherWrap.append(otherLabel, otherInput);
      optsWrap.appendChild(otherWrap);

      fieldset.appendChild(optsWrap);
      body.appendChild(fieldset);
    }
  }

  function collectAskUserAnswers() {
    const answers = {};
    if (!els.askUserBody) return answers;
    const fieldsets = els.askUserBody.querySelectorAll(".ask-user-q");
    fieldsets.forEach((fs) => {
      const qid = fs.dataset.qid || "";
      if (!qid) return;
      const multi = fs.dataset.multi === "1";
      const values = [];
      if (multi) {
        fs.querySelectorAll('input[type="checkbox"]:checked').forEach((inp) => {
          if (inp.classList.contains("ask-user-other-toggle")) {
            const text = (
              fs.querySelector(".ask-user-other-input")?.value || ""
            ).trim();
            if (text) values.push(text);
          } else {
            const id = inp.dataset.optionId || inp.value;
            // Prefer option id, fall back to label
            values.push(id || inp.dataset.optionLabel || "");
          }
        });
      } else {
        const checked = fs.querySelector('input[type="radio"]:checked');
        if (checked) {
          if (checked.classList.contains("ask-user-other-toggle")) {
            const text = (
              fs.querySelector(".ask-user-other-input")?.value || ""
            ).trim();
            if (text) values.push(text);
          } else {
            const id = checked.dataset.optionId || checked.value;
            values.push(id || checked.dataset.optionLabel || "");
          }
        }
      }
      answers[qid] = values.filter(Boolean);
    });
    return answers;
  }

  function submitAskUserAnswers() {
    const pq = state.pendingUserQuestion;
    if (!pq || !pq.requestId) {
      closeAskUserModal();
      return;
    }
    const answers = collectAskUserAnswers();
    sendWs({
      type: "user_question_answer",
      requestId: pq.requestId,
      outcome: "accepted",
      answers,
    });
    // Keep modal until user_question_resolved; close optimistically if WS down
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      closeAskUserModal();
      toast("Not connected; answer not sent", "danger");
    }
  }

  function cancelAskUserQuestion() {
    const pq = state.pendingUserQuestion;
    if (pq && pq.requestId) {
      sendWs({
        type: "user_question_answer",
        requestId: pq.requestId,
        outcome: "cancelled",
        answers: {},
      });
    }
    closeAskUserModal();
  }

  function updateComposerPlaceholder() {
    const ta = els.input;
    if (!ta) return;
    const w = ta.clientWidth || 0;
    const next = w >= 280 ? PLACEHOLDER_FULL : PLACEHOLDER_SHORT;
    if (ta.getAttribute("placeholder") !== next) {
      ta.setAttribute("placeholder", next);
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
    // Composer height changes #transcript clientHeight — re-stick same turn.
    if (state.stickToBottom) scrollIfSticky();
    updateComposerPlaceholder();
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

  function isImagePath(path) {
    return /\.(png|jpe?g|gif|webp|bmp|svg|ico)$/i.test(String(path || ""));
  }

  function isVideoPath(path) {
    return /\.(mp4|mov|webm|m4v)$/i.test(String(path || ""));
  }

  function isMediaPath(path) {
    return isImagePath(path) || isVideoPath(path);
  }

  function isHtmlPath(path) {
    return /\.html?$/i.test(String(path || ""));
  }

  /** First path-like token ending in .html/.htm; prefer longer/more absolute. */
  function extractHtmlPreviewCandidate(text) {
    if (text == null || text === "") return "";
    const s = String(text);
    const found = [];
    const push = (raw) => {
      const t = String(raw || "").trim();
      if (!t || !isHtmlPath(t.replace(/^["'`]|["'`]$/g, ""))) return;
      found.push(t);
    };
    // Quoted paths
    const quoted = /["'`]([^"'`\n\r]+?\.html?)["'`]/gi;
    let m;
    while ((m = quoted.exec(s)) !== null) push(m[1]);
    // file:// URLs
    const fileUrl = /\bfile:\/\/\/?[^\s"'<>]+?\.html?\b/gi;
    while ((m = fileUrl.exec(s)) !== null) push(m[0]);
    // Windows abs, unix abs, relative with separators, or bare filename
    const unquoted =
      /(?:^|[\s=:({\[,;])((?:[A-Za-z]:[\\/]|[\\/]|\.{1,2}[\\/])?(?:[^\s"'<>|*?\n\r]+[\\/])*[^\s"'<>|*?\n\r\\/]+\.html?)\b/gi;
    while ((m = unquoted.exec(s)) !== null) push(m[1]);
    // Bare index.html-style tokens when nothing else matched
    if (!found.length) {
      const bare = /\b([A-Za-z0-9_.-]+\.html?)\b/gi;
      while ((m = bare.exec(s)) !== null) push(m[1]);
    }
    if (!found.length) return "";
    const absScore = (p) => {
      const x = p.replace(/^["'`]|["'`]$/g, "");
      if (/^file:/i.test(x) || /^[A-Za-z]:[\\/]/.test(x) || x.startsWith("/")) return 2;
      if (/[\\/]/.test(x)) return 1;
      return 0;
    };
    found.sort((a, b) => {
      const d = absScore(b) - absScore(a);
      if (d !== 0) return d;
      return b.length - a.length;
    });
    return found[0] || "";
  }

  /** Normalize candidate to a session-relative path for startSitePreview, or "". */
  function toSitePreviewRelPath(candidate) {
    if (candidate == null || candidate === "") return "";
    let p = String(candidate).trim();
    if (
      (p.startsWith('"') && p.endsWith('"')) ||
      (p.startsWith("'") && p.endsWith("'")) ||
      (p.startsWith("`") && p.endsWith("`"))
    ) {
      p = p.slice(1, -1).trim();
    }
    p = p.replace(/^file:\/\/\/?/i, "");
    p = p.replace(/\\/g, "/");
    // file:///C:/... sometimes leaves /C:/...
    if (/^\/[A-Za-z]:\//.test(p)) p = p.slice(1);
    p = p.replace(/\/{2,}/g, "/");
    if (!isHtmlPath(p)) return "";

    const rootRaw =
      (state.fs && state.fs.root) ||
      (state.selectedMeta && state.selectedMeta.cwd) ||
      "";
    const root = String(rootRaw).replace(/\\/g, "/").replace(/\/+$/, "");
    const pLower = p.toLowerCase();
    const rootLower = root.toLowerCase();

    if (root && (pLower === rootLower || pLower.startsWith(rootLower + "/"))) {
      const rel = p.slice(root.length).replace(/^\/+/, "");
      return rel || p.split("/").filter(Boolean).pop() || "";
    }

    // Absolute outside session root: strip through project folder basename if present
    if (/^[A-Za-z]:\//.test(p) || p.startsWith("/")) {
      if (root) {
        const base = root.split("/").filter(Boolean).pop() || "";
        if (base) {
          const needle = "/" + base.toLowerCase() + "/";
          const idx = pLower.lastIndexOf(needle);
          if (idx >= 0) {
            return p.slice(idx + needle.length);
          }
        }
      }
      const parts = p.split("/").filter(Boolean);
      if (parts.length && /^[A-Za-z]:$/.test(parts[0])) parts.shift();
      if (!parts.length) return "";
      // Relative tail: keep last segment(s) so nested sites still resolve under root
      return parts.length > 1 ? parts.slice(-2).join("/") : parts[0];
    }

    return p.replace(/^\.\//, "");
  }

  function htmlPathFromToolRow(row) {
    if (!row) return "";
    const parts = [
      row._toolSummary,
      row._toolTitle,
      row.querySelector?.(".tool-one-liner")?.textContent,
      row.querySelector?.(".tool-detail")?.textContent,
      row.querySelector?.(".tool-name")?.textContent,
    ];
    for (const part of parts) {
      const c = extractHtmlPreviewCandidate(part);
      const rel = toSitePreviewRelPath(c);
      if (rel) return rel;
    }
    return "";
  }

  function refreshToolPreviewAction(row) {
    if (!row) return;
    const rel = htmlPathFromToolRow(row);
    let btn = row.querySelector(".tool-preview-btn");
    if (!rel) {
      if (btn) btn.remove();
      return;
    }
    const sum = row.querySelector("summary");
    if (!sum) return;
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-ghost btn-sm tool-preview-btn";
      btn.textContent = "Preview";
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const r = btn.dataset.previewRel || "";
        if (r) startSitePreview(r);
      });
      sum.appendChild(btn);
    }
    btn.dataset.previewRel = rel;
    btn.title = "Open site preview: " + rel;
  }

  function rawFsUrl(root, rel, opts) {
    let q =
      `/api/fs/raw?root=${encodeURIComponent(root)}` +
      `&path=${encodeURIComponent(rel)}`;
    if (opts && (opts.download === true || opts.download === 1 || opts.download === "1")) {
      q += "&download=1";
    }
    return apiUrl(q);
  }

  let sitePreviewUrl = "";
  let sitePreviewDevice = "desktop";
  let sitePreviewResizeObserver = null;

  /** Logical layout sizes used for media queries inside the iframe. */
  const SITE_PREVIEW_DEVICE_DIMS = {
    desktop: { width: 1280, height: 800 },
    tablet: { width: 768, height: 1024 },
    mobile: { width: 390, height: 844 },
  };

  function updateSitePreviewScaleLabel(scale, logicalW, logicalH) {
    const scaleEl = els.sitePreviewScale;
    if (!scaleEl) return;
    if (scale >= 0.995) {
      scaleEl.textContent = "";
      scaleEl.title = "";
      scaleEl.classList.add("hidden");
      return;
    }
    scaleEl.textContent = "Scaled " + Math.round(scale * 100) + "%";
    scaleEl.title = logicalW + "\u00d7" + logicalH + " layout";
    scaleEl.classList.remove("hidden");
  }

  /**
   * Size the preview iframe to a logical device width so CSS media queries
   * see desktop/tablet widths even on a narrow stage. When the stage is
   * smaller than the logical frame, scale the frame down with CSS transform
   * and shrink an outer shell so layout matches the visual size.
   *
   * scale = min(1, stageW/logicalW, stageH/logicalH)  for desktop/tablet
   * Mobile stays 1:1 and clamps to stage (no transform).
   * Desktop fills the stage 1:1 when stage is larger than 1280×800.
   */
  function applySitePreviewIframeSize() {
    const wrap = els.sitePreviewFrameWrap;
    const iframe = els.sitePreviewFrame;
    const shell = els.sitePreviewScaleShell;
    if (!wrap || !iframe) return;

    const d = sitePreviewDevice;
    const dims = SITE_PREVIEW_DEVICE_DIMS[d] || SITE_PREVIEW_DEVICE_DIMS.desktop;
    const stage = shell
      ? shell.parentElement
      : wrap.parentElement && wrap.parentElement.classList.contains("site-preview-stage")
        ? wrap.parentElement
        : wrap.parentElement && wrap.parentElement.parentElement;
    const stageW = stage ? stage.clientWidth : 0;
    const stageH = stage ? stage.clientHeight : 0;
    if (stageW <= 0 || stageH <= 0) return;

    let logicalW;
    let logicalH;
    let scale = 1;

    if (d === "mobile") {
      // True phone frame at 1:1; clamp only if stage is smaller than the frame.
      logicalW = Math.min(dims.width, stageW);
      logicalH = Math.min(dims.height, stageH);
      scale = 1;
    } else if (d === "tablet") {
      logicalW = dims.width;
      logicalH = dims.height;
      scale = Math.min(1, stageW / logicalW, stageH / logicalH);
      if (!Number.isFinite(scale) || scale <= 0) scale = 1;
    } else {
      // Desktop: fill stage when room for 1280×800; otherwise logical + scale-to-fit.
      if (stageW >= dims.width && stageH >= dims.height) {
        logicalW = stageW;
        logicalH = stageH;
        scale = 1;
      } else {
        logicalW = dims.width;
        logicalH = dims.height;
        scale = Math.min(1, stageW / logicalW, stageH / logicalH);
        if (!Number.isFinite(scale) || scale <= 0) scale = 1;
      }
    }

    const scaled = scale < 0.995;
    const visualW = Math.round(logicalW * scale);
    const visualH = Math.round(logicalH * scale);

    wrap.style.width = logicalW + "px";
    wrap.style.height = logicalH + "px";
    wrap.style.maxWidth = "none";
    wrap.style.maxHeight = "none";
    if (scaled) {
      wrap.style.transform = "scale(" + scale + ")";
      wrap.style.transformOrigin = "top left";
      wrap.classList.add("is-scaled");
    } else {
      wrap.style.transform = "";
      wrap.style.transformOrigin = "";
      wrap.classList.remove("is-scaled");
    }

    iframe.style.width = logicalW + "px";
    iframe.style.height = logicalH + "px";

    if (shell) {
      shell.style.width = visualW + "px";
      shell.style.height = visualH + "px";
    }

    updateSitePreviewScaleLabel(scale, logicalW, logicalH);

    try {
      if (iframe.contentWindow) {
        iframe.contentWindow.dispatchEvent(new Event("resize"));
      }
    } catch (_) {
      /* cross-origin or not ready */
    }
  }

  function ensureSitePreviewResizeObserver() {
    if (sitePreviewResizeObserver || typeof ResizeObserver === "undefined") return;
    const stage =
      (els.sitePreviewScaleShell && els.sitePreviewScaleShell.parentElement) ||
      document.querySelector(".site-preview-stage");
    if (!stage) return;
    sitePreviewResizeObserver = new ResizeObserver(() => {
      if (!els.modalSitePreview || els.modalSitePreview.classList.contains("hidden")) {
        return;
      }
      applySitePreviewIframeSize();
    });
    sitePreviewResizeObserver.observe(stage);
  }

  function setSitePreviewDevice(device) {
    const d =
      device === "tablet" || device === "mobile" ? device : "desktop";
    sitePreviewDevice = d;
    if (els.sitePreviewFrameWrap) {
      els.sitePreviewFrameWrap.dataset.device = d;
    }
    $$(".site-preview-preset").forEach((btn) => {
      const on = btn.getAttribute("data-device") === d;
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    ensureSitePreviewResizeObserver();
    requestAnimationFrame(() => {
      applySitePreviewIframeSize();
      requestAnimationFrame(applySitePreviewIframeSize);
    });
  }

  async function startSitePreview(rel) {
    const root =
      state.fs.root || (state.selectedMeta && state.selectedMeta.cwd) || "";
    if (!root) {
      toast("No project root for this session", "danger");
      return;
    }
    if (!isHtmlPath(rel)) {
      toast("Preview only works for .html files", "danger");
      return;
    }
    try {
      const res = await fetch(apiUrl("/api/preview/start"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root, path: rel }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || `Preview failed (HTTP ${res.status})`, "danger");
        return;
      }
      const previewUrl = data.previewUrl || "/preview-site/";
      sitePreviewUrl = apiUrl(previewUrl);
      if (els.sitePreviewFrame) {
        els.sitePreviewFrame.src = sitePreviewUrl;
      }
      if (els.sitePreviewPath) {
        els.sitePreviewPath.textContent = rel;
        els.sitePreviewPath.title = rel;
      }
      setSitePreviewDevice("desktop");
      if (els.modalSitePreview) {
        els.modalSitePreview.classList.remove("hidden");
      }
      // Re-measure after modal is visible so stage has real dimensions
      requestAnimationFrame(() => {
        setSitePreviewDevice(sitePreviewDevice);
      });
    } catch (err) {
      toast(String(err && err.message ? err.message : err), "danger");
    }
  }

  async function stopSitePreview() {
    if (els.sitePreviewFrame) {
      els.sitePreviewFrame.src = "about:blank";
    }
    sitePreviewUrl = "";
    if (els.modalSitePreview) {
      els.modalSitePreview.classList.add("hidden");
    }
    try {
      await fetch(apiUrl("/api/preview/stop"), { method: "POST" });
    } catch (_) {
      /* ignore network errors on close */
    }
  }

  // Hub disk handshake (plan_mode.json) + composer inject. Not the stock TUI a-key / exit_plan_mode.
  const PLAN_APPROVE_INJECT = "approved — implement the plan in plan.md";
  const PLAN_REQUEST_CHANGES_INJECT = "Request changes to the plan:\n\n";

  function setViewPlanVisible(show) {
    if (!els.btnViewPlan) return;
    els.btnViewPlan.classList.toggle("hidden", !show);
  }

  function closePlanModal() {
    if (els.modalPlan) els.modalPlan.classList.add("hidden");
  }

  function updatePlanAwaitBanner(plan) {
    if (!els.planAwaitBanner) return;
    const awaiting = !!(plan && plan.awaitingApproval && plan.exists);
    els.planAwaitBanner.classList.toggle("hidden", !awaiting);
  }

  function maybeAutoOpenPlanAwait(plan) {
    if (!plan || !plan.awaitingApproval || !plan.exists) return;
    const sid = String(plan.sessionId || state.selectedId || "").trim();
    if (!sid || state.selectedId !== sid) return;
    if (!state._planAwaitOpened) state._planAwaitOpened = new Set();
    if (state._planAwaitOpened.has(sid)) return;
    state._planAwaitOpened.add(sid);
    openPlanModal();
  }

  function renderPlanMarkdown(source) {
    if (!els.planBody) return;
    const text = source || "";
    if (typeof window.marked === "undefined" || typeof window.DOMPurify === "undefined") {
      els.planBody.textContent = text || "(empty plan)";
      return;
    }
    try {
      if (window.marked && typeof window.marked.setOptions === "function") {
        window.marked.setOptions({ gfm: true, breaks: false });
      }
      const rawHtml =
        typeof window.marked.parse === "function"
          ? window.marked.parse(text || "")
          : window.marked(text || "");
      els.planBody.innerHTML = window.DOMPurify.sanitize(rawHtml || "");
      if (!text.trim()) {
        els.planBody.innerHTML = '<p class="muted">(empty plan.md)</p>';
      }
    } catch (err) {
      els.planBody.textContent = "Preview failed: " + err + "\n\n" + text;
    }
  }

  function updatePlanStatusChip(plan) {
    if (!els.planStatusChip) return;
    const awaiting = !!(plan && plan.awaitingApproval);
    const stateLabel = plan && plan.state ? String(plan.state) : "";
    if (awaiting) {
      els.planStatusChip.textContent = "Awaiting approval (runtime)";
      els.planStatusChip.classList.remove("hidden");
    } else if (stateLabel) {
      els.planStatusChip.textContent = "State: " + stateLabel;
      els.planStatusChip.classList.remove("hidden");
    } else {
      els.planStatusChip.textContent = "";
      els.planStatusChip.classList.add("hidden");
    }
  }

  async function refreshSessionPlan(sessionId) {
    const sid = String(sessionId || state.selectedId || "").trim();
    if (!sid) {
      state.sessionPlan = null;
      setViewPlanVisible(false);
      updatePlanAwaitBanner(null);
      return null;
    }
    try {
      const res = await fetch(
        apiUrl(`/api/sessions/${encodeURIComponent(sid)}/plan`)
      );
      if (state.selectedId !== sid) return null;
      if (res.status === 404) {
        state.sessionPlan = null;
        setViewPlanVisible(false);
        updatePlanAwaitBanner(null);
        return null;
      }
      const data = await res.json().catch(() => ({}));
      if (state.selectedId !== sid) return null;
      if (!res.ok) {
        state.sessionPlan = null;
        setViewPlanVisible(false);
        updatePlanAwaitBanner(null);
        return null;
      }
      state.sessionPlan = data;
      setViewPlanVisible(!!data.exists);
      updatePlanAwaitBanner(data);
      maybeAutoOpenPlanAwait(data);
      return data;
    } catch (_) {
      if (state.selectedId === sid) {
        state.sessionPlan = null;
        setViewPlanVisible(false);
        updatePlanAwaitBanner(null);
      }
      return null;
    }
  }

  async function openPlanModal() {
    const sid = state.selectedId;
    if (!sid) {
      toast("Select a session first", "danger");
      return;
    }
    let plan = state.sessionPlan;
    if (!plan || plan.sessionId !== sid) {
      plan = await refreshSessionPlan(sid);
    }
    if (!plan || !plan.exists) {
      toast("No plan.md for this session", "danger");
      setViewPlanVisible(false);
      return;
    }
    renderPlanMarkdown(plan.markdown || "");
    updatePlanStatusChip(plan);
    if (els.modalPlan) els.modalPlan.classList.remove("hidden");
  }

  function injectPlanComposerText(text) {
    closePlanModal();
    if (!els.input) return;
    els.input.value = text;
    try {
      if (typeof autoGrow === "function") autoGrow();
    } catch (_) {}
    try {
      els.input.focus({ preventScroll: true });
    } catch (_) {
      try {
        els.input.focus();
      } catch (_) {}
    }
    // Place caret at end for request-changes prompt
    try {
      const len = els.input.value.length;
      els.input.setSelectionRange(len, len);
    } catch (_) {}
    setComposerEnabled(composerConnected());
  }

  async function postPlanAction(action) {
    const sid = state.selectedId;
    if (!sid) throw new Error("No session selected");
    const res = await fetch(
      apiUrl(`/api/sessions/${encodeURIComponent(sid)}/plan/action`),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      }
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Plan action failed");
    state.sessionPlan = data;
    setViewPlanVisible(!!data.exists);
    updatePlanAwaitBanner(data);
    updatePlanStatusChip(data);
    return data;
  }

  async function hardApprovePlan() {
    try {
      await postPlanAction("approve");
    } catch (err) {
      toast(String(err && err.message ? err.message : err), "danger");
      return;
    }
    closePlanModal();
    if (els.input) {
      els.input.value = PLAN_APPROVE_INJECT;
      try {
        if (typeof autoGrow === "function") autoGrow();
      } catch (_) {}
    }
    submitPrompt();
  }

  async function hardRequestPlanChanges() {
    try {
      await postPlanAction("request_changes");
    } catch (err) {
      toast(String(err && err.message ? err.message : err), "danger");
      return;
    }
    injectPlanComposerText(PLAN_REQUEST_CHANGES_INJECT);
  }

  async function hardQuitPlan() {
    try {
      await postPlanAction("quit");
    } catch (err) {
      toast(String(err && err.message ? err.message : err), "danger");
      return;
    }
    closePlanModal();
    toast("Plan mode cleared on Hub", "");
  }

  function hideImagePreview() {
    if (els.fileImage) els.fileImage.src = "";
    if (els.fileImageWrap) els.fileImageWrap.classList.add("hidden");
    closeLightbox();
  }

  function hideVideoPreview() {
    if (els.fileVideo) {
      try {
        els.fileVideo.pause();
      } catch (_) {}
      els.fileVideo.removeAttribute("src");
      els.fileVideo.src = "";
      try {
        els.fileVideo.load();
      } catch (_) {}
    }
    if (els.fileVideoWrap) els.fileVideoWrap.classList.add("hidden");
  }

  function hideMediaPreviews() {
    hideImagePreview();
    hideVideoPreview();
  }

  function basenameFromPath(path) {
    const s = String(path || "").replace(/\\/g, "/");
    const parts = s.split("/");
    return parts[parts.length - 1] || s || "download";
  }

  async function shareFsFile(root, rel) {
    if (!root || !rel) {
      toast("No file to share", "danger");
      return;
    }
    const name = basenameFromPath(rel);
    const url = rawFsUrl(root, rel);
    try {
      const res = await fetch(url);
      if (!res.ok) {
        toast(`Could not load file (HTTP ${res.status})`, "danger");
        return;
      }
      const blob = await res.blob();
      const type = blob.type || "";
      const file = new File([blob], name, { type: type || "application/octet-stream" });
      if (
        typeof navigator !== "undefined" &&
        typeof navigator.canShare === "function" &&
        typeof navigator.share === "function" &&
        navigator.canShare({ files: [file] })
      ) {
        await navigator.share({ files: [file], title: name });
        return;
      }
      toast("Open full size, then long-press to save");
      window.open(rawFsUrl(root, rel, { download: 1 }), "_blank", "noopener");
    } catch (err) {
      if (err && (err.name === "AbortError" || err.name === "NotAllowedError")) {
        return;
      }
      toast("Share failed: " + (err && err.message ? err.message : err), "danger");
      try {
        window.open(rawFsUrl(root, rel, { download: 1 }), "_blank", "noopener");
      } catch (_) {}
    }
  }

  function openLightbox(src) {
    if (!els.imageLightbox || !els.lightboxImg || !src) return;
    els.lightboxImg.src = src;
    els.imageLightbox.classList.remove("hidden");
  }

  function closeLightbox() {
    if (els.lightboxImg) els.lightboxImg.src = "";
    if (els.imageLightbox) els.imageLightbox.classList.add("hidden");
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
    if (isMediaPath(state.fs.openPath)) return;
    const next = mode === "preview" ? "preview" : "edit";
    state.fileViewMode = next;
    const showPreview = next === "preview";
    if (els.fileEditor) els.fileEditor.classList.toggle("hidden", showPreview);
    if (els.filePreview) els.filePreview.classList.toggle("hidden", !showPreview);
    if (els.fileImageWrap) els.fileImageWrap.classList.add("hidden");
    if (els.fileVideoWrap) els.fileVideoWrap.classList.add("hidden");
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
    const isMd = isMarkdownPath(path) && !isMediaPath(path);
    if (els.fileMdModes) els.fileMdModes.classList.toggle("hidden", !isMd);
    if (!isMd) {
      state.fileViewMode = "edit";
      if (!isMediaPath(path)) {
        if (els.fileEditor) els.fileEditor.classList.remove("hidden");
        if (els.filePreview) els.filePreview.classList.add("hidden");
        if (els.btnFileEdit) els.btnFileEdit.setAttribute("aria-selected", "true");
        if (els.btnFilePreview) els.btnFilePreview.setAttribute("aria-selected", "false");
      }
    }
  }

  function clearFilePreview() {
    if (els.filePreview) els.filePreview.innerHTML = "";
    hideMediaPreviews();
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
      if (els.fileImageWrap) els.fileImageWrap.classList.add("hidden");
      if (els.fileVideoWrap) els.fileVideoWrap.classList.add("hidden");
      if (els.fileMdModes) els.fileMdModes.classList.add("hidden");
      if (els.btnFileSave) els.btnFileSave.classList.remove("hidden");
      if (els.btnFileShare) els.btnFileShare.classList.add("hidden");
      updateFileDirtyUi();
      if (state.mainMode === "file") {
        setMainMode("chat");
      }
    }
    if (els.fileTree) els.fileTree.innerHTML = "";
  }

  function updateFileDirtyUi() {
    const dirty = !!state.fs.dirty;
    const isMedia = isMediaPath(state.fs.openPath);
    if (els.fileDirty) els.fileDirty.classList.toggle("hidden", !dirty || isMedia);
    if (els.btnFileSave) {
      els.btnFileSave.classList.toggle("hidden", isMedia);
      els.btnFileSave.disabled =
        isMedia || !dirty || state.fs.saving || !state.fs.openPath;
    }
    if (els.btnFileShare) {
      els.btnFileShare.classList.toggle("hidden", !isMedia);
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
    if (els.fileImageWrap) els.fileImageWrap.classList.add("hidden");
    if (els.fileVideoWrap) els.fileVideoWrap.classList.add("hidden");
    if (els.fileMdModes) els.fileMdModes.classList.add("hidden");
    if (els.btnFileSave) els.btnFileSave.classList.remove("hidden");
    if (els.btnFileShare) els.btnFileShare.classList.add("hidden");
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

        if (type === "file" && isHtmlPath(name)) {
          // span, not button: file row is already a <button>
          const prevBtn = document.createElement("span");
          prevBtn.className = "file-preview-btn";
          prevBtn.textContent = "Preview";
          prevBtn.title = "Preview site (relative CSS/JS)";
          prevBtn.setAttribute("role", "button");
          prevBtn.tabIndex = 0;
          const runPreview = (e) => {
            e.preventDefault();
            e.stopPropagation();
            startSitePreview(rel);
          };
          prevBtn.addEventListener("click", runPreview);
          prevBtn.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") runPreview(e);
          });
          btn.appendChild(prevBtn);
        }

        if (type === "dir") {
          btn.addEventListener("click", () => toggleDir(rel));
        } else {
          btn.addEventListener("click", () => openFile(rel));
          if (isHtmlPath(name)) {
            btn.addEventListener("dblclick", (e) => {
              e.preventDefault();
              startSitePreview(rel);
            });
          }
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

    if (isImagePath(rel)) {
      state.fs.root = root;
      state.fs.openPath = rel;
      state.fs.content = "";
      state.fs.baseline = "";
      state.fs.dirty = false;
      state.fs.error = null;
      state.fileViewMode = "edit";
      hideVideoPreview();
      if (els.fileEditor) {
        els.fileEditor.value = "";
        els.fileEditor.disabled = true;
        els.fileEditor.classList.add("hidden");
      }
      if (els.filePreview) {
        els.filePreview.innerHTML = "";
        els.filePreview.classList.add("hidden");
      }
      if (els.fileMdModes) els.fileMdModes.classList.add("hidden");
      if (els.filePathLabel) els.filePathLabel.textContent = rel;
      const src = rawFsUrl(root, rel) + "&t=" + Date.now();
      if (els.fileImage) els.fileImage.src = src;
      if (els.fileImageWrap) els.fileImageWrap.classList.remove("hidden");
      if (els.fileStatus) els.fileStatus.textContent = "Image preview";
      updateFileDirtyUi();
      setMainMode("file");
      renderFileTree();
      if (isMobile()) closeRail();
      return;
    }

    if (isVideoPath(rel)) {
      state.fs.root = root;
      state.fs.openPath = rel;
      state.fs.content = "";
      state.fs.baseline = "";
      state.fs.dirty = false;
      state.fs.error = null;
      state.fileViewMode = "edit";
      hideImagePreview();
      if (els.fileEditor) {
        els.fileEditor.value = "";
        els.fileEditor.disabled = true;
        els.fileEditor.classList.add("hidden");
      }
      if (els.filePreview) {
        els.filePreview.innerHTML = "";
        els.filePreview.classList.add("hidden");
      }
      if (els.fileMdModes) els.fileMdModes.classList.add("hidden");
      if (els.filePathLabel) els.filePathLabel.textContent = rel;
      const src = rawFsUrl(root, rel) + "&t=" + Date.now();
      if (els.fileVideo) els.fileVideo.src = src;
      if (els.fileVideoWrap) els.fileVideoWrap.classList.remove("hidden");
      if (els.fileStatus) els.fileStatus.textContent = "Video preview";
      updateFileDirtyUi();
      setMainMode("file");
      renderFileTree();
      if (isMobile()) closeRail();
      return;
    }

    hideMediaPreviews();
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
        els.fileEditor.classList.remove("hidden");
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
      if (isMobile()) closeRail();
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

  function sessionFsRoot() {
    return state.fs.root || (state.selectedMeta && state.selectedMeta.cwd) || "";
  }

  function prefillAttachedPaths(paths) {
    if (!els.input || !paths || !paths.length) return;
    const lines = paths.map((p) => `- ${String(p).replace(/\\/g, "/")}`);
    const block = "Attached file(s):\n" + lines.join("\n") + "\n\n";
    const cur = els.input.value || "";
    if (cur.trim()) {
      els.input.value = cur.replace(/\s*$/, "") + "\n\n" + block;
    } else {
      els.input.value = block;
    }
    autoGrow();
  }

  function setUploadBusy(busy) {
    if (els.btnAttach) els.btnAttach.disabled = !!busy;
    if (els.btnFsUpload) els.btnFsUpload.disabled = !!busy;
    if (els.composerFileInput) els.composerFileInput.disabled = !!busy;
  }

  async function refreshFsAfterUpload() {
    const root = sessionFsRoot();
    if (!root) return;
    if (!state.fs.root) state.fs.root = root;
    state.fs.cache.delete("");
    state.fs.cache.delete("uploads");
    try {
      await fetchFsList("");
      if (state.fs.expanded && state.fs.expanded.has("uploads")) {
        await fetchFsList("uploads");
      }
    } catch (_) {
      /* toast already from fetchFsList */
    }
  }

  async function uploadFsFiles(fileList, opts) {
    opts = opts || {};
    const prefill = !!opts.prefillComposer;
    const root = sessionFsRoot();
    if (!root) {
      toast("Open a session to upload", "danger");
      return;
    }
    const files = Array.from(fileList || []).filter(Boolean);
    if (!files.length) return;

    setUploadBusy(true);
    const uploaded = [];
    let lastError = "";
    try {
      for (const file of files) {
        const fd = new FormData();
        fd.append("root", root);
        fd.append("path", "uploads");
        fd.append("file", file, file.name || "upload.bin");
        try {
          const res = await fetch(apiUrl("/api/fs/upload"), {
            method: "POST",
            body: fd,
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            lastError =
              data.error ||
              (res.status === 413
                ? "File too large"
                : res.status === 415
                  ? "Unsupported file type"
                  : `Upload failed (HTTP ${res.status})`);
            continue;
          }
          if (data.path) uploaded.push(data.path);
        } catch (err) {
          lastError = String(err);
        }
      }
      if (uploaded.length) {
        await refreshFsAfterUpload();
        if (prefill) {
          prefillAttachedPaths(uploaded);
          if (els.input && !els.input.disabled) els.input.focus();
        }
        const label =
          uploaded.length === 1
            ? "Uploaded " + uploaded[0]
            : "Uploaded " + uploaded.length + " files";
        toast(label);
      }
      if (lastError) {
        toast(
          uploaded.length
            ? "Some uploads failed: " + lastError
            : lastError,
          "danger"
        );
      }
    } finally {
      setUploadBusy(false);
      if (els.composerFileInput) els.composerFileInput.value = "";
    }
  }

  function openUploadPicker() {
    if (!sessionFsRoot()) {
      toast("Open a session to upload", "danger");
      return;
    }
    if (!els.composerFileInput) {
      toast("Upload control missing", "danger");
      return;
    }
    els.composerFileInput.click();
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
    if (!text || !state.selectedId) return;
    if (state.wsState !== "open" || state.status.agent !== "up") {
      toast("Not connected to agent", "danger");
      return;
    }
    const sid = promptSessionId() || state.selectedId;
    const cwd = (state.selectedMeta && state.selectedMeta.cwd) || "";
    // Persist last prompt for one-tap Resend after hub process restart.
    saveLastPrompt({ sessionId: sid, text, cwd });
    // Successful hub prompt path: if already hub-created, mark live immediately
    if (isHubCreatedSession(sid) || isHubCreatedSession(state.selectedId)) {
      setSessionMode("live-remote", {
        attachSwitched: !!(
          state.livePromptSessionId &&
          state.livePromptSessionId !== state.selectedId
        ),
      });
    }
    const alreadyRunning = !!state.turnRunning;
    // Do not reset stream buffers when only queuing behind an active turn
    if (!alreadyRunning) {
      beginNewUserTurn();
      // Optimistic user bubble for instant feedback (server echo dedupes).
      const el = appendMessage({ role: "user", text }, { stream: true });
      const body = el && el.querySelector(".term-body");
      if (body) body._rawText = text;
      // Pin You: at top (CLI turn focus); stickToBottom still tracks stream below.
      // Top pin wins first paint over appendMessage's scrollIfSticky.
      activateUserPrompt(el, { scrollToTop: true });
    }
    sendWs({
      type: "prompt",
      sessionId: sid,
      text,
      cwd,
    });
    clearComposerDraft(state.selectedId);
    if (sid && sid !== state.selectedId) clearComposerDraft(sid);
    els.input.value = "";
    autoGrow();
    closeSlash();
    if (!alreadyRunning) {
      setTurnRunning(true);
    } else {
      // Optimistic until server "queued" / status arrives
      state.promptQueueLength = (state.promptQueueLength || 0) + 1;
    }
    setComposerEnabled(true);
    forceComposerUnlocked();
    updateStatusPill();
  }

  // Slash palette (position: fixed so chat-panel overflow does not clip it)
  function positionSlashPalette() {
    if (!els.slash || els.slash.classList.contains("hidden")) return;
    if (state._slashTouching) return;
    const shell = els.form && els.form.closest(".composer-shell");
    const anchor = shell || els.form || els.input;
    if (!anchor) return;
    const rect = anchor.getBoundingClientRect();
    const vv = window.visualViewport;
    const vTop = vv ? vv.offsetTop : 0;
    const vHeight = vv ? vv.height : window.innerHeight;
    const margin = 8;
    const maxH = Math.min(240, Math.floor(vHeight * 0.4));
    const availAbove = rect.top - vTop - margin;
    const height = Math.min(maxH, Math.max(120, availAbove));
    let top = rect.top - height - 4;
    if (top < vTop + margin) {
      // Not enough room above — place below composer if possible, else clamp
      top = Math.max(vTop + margin, rect.bottom + 4);
    }
    const left = Math.max(margin, rect.left);
    const width = Math.max(120, rect.width);
    const maxHeight = Math.min(maxH, Math.max(80, vHeight - (top - vTop) - margin));
    els.slash.style.top = Math.round(top) + "px";
    els.slash.style.left = Math.round(left) + "px";
    els.slash.style.width = Math.round(width) + "px";
    els.slash.style.right = "auto";
    els.slash.style.bottom = "auto";
    els.slash.style.maxHeight = Math.round(maxHeight) + "px";
  }

  async function refreshSkills() {
    try {
      const res = await fetch(apiUrl("/api/skills"));
      if (!res.ok) return;
      const data = await res.json();
      state.skills = data.items || [];
      state._skillsLoaded = true;
    } catch (_) {
      /* keep prior list */
    }
  }

  function slashItemsSignature(items) {
    return (items || [])
      .map((c) => (c.name || "") + ":" + (c._slashScore || 0))
      .join("|");
  }

  function updateSlashActiveOnly() {
    if (!els.slash) return;
    const nodes = els.slash.querySelectorAll(".slash-item");
    nodes.forEach((el, i) => {
      const isActive = i === state.slashIndex;
      el.classList.toggle("active", isActive);
      if (isActive) el.setAttribute("aria-selected", "true");
      else el.removeAttribute("aria-selected");
    });
  }

  function openSlash(filter) {
    if (!els.slash) return;
    // Fetch skills once on first open if bootstrap has not finished.
    if (!state._skillsLoaded && !state._skillsFetching) {
      state._skillsFetching = true;
      refreshSkills()
        .finally(() => {
          state._skillsFetching = false;
        })
        .then(() => {
          if (!state.slashOpen) return;
          const val = els.input.value || "";
          const nextFilter = val.startsWith("/")
            ? val.split("\n")[0].slice(1)
            : "";
          openSlash(nextFilter);
        });
    }
    const wasOpen = state.slashOpen;
    const prevListSig = state._slashListSig;
    const q = (filter || "").toLowerCase();
    const source = slashCommandSource();
    const ranked = source
      .map((c) => ({ c, score: rankSlashMatch(c, q) }))
      .filter((x) => x.score > 0 || !q)
      .sort(
        (a, b) =>
          b.score - a.score || (a.c.name || "").localeCompare(b.c.name || "")
      );
    state.slashItems = ranked
      .map((x) => Object.assign({}, x.c, { _slashScore: x.score }))
      .slice(0, 50);

    let idx = 0;
    let strong = false;
    if (q) {
      const exact = state.slashItems.findIndex(
        (c) => (c.name || "").toLowerCase() === q
      );
      if (exact >= 0) {
        idx = exact;
        strong = true;
      } else {
        const pref = state.slashItems.findIndex((c) =>
          (c.name || "").toLowerCase().startsWith(q)
        );
        if (pref >= 0) {
          idx = pref;
          strong = true;
        } else {
          idx = 0;
          strong = false;
        }
      }
    } else {
      strong = false;
    }
    state.slashIndex = state.slashItems.length ? idx : 0;
    state.slashStrongMatch = Boolean(strong && q && state.slashItems.length);

    const listSig = slashItemsSignature(state.slashItems);
    const fullSig = listSig + "#" + state.slashIndex + "#" + q;

    if (!state.slashItems.length) {
      const emptySig = "empty#" + q;
      if (wasOpen && state._slashSig === emptySig) return;
      state._slashSig = emptySig;
      state._slashListSig = "empty";
      // Always show palette when typing /; builtins keep it non-empty.
      els.slash.innerHTML = `<div class="slash-item"><span class="desc">No matching commands</span></div>`;
      els.slash.classList.remove("hidden");
      state.slashOpen = true;
      if (!wasOpen) positionSlashPalette();
      return;
    }

    // Identical list + index + filter: skip DOM work (keyup/input spam).
    if (wasOpen && state._slashSig === fullSig) {
      return;
    }

    // Same items, only active index changed: update classes, keep scroll.
    if (wasOpen && listSig === prevListSig) {
      state._slashSig = fullSig;
      updateSlashActiveOnly();
      return;
    }

    state._slashListSig = listSig;
    state._slashSig = fullSig;
    renderSlash({
      preserveScroll: wasOpen,
      scrollActive: false,
    });
    els.slash.classList.remove("hidden");
    state.slashOpen = true;
    if (!wasOpen) positionSlashPalette();
  }

  function renderSlash(opts) {
    if (!els.slash) return;
    const preserveScroll = opts && opts.preserveScroll;
    const scrollActive = opts && opts.scrollActive;
    const prevScroll = preserveScroll ? els.slash.scrollTop : 0;
    els.slash.innerHTML = "";
    const q = (() => {
      const val = els.input.value || "";
      if (!val.startsWith("/")) return "";
      const first = val.split("\n")[0];
      if (first.includes(" ") && first !== "/") return "";
      return first.slice(1).toLowerCase();
    })();
    state.slashItems.forEach((c, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const weak =
        q &&
        (c._slashScore === 10 ||
          (!(c.name || "").toLowerCase().includes(q) &&
            (c.description || "").toLowerCase().includes(q)));
      btn.className =
        "slash-item" +
        (i === state.slashIndex ? " active" : "") +
        (weak ? " weak" : "");
      btn.setAttribute("role", "option");
      if (i === state.slashIndex) btn.setAttribute("aria-selected", "true");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = "/" + (c.name || "");
      const desc = document.createElement("span");
      desc.className = "desc";
      desc.textContent = c.description || "";
      btn.append(name, desc);
      let picked = false;
      let startY = 0;
      btn.addEventListener("pointerdown", (e) => {
        startY = e.clientY;
      });
      const pick = (e) => {
        if (picked) return;
        // Ignore pointerup/click that ends a scroll gesture on the item.
        if (Math.abs(e.clientY - startY) > 8) return;
        picked = true;
        e.preventDefault();
        selectSlash(c);
      };
      btn.addEventListener("pointerup", pick);
      btn.addEventListener("click", pick);
      els.slash.appendChild(btn);
    });
    if (scrollActive) {
      const active = els.slash.querySelector(".slash-item.active");
      if (active && typeof active.scrollIntoView === "function") {
        active.scrollIntoView({ block: "nearest" });
      }
    } else if (preserveScroll) {
      els.slash.scrollTop = prevScroll;
    }
  }

  function closeSlash() {
    state.slashOpen = false;
    state._slashListSig = null;
    state._slashSig = null;
    state._slashTouching = false;
    if (!els.slash) return;
    els.slash.classList.add("hidden");
    els.slash.innerHTML = "";
    els.slash.style.top = "";
    els.slash.style.left = "";
    els.slash.style.width = "";
    els.slash.style.right = "";
    els.slash.style.bottom = "";
    els.slash.style.maxHeight = "";
  }

  function selectSlash(cmd) {
    const name = cmd.name || "";
    const hint = (cmd.input && cmd.input.hint) || "";
    els.input.value = hint ? `/${name} ` : `/${name}`;
    closeSlash();
    els.input.focus();
    autoGrow();
  }

  function maybeOpenSlashFromValue() {
    const val = els.input.value || "";
    if (val.startsWith("/")) {
      const firstLine = val.split("\n")[0];
      if (!firstLine.includes(" ") || firstLine === "/") {
        openSlash(firstLine.slice(1));
        return true;
      }
    }
    closeSlash();
    return false;
  }

  function onComposerInput() {
    forceComposerUnlocked();
    autoGrow(); // re-sticks when stickToBottom (composer shrinks transcript)
    saveComposerDraft(state.selectedId);
    maybeOpenSlashFromValue();
  }

  async function openNewModal() {
    els.modalNew.classList.remove("hidden");
    els.projectSearch.value = "";
    if (els.projectNewName) els.projectNewName.value = "";
    state.entryChoiceCwd = null;
    setProjectModalMode("list");
    await refreshProjects();
    if (els.projectNewName) els.projectNewName.focus();
    else els.projectSearch.focus();
  }

  function closeNewModal() {
    els.modalNew.classList.add("hidden");
    state.entryChoiceCwd = null;
    setProjectModalMode("list");
  }

  function setProjectModalMode(mode) {
    const m = mode === "browse" || mode === "entry" ? mode : "list";
    state.projectModalMode = m;
    if (els.projectListView) els.projectListView.classList.toggle("hidden", m !== "list");
    if (els.projectBrowser) els.projectBrowser.classList.toggle("hidden", m !== "browse");
    if (els.projectEntryChoice) els.projectEntryChoice.classList.toggle("hidden", m !== "entry");
    if (els.modalNewTitle) {
      els.modalNewTitle.textContent =
        m === "entry" ? "Resume or start new" : "New session";
    }
    if (els.modalNewHelp) {
      els.modalNewHelp.classList.toggle("hidden", m === "entry");
      if (m !== "entry") {
        els.modalNewHelp.textContent = "Pick a project working directory.";
      }
    }
  }

  /** Normalize path like hub.session_policy.cwd_key (backslash, strip, casefold). */
  function cwdKeyClient(cwd) {
    return String(cwd || "")
      .replace(/\//g, "\\")
      .replace(/\\+$/, "")
      .toLowerCase();
  }

  /** Client mirror of hub.session_policy.sessions_matching_cwd. */
  function sessionsMatchingCwd(cwd, excludeSubagents) {
    const key = cwdKeyClient(cwd);
    if (!key) return [];
    const exclude = excludeSubagents !== false;
    const items = (state.sessions || []).filter((s) => {
      if (!s) return false;
      if (exclude && s.isSubagent) return false;
      return cwdKeyClient(s.cwd) === key;
    });
    items.sort((a, b) => {
      const ta = String((a && a.updatedAt) || "");
      const tb = String((b && b.updatedAt) || "");
      if (ta === tb) return 0;
      return ta < tb ? 1 : -1;
    });
    return items;
  }

  function entryRequiresResumeChoice(priorCount) {
    return Number(priorCount) > 0;
  }

  async function onProjectChosen(cwd) {
    const path = String(cwd || "").trim();
    if (!path) {
      toast("No folder selected", "danger");
      return;
    }
    const priors = sessionsMatchingCwd(path);
    if (!entryRequiresResumeChoice(priors.length)) {
      await createSession(path);
      return;
    }
    showEntryChoice(path, priors);
  }

  function showEntryChoice(cwd, priors) {
    state.entryChoiceCwd = String(cwd || "");
    setProjectModalMode("entry");
    if (els.projectEntryCwd) {
      els.projectEntryCwd.textContent = state.entryChoiceCwd;
      els.projectEntryCwd.title = state.entryChoiceCwd;
    }
    if (!els.projectEntryPriors) return;
    els.projectEntryPriors.innerHTML = "";
    const list = Array.isArray(priors) ? priors : [];
    for (const row of list) {
      const wrap = document.createElement("div");
      wrap.className = "project-entry-row";
      wrap.setAttribute("role", "listitem");
      const meta = document.createElement("div");
      meta.className = "entry-meta";
      const title = document.createElement("span");
      title.className = "entry-title";
      title.textContent = row.title || "Untitled session";
      const idEl = document.createElement("span");
      idEl.className = "entry-id";
      const sid = String(row.sessionId || "");
      idEl.textContent = sid.length > 12 ? sid.slice(0, 8) + "…" : sid;
      idEl.title = sid;
      meta.append(title, idEl);
      const resumeBtn = document.createElement("button");
      resumeBtn.type = "button";
      resumeBtn.className = "btn btn-sm btn-accent";
      resumeBtn.textContent = "Resume";
      resumeBtn.addEventListener("click", async () => {
        closeNewModal();
        await openSession(row);
      });
      wrap.append(meta, resumeBtn);
      els.projectEntryPriors.appendChild(wrap);
    }
  }

  async function stopTurnForSession(sessionId) {
    const sid = String(sessionId || "").trim();
    if (!sid) return false;
    sendWs({ type: "cancel", sessionId: sid });
    try {
      const res = await fetch(apiUrl("/api/admin/reset-turn"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: sid }),
      });
      return res.ok;
    } catch (_) {
      return false;
    }
  }

  /**
   * Stop turn + re-attach the same selected session (CLI interrupt-then-continue).
   * @returns {Promise<{ok:boolean, reason?:string, expectedView?:string, liveId?:string, switched?:boolean}>}
   */
  async function reloadResumeSession() {
    const viewId = String(state.selectedId || "").trim();
    const sid = String(state.livePromptSessionId || state.selectedId || "").trim();
    if (!viewId && !sid) {
      toast("No session selected", "danger");
      return { ok: false, reason: "no_session" };
    }
    const expectedView = viewId || sid;
    const cancelId = sid || viewId;
    if (state.reloadingSession) {
      return { ok: false, reason: "busy", expectedView };
    }
    state.reloadingSession = true;
    if (els.btnReload) {
      els.btnReload.disabled = true;
      els.btnReload.classList.add("hidden");
    }
    try {
      const clearOk = await stopTurnForSession(cancelId);
      if (!clearOk) {
        toast("Could not clear turn — Reload aborted", "danger");
        return { ok: false, reason: "clear_failed", expectedView };
      }
      // Brief poll for turn clear (force-clear is immediate; allow status to catch up).
      const deadline = Date.now() + 3000;
      while (Date.now() < deadline) {
        if (!state.turnRunning || !turnRunningOnSelected()) break;
        await new Promise((r) => setTimeout(r, 150));
      }
      // Prefer original selected view id for re-attach (resume, not session/new).
      let session =
        (state.sessions || []).find((s) => s && s.sessionId === expectedView) ||
        null;
      if (!session && state.selectedMeta && state.selectedMeta.sessionId === expectedView) {
        session = state.selectedMeta;
      }
      if (!session) {
        session = {
          sessionId: expectedView,
          title: (state.selectedMeta && state.selectedMeta.title) || "Session",
          cwd: (state.selectedMeta && state.selectedMeta.cwd) || "",
          updatedAt: new Date().toISOString(),
          modelId: (state.selectedMeta && state.selectedMeta.modelId) || "",
          path: "",
        };
      }
      const opened = await openSession(session);
      if (!opened || !opened.ok) {
        toast("Resume failed — could not re-attach this session", "danger");
        return {
          ok: false,
          reason: (opened && opened.reason) || "open_failed",
          expectedView,
        };
      }
      const selectedAfter = String(state.selectedId || "").trim();
      if (selectedAfter !== expectedView) {
        toast("Session attach moved selection; check the open session.", "danger");
        return { ok: false, reason: "selection_mismatch", expectedView };
      }
      if (opened.switched && opened.liveId && opened.liveId !== expectedView) {
        toast(
          "Attach switched live target; still showing this session. Sends use the live hub session.",
          "danger"
        );
        return {
          ok: true,
          switched: true,
          expectedView,
          liveId: opened.liveId,
        };
      }
      toast("Session resumed — send to continue", "");
      if (els.input) els.input.focus({ preventScroll: true });
      return {
        ok: true,
        expectedView,
        liveId: opened.liveId || expectedView,
        switched: false,
      };
    } catch (err) {
      toast("Reload failed: " + err, "danger");
      return { ok: false, reason: "error", expectedView, error: String(err) };
    } finally {
      state.reloadingSession = false;
      setComposerEnabled(composerConnected());
    }
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
      btn.addEventListener("click", () => onProjectChosen(p.path));
      els.projectList.appendChild(btn);
    }
  }

  async function loadProjectBrowse(rel) {
    const path = rel == null ? "" : String(rel);
    try {
      const q = path ? `?path=${encodeURIComponent(path)}` : "";
      const res = await fetch(apiUrl(`/api/projects/browse${q}`));
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Failed to browse folders", "danger");
        return null;
      }
      state.projectBrowse = data;
      renderProjectBrowse();
      return data;
    } catch (err) {
      toast("Failed to browse folders: " + err, "danger");
      return null;
    }
  }

  function renderProjectBrowse() {
    const data = state.projectBrowse;
    if (!els.projectBrowserList) return;
    els.projectBrowserList.innerHTML = "";
    if (!data) {
      if (els.projectBrowserPath) {
        els.projectBrowserPath.textContent = "/";
        els.projectBrowserPath.title = "";
      }
      if (els.btnBrowseUp) els.btnBrowseUp.disabled = true;
      return;
    }
    const rel = data.path || "";
    const label = rel ? `/${rel}` : "/";
    if (els.projectBrowserPath) {
      els.projectBrowserPath.textContent = label;
      els.projectBrowserPath.title = data.absolute || label;
    }
    if (els.btnBrowseUp) {
      els.btnBrowseUp.disabled = data.parent === null || data.parent === undefined;
    }
    const entries = data.entries || [];
    for (const e of entries) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "project-row";
      btn.setAttribute("role", "listitem");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = e.name || e.path || "";
      btn.append(name);
      btn.addEventListener("click", () => {
        loadProjectBrowse(e.path || "");
      });
      els.projectBrowserList.appendChild(btn);
    }
  }

  async function openProjectBrowser() {
    setProjectModalMode("browse");
    await loadProjectBrowse("");
  }

  function closeProjectBrowser() {
    state.projectBrowse = null;
    setProjectModalMode("list");
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
        await onProjectChosen(data.path);
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
    const applyOffset = () => {
      const vv = window.visualViewport;
      if (vv) {
        const offset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
        document.documentElement.style.setProperty("--vv-offset", offset + "px");
      }
    };
    const applyLayout = () => {
      applyOffset();
      autoGrow();
      updateComposerPlaceholder();
      if (state.slashOpen) positionSlashPalette();
      if (state.stickToBottom) scrollIfSticky();
    };
    const vv = window.visualViewport;
    if (vv) {
      vv.addEventListener("resize", applyLayout);
      // scroll: keyboard chrome offset only — do not re-pin slash palette
      // (iOS fires visualViewport scroll while the palette itself is scrolled).
      vv.addEventListener("scroll", applyOffset);
    }
    window.addEventListener("resize", applyLayout);
    if (typeof ResizeObserver !== "undefined") {
      const roTarget = els.input || (els.form && els.form.closest(".composer"));
      if (roTarget) {
        const ro = new ResizeObserver(() => {
          updateComposerPlaceholder();
        });
        ro.observe(roTarget);
      }
    }
    updateComposerPlaceholder();
    applyLayout();
  }

  function bindEvents() {
    if (els.statusPill) {
      els.statusPill.addEventListener("click", (e) => {
        const st = els.statusPill.dataset.state;
        if (st !== "acp-hung" && st !== "agent-down") return;
        if (state.restartingAgent) return;
        e.preventDefault();
        void restartAgentFromPill();
      });
      els.statusPill.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const st = els.statusPill.dataset.state;
        if (st !== "acp-hung" && st !== "agent-down") return;
        if (state.restartingAgent) return;
        e.preventDefault();
        void restartAgentFromPill();
      });
    }

    els.sessionSearch.addEventListener("input", () => {
      state.filter = els.sessionSearch.value;
      renderSessions();
    });

    const kindFilter = document.querySelector(".session-kind-filter");
    if (kindFilter) {
      kindFilter.addEventListener("click", (e) => {
        const chip = e.target.closest(".kind-chip");
        if (!chip || !kindFilter.contains(chip)) return;
        const kind = chip.getAttribute("data-kind");
        if (kind !== "all" && kind !== "working" && kind !== "subagent") return;
        state.sessionKindFilter = kind;
        try {
          sessionStorage.setItem("grh.sessionKindFilter", kind);
        } catch (_) {}
        $$(".kind-chip", kindFilter).forEach((c) => {
          c.classList.toggle("active", c.getAttribute("data-kind") === kind);
        });
        renderSessions();
      });
    }

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
    if (els.btnFsUpload) {
      els.btnFsUpload.addEventListener("click", () => {
        openUploadPicker();
      });
    }
    if (els.btnAttach) {
      els.btnAttach.addEventListener("click", () => {
        openUploadPicker();
      });
    }
    if (els.composerFileInput) {
      els.composerFileInput.addEventListener("change", () => {
        const list = els.composerFileInput.files;
        if (!list || !list.length) return;
        // Composer attach prefills paths; Files Upload uses the same input.
        void uploadFsFiles(list, { prefillComposer: true });
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
    if (els.btnFileShare) {
      els.btnFileShare.addEventListener("click", () => {
        const root = state.fs.root || (state.selectedMeta && state.selectedMeta.cwd) || "";
        const rel = state.fs.openPath;
        if (!root || !rel || !isMediaPath(rel)) {
          toast("No media file open", "danger");
          return;
        }
        void shareFsFile(root, rel);
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
    if (els.fileImage) {
      els.fileImage.addEventListener("click", () => {
        const src = els.fileImage.getAttribute("src") || "";
        if (src) openLightbox(src);
      });
    }
    if (els.btnLightboxClose) {
      els.btnLightboxClose.addEventListener("click", (e) => {
        e.stopPropagation();
        closeLightbox();
      });
    }
    if (els.imageLightbox) {
      els.imageLightbox.addEventListener("click", (e) => {
        if (e.target === els.imageLightbox) closeLightbox();
      });
    }
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (els.imageLightbox && !els.imageLightbox.classList.contains("hidden")) {
          e.preventDefault();
          closeLightbox();
          return;
        }
        if (
          els.modalSitePreview &&
          !els.modalSitePreview.classList.contains("hidden")
        ) {
          e.preventDefault();
          stopSitePreview();
          return;
        }
        if (els.modalPlan && !els.modalPlan.classList.contains("hidden")) {
          e.preventDefault();
          closePlanModal();
          return;
        }
        if (els.modalAskUser && !els.modalAskUser.classList.contains("hidden")) {
          e.preventDefault();
          cancelAskUserQuestion();
        }
      }
    });


    els.btnMenu.addEventListener("click", openRail);
    els.backdrop.addEventListener("click", closeRail);
    if (els.btnRailCollapse) {
      els.btnRailCollapse.addEventListener("click", closeRail);
    }
    els.btnNew.addEventListener("click", openNewModal);
    if (els.btnEmptyNew) els.btnEmptyNew.addEventListener("click", openNewModal);
    if (els.btnEmptySessions) els.btnEmptySessions.addEventListener("click", openRail);
    if (els.btnRenameSession) {
      els.btnRenameSession.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!state.selectedId) return;
        const title =
          (state.selectedMeta && state.selectedMeta.title) ||
          (els.chatTitle && els.chatTitle.textContent) ||
          "Untitled session";
        startRenameSession(state.selectedId, title, els.chatTitle);
      });
    }
    if (els.btnViewPlan) {
      els.btnViewPlan.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        openPlanModal();
      });
    }
    if (els.btnPlanApprove) {
      els.btnPlanApprove.addEventListener("click", (e) => {
        e.preventDefault();
        hardApprovePlan();
      });
    }
    if (els.btnPlanRequestChanges) {
      els.btnPlanRequestChanges.addEventListener("click", (e) => {
        e.preventDefault();
        hardRequestPlanChanges();
      });
    }
    if (els.btnPlanQuit) {
      els.btnPlanQuit.addEventListener("click", (e) => {
        e.preventDefault();
        hardQuitPlan();
      });
    }
    if (els.btnPlanAwaitOpen) {
      els.btnPlanAwaitOpen.addEventListener("click", (e) => {
        e.preventDefault();
        openPlanModal();
      });
    }

    $$("[data-close]").forEach((el) => {
      el.addEventListener("click", () => {
        const id = el.getAttribute("data-close");
        if (id === "modal-new") closeNewModal();
        if (id === "modal-ask-user") cancelAskUserQuestion();
        if (id === "modal-site-preview") stopSitePreview();
        if (id === "modal-plan") closePlanModal();
      });
    });
    $$(".site-preview-preset").forEach((btn) => {
      btn.addEventListener("click", () => {
        setSitePreviewDevice(btn.getAttribute("data-device") || "desktop");
      });
    });
    if (els.btnSitePreviewOpen) {
      els.btnSitePreviewOpen.addEventListener("click", () => {
        if (!sitePreviewUrl) return;
        window.open(sitePreviewUrl, "_blank", "noopener,noreferrer");
      });
    }
    if (els.btnAskUserSubmit) {
      els.btnAskUserSubmit.addEventListener("click", () => {
        submitAskUserAnswers();
      });
    }
    if (els.btnAskUserCancel) {
      els.btnAskUserCancel.addEventListener("click", () => {
        cancelAskUserQuestion();
      });
    }

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
    if (els.btnBrowseFolders) {
      els.btnBrowseFolders.addEventListener("click", () => {
        openProjectBrowser();
      });
    }
    if (els.btnBrowseBack) {
      els.btnBrowseBack.addEventListener("click", () => {
        closeProjectBrowser();
      });
    }
    if (els.btnBrowseUp) {
      els.btnBrowseUp.addEventListener("click", () => {
        const data = state.projectBrowse;
        if (!data || data.parent === null || data.parent === undefined) return;
        loadProjectBrowse(data.parent);
      });
    }
    if (els.btnBrowseStart) {
      els.btnBrowseStart.addEventListener("click", () => {
        const abs = state.projectBrowse && state.projectBrowse.absolute;
        if (!abs) {
          toast("No folder selected", "danger");
          return;
        }
        onProjectChosen(abs);
      });
    }
    if (els.btnEntryStartNew) {
      els.btnEntryStartNew.addEventListener("click", () => {
        const cwd = state.entryChoiceCwd;
        if (!cwd) {
          toast("No project selected", "danger");
          return;
        }
        createSession(cwd);
      });
    }
    if (els.btnEntryBack) {
      els.btnEntryBack.addEventListener("click", () => {
        state.entryChoiceCwd = null;
        setProjectModalMode("list");
      });
    }
    if (els.btnReload) {
      els.btnReload.addEventListener("click", () => {
        reloadResumeSession();
      });
    }

    els.form.addEventListener("submit", (e) => {
      e.preventDefault();
      const action = resolveSlashOnSubmit();
      if (action === "select" && state.slashItems[state.slashIndex]) {
        selectSlash(state.slashItems[state.slashIndex]);
        return;
      }
      closeSlash();
      submitPrompt();
    });

    els.input.addEventListener("input", onComposerInput);
    els.input.addEventListener("keyup", (e) => {
      if (
        e.key === "/" ||
        e.key === "Backspace" ||
        e.key === "Process" ||
        (els.input.value || "").startsWith("/")
      ) {
        maybeOpenSlashFromValue();
      }
    });
    els.input.addEventListener("beforeinput", (e) => {
      // iOS sometimes delivers "/" before the value updates; schedule a check
      if (e.data === "/" || (els.input.value || "").startsWith("/")) {
        requestAnimationFrame(() => maybeOpenSlashFromValue());
      }
    });
    els.input.addEventListener("focus", () => {
      autoGrow();
      maybeOpenSlashFromValue();
      // prevent iOS scroll-jump centering the field mid-screen too aggressively
      setTimeout(() => {
        if (state.stickToBottom) scrollIfSticky();
        if (state.slashOpen) positionSlashPalette();
      }, 50);
    });

    // Isolate palette scrolling from viewport reposition thrash on mobile.
    if (els.slash) {
      els.slash.addEventListener(
        "touchstart",
        () => {
          state._slashTouching = true;
        },
        { passive: true }
      );
      els.slash.addEventListener(
        "touchend",
        () => {
          state._slashTouching = false;
        },
        { passive: true }
      );
      els.slash.addEventListener(
        "touchcancel",
        () => {
          state._slashTouching = false;
        },
        { passive: true }
      );
      els.slash.addEventListener(
        "scroll",
        (e) => {
          e.stopPropagation();
        },
        { passive: true }
      );
    }

    els.input.addEventListener("keydown", (e) => {
      if (state.slashOpen) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          state.slashIndex = Math.min(state.slashIndex + 1, state.slashItems.length - 1);
          state._slashSig =
            slashItemsSignature(state.slashItems) +
            "#" +
            state.slashIndex +
            "#" +
            ((els.input.value || "").split("\n")[0].slice(1) || "").toLowerCase();
          updateSlashActiveOnly();
          const active = els.slash && els.slash.querySelector(".slash-item.active");
          if (active && typeof active.scrollIntoView === "function") {
            active.scrollIntoView({ block: "nearest" });
          }
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          state.slashIndex = Math.max(state.slashIndex - 1, 0);
          state._slashSig =
            slashItemsSignature(state.slashItems) +
            "#" +
            state.slashIndex +
            "#" +
            ((els.input.value || "").split("\n")[0].slice(1) || "").toLowerCase();
          updateSlashActiveOnly();
          const active = els.slash && els.slash.querySelector(".slash-item.active");
          if (active && typeof active.scrollIntoView === "function") {
            active.scrollIntoView({ block: "nearest" });
          }
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          closeSlash();
          return;
        }
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          const action = resolveSlashOnSubmit();
          if (action === "select" && state.slashItems[state.slashIndex]) {
            selectSlash(state.slashItems[state.slashIndex]);
            return;
          }
          closeSlash();
          submitPrompt();
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
      const sid = state.livePromptSessionId || state.selectedId;
      sendWs({ type: "cancel", sessionId: sid });
      // Fallback if hub/agent cancel leaves turn stuck (older hubs without force-clear).
      setTimeout(async () => {
        if (!state.turnRunning) return;
        if (state.selectedId !== sid && state.livePromptSessionId !== sid) return;
        try {
          const res = await fetch(apiUrl("/api/admin/reset-turn"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ sessionId: sid }),
          });
          if (res.ok) {
            toast("Turn force-cleared (Stop fallback)", "");
          } else {
            toast(
              "Stop did not clear the turn. Try Stop again or Reload.",
              "danger"
            );
          }
        } catch (err) {
          toast("Stop fallback failed: " + err, "danger");
        }
      }, 1500);
    });

    els.transcript.addEventListener("scroll", () => {
      updateJumpLatest();
      scheduleStickyUserFromScroll();
    });

    if (els.btnJumpLatest) {
      els.btnJumpLatest.addEventListener("click", jumpToLatest);
    }
    if (els.btnErrorDismiss) {
      els.btnErrorDismiss.addEventListener("click", dismissErrorStrip);
    }
    if (els.btnErrorCopy) {
      els.btnErrorCopy.addEventListener("click", copyErrorStrip);
    }
    if (els.btnErrorResend) {
      els.btnErrorResend.addEventListener("click", () => {
        resendLastPrompt();
        dismissErrorStrip();
      });
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
    // Live stream owns the transcript while a turn is running on this session
    if (turnRunningOnSelected()) return;
    try {
      const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/history`));
      if (!res.ok) return;
      const data = await res.json();
      if (sessionId !== state.selectedId) return;
      if (turnRunningOnSelected()) return;
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

  function usageLevel(pct) {
    if (pct == null || !Number.isFinite(pct)) return "ok";
    if (pct >= 50) return "danger";
    if (pct >= 35) return "warn";
    return "ok";
  }

  function formatTokenCompact(n) {
    n = Number(n);
    if (!Number.isFinite(n) || n < 0) return "—";
    if (n < 1000) return String(Math.round(n));
    if (n < 1_000_000) {
      const k = n / 1000;
      return (k >= 100 ? Math.round(k) : Math.round(k * 10) / 10).toString().replace(/\.0$/, "") + "K";
    }
    const m = n / 1_000_000;
    return (m >= 10 ? Math.round(m) : Math.round(m * 10) / 10).toString().replace(/\.0$/, "") + "M";
  }

  function parseIsoDate(iso) {
    if (!iso || typeof iso !== "string") return null;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function formatResetCompact(iso) {
    const d = parseIsoDate(iso);
    if (!d) return "";
    return (
      "↻ " +
      d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
    );
  }

  function formatPeriodDay(iso) {
    const d = parseIsoDate(iso);
    if (!d) return "";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function formatResetLong(iso) {
    const d = parseIsoDate(iso);
    if (!d) return "";
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function formatResetAria(iso) {
    const d = parseIsoDate(iso);
    if (!d) return "";
    return d.toLocaleDateString(undefined, {
      month: "long",
      day: "numeric",
    });
  }

  function productLabel(product) {
    if (!product || typeof product !== "string") return "Grok Build";
    if (product === "GrokBuild") return "Grok Build";
    return product;
  }

  function clearUsageHideTimer() {
    if (state.usageHideTimer) {
      clearTimeout(state.usageHideTimer);
      state.usageHideTimer = null;
    }
  }

  function setUsageSegActive(seg) {
    if (els.usageSegContext) {
      els.usageSegContext.classList.toggle("usage-seg-active", seg === "context");
      els.usageSegContext.setAttribute("aria-expanded", seg === "context" ? "true" : "false");
    }
    if (els.usageSegPlan) {
      els.usageSegPlan.classList.toggle("usage-seg-active", seg === "plan");
      els.usageSegPlan.setAttribute("aria-expanded", seg === "plan" ? "true" : "false");
    }
  }

  function positionUsagePopover(anchorEl) {
    if (!els.usagePopover || !els.usageBar || !anchorEl) return;
    const barRect = els.usageBar.getBoundingClientRect();
    const anchorRect = anchorEl.getBoundingClientRect();
    const pad = 8;
    const maxW = Math.min(320, window.innerWidth * 0.92);
    els.usagePopover.style.maxWidth = `${maxW}px`;
    // Measure after content set
    els.usagePopover.classList.remove("hidden");
    const popW = Math.min(els.usagePopover.offsetWidth || maxW, maxW);
    const popH = els.usagePopover.offsetHeight || 0;
    let left = anchorRect.left;
    if (left + popW > window.innerWidth - pad) left = window.innerWidth - pad - popW;
    if (left < pad) left = pad;
    let top = barRect.bottom + 4;
    if (top + popH > window.innerHeight - pad && barRect.top > popH + pad) {
      top = barRect.top - popH - 4;
    }
    els.usagePopover.style.left = `${Math.round(left)}px`;
    els.usagePopover.style.top = `${Math.round(top)}px`;
  }

  function showUsagePopover(seg, anchorEl) {
    if (!els.usagePopover) return;
    clearUsageHideTimer();
    const text = (state.usageTitles && state.usageTitles[seg]) || "";
    els.usagePopover.textContent = text || "—";
    state.usagePopoverSeg = seg;
    setUsageSegActive(seg);
    positionUsagePopover(anchorEl || (seg === "plan" ? els.usageSegPlan : els.usageSegContext));
  }

  function hideUsagePopover() {
    clearUsageHideTimer();
    state.usagePopoverSeg = null;
    state.usagePopoverPinned = false;
    setUsageSegActive(null);
    if (els.usagePopover) {
      els.usagePopover.classList.add("hidden");
      els.usagePopover.textContent = "";
    }
  }

  function toggleUsagePopover(seg, el) {
    if (state.usagePopoverSeg === seg && state.usagePopoverPinned) {
      hideUsagePopover();
      return;
    }
    state.usagePopoverPinned = true;
    showUsagePopover(seg, el);
  }

  function scheduleHideUsagePopover() {
    clearUsageHideTimer();
    if (state.usagePopoverPinned) return;
    state.usageHideTimer = setTimeout(() => {
      state.usageHideTimer = null;
      if (!state.usagePopoverPinned) hideUsagePopover();
    }, 160);
  }

  function hideUsageBar() {
    state.usage = null;
    state.usageTitles = { context: "", plan: "" };
    hideUsagePopover();
    if (els.usageBar) els.usageBar.classList.add("hidden");
    if (els.usageBarFill) {
      els.usageBarFill.style.width = "0%";
      els.usageBarFill.dataset.level = "ok";
    }
    if (els.usageBarFillPlan) {
      els.usageBarFillPlan.style.width = "0%";
      els.usageBarFillPlan.dataset.level = "ok";
    }
    if (els.usageBarLabel) els.usageBarLabel.textContent = "—";
    if (els.usageBarTokens) els.usageBarTokens.textContent = "—";
    if (els.usageBarPlan) els.usageBarPlan.textContent = "—";
    if (els.usageBarReset) els.usageBarReset.textContent = "";
    if (els.usageSegPlan) {
      els.usageSegPlan.classList.add("usage-seg-na");
      els.usageSegPlan.setAttribute("aria-label", "Weekly plan usage unavailable");
    }
  }

  function updateUsageBar(data) {
    if (!els.usageBar || !els.usageBarFill || !els.usageBarLabel) return;

    const hasContext =
      data && data.contextPercent != null && Number.isFinite(Number(data.contextPercent));

    // Always show bar once updateUsageBar runs (context and/or plan segments).
    // Never leave the bar hidden when contextPercent is present.
    els.usageBar.classList.remove("hidden");

    if (!hasContext) {
      els.usageBarFill.style.width = "0%";
      els.usageBarFill.dataset.level = "ok";
      els.usageBarLabel.textContent = "—";
      if (els.usageBarTokens) els.usageBarTokens.textContent = "—";
      state.usageTitles.context =
        "Session context window\n" +
        "Not available from this session yet.\n\n" +
        "How full this chat is vs the model context limit.\n" +
        "Not your weekly Grok plan limit.";
    } else {
      const pct = Math.max(0, Math.min(100, Number(data.contextPercent)));
      const rounded = Math.round(pct);
      els.usageBarFill.style.width = `${pct}%`;
      els.usageBarFill.dataset.level = usageLevel(pct);
      els.usageBarLabel.textContent = `${rounded}%`;

      const used = data.contextTokensUsed;
      const windowTok = data.contextWindowTokens;
      const usedOk = used != null && Number.isFinite(Number(used));
      const winOk = windowTok != null && Number.isFinite(Number(windowTok));
      if (els.usageBarTokens) {
        els.usageBarTokens.textContent =
          usedOk && winOk
            ? `${formatTokenCompact(used)} / ${formatTokenCompact(windowTok)}`
            : "—";
      }
      if (usedOk && winOk) {
        state.usageTitles.context =
          "Session context window\n" +
          `${Number(used).toLocaleString()} / ${Number(windowTok).toLocaleString()} tokens (${rounded}%)\n\n` +
          "How full this chat is vs the model context limit.\n" +
          "Not your weekly Grok plan limit.";
      } else {
        state.usageTitles.context =
          "Session context window\n" +
          `${rounded}%\n\n` +
          "How full this chat is vs the model context limit.\n" +
          "Not your weekly Grok plan limit.";
      }
    }

    // Weekly plan segment (from nested plan or top-level weeklyPercent)
    const plan = (data && data.plan) || null;
    const weeklyRaw =
      plan && plan.weeklyPercent != null
        ? plan.weeklyPercent
        : data && data.weeklyPercent != null
          ? data.weeklyPercent
          : null;
    const planOk = weeklyRaw != null && Number.isFinite(Number(weeklyRaw));
    const periodEnd =
      (plan && plan.periodEnd) || (data && data.periodEnd) || null;
    const periodStart =
      (plan && plan.periodStart) || (data && data.periodStart) || null;
    const product =
      (plan && plan.product) || (data && data.product) || "GrokBuild";

    if (els.usageBarFillPlan) {
      if (planOk) {
        const pPct = Math.max(0, Math.min(100, Number(weeklyRaw)));
        els.usageBarFillPlan.style.width = `${pPct}%`;
        els.usageBarFillPlan.dataset.level = usageLevel(pPct);
      } else {
        els.usageBarFillPlan.style.width = "0%";
        els.usageBarFillPlan.dataset.level = "ok";
      }
    }
    if (els.usageBarPlan) {
      els.usageBarPlan.textContent = planOk ? `${Math.round(Number(weeklyRaw))}%` : "—";
    }
    if (els.usageBarReset) {
      els.usageBarReset.textContent = planOk ? formatResetCompact(periodEnd) : "";
    }
    if (els.usageSegPlan) {
      els.usageSegPlan.classList.toggle("usage-seg-na", !planOk);
      if (planOk) {
        const p = Math.round(Number(weeklyRaw));
        const resetAria = formatResetAria(periodEnd);
        els.usageSegPlan.setAttribute(
          "aria-label",
          resetAria
            ? `Weekly plan usage ${p}%, resets ${resetAria}`
            : `Weekly plan usage ${p}%`
        );
      } else {
        const err =
          (plan && plan.error) || (data && data.planError) || null;
        els.usageSegPlan.setAttribute(
          "aria-label",
          err ? `Weekly plan usage unavailable: ${err}` : "Weekly plan usage unavailable"
        );
      }
    }
    if (planOk) {
      const p = Math.round(Number(weeklyRaw));
      const startDay = formatPeriodDay(periodStart);
      const endDay = formatPeriodDay(periodEnd);
      const resetLong = formatResetLong(periodEnd);
      let body =
        `Weekly plan usage (${productLabel(product)})\n` +
        `${p}% of weekly allowance\n`;
      if (startDay && endDay) {
        body += `\nPeriod: ${startDay} – ${endDay}`;
      }
      if (resetLong) {
        body += `\nNext reset: ${resetLong} (local)`;
      }
      body += "\n\nAuto-updates while this page is open.";
      state.usageTitles.plan = body;
    } else {
      const err =
        (plan && plan.error) ||
        (data && data.plan && data.plan.error) ||
        null;
      state.usageTitles.plan =
        "Weekly plan usage (Grok Build)\n" +
        (err ? `Unavailable (${err}).\n\n` : "Not available yet.\n\n") +
        "Shows weekly Grok Build allowance from local CLI login.\n" +
        "Separate from session context window.";
    }

    // Keep open popover text fresh
    if (state.usagePopoverSeg && els.usagePopover && !els.usagePopover.classList.contains("hidden")) {
      const t = state.usageTitles[state.usagePopoverSeg] || "";
      els.usagePopover.textContent = t || "—";
      const anchor =
        state.usagePopoverSeg === "plan" ? els.usageSegPlan : els.usageSegContext;
      positionUsagePopover(anchor);
    }
  }

  function bindUsageBarEvents() {
    if (!els.usageBar) return;

    const segs = [
      { key: "context", el: els.usageSegContext },
      { key: "plan", el: els.usageSegPlan },
    ];

    for (const { key, el } of segs) {
      if (!el) continue;
      el.addEventListener("mouseenter", () => {
        if (state.usagePopoverPinned && state.usagePopoverSeg !== key) return;
        showUsagePopover(key, el);
      });
      el.addEventListener("mouseleave", () => {
        scheduleHideUsagePopover();
      });
      el.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        toggleUsagePopover(key, el);
      });
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggleUsagePopover(key, el);
        }
      });
    }

    els.usageBar.addEventListener("mouseleave", () => {
      scheduleHideUsagePopover();
    });
    els.usageBar.addEventListener("mouseenter", () => {
      clearUsageHideTimer();
    });

    document.addEventListener("click", (e) => {
      if (!state.usagePopoverSeg) return;
      if (els.usageBar && els.usageBar.contains(e.target)) return;
      hideUsagePopover();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.usagePopoverSeg) {
        hideUsagePopover();
      }
    });

    window.addEventListener("resize", () => {
      if (!state.usagePopoverSeg) return;
      const anchor =
        state.usagePopoverSeg === "plan" ? els.usageSegPlan : els.usageSegContext;
      positionUsagePopover(anchor);
    });
  }

  async function refreshUsage() {
    if (!state.selectedId) {
      hideUsageBar();
      return;
    }
    const id = state.selectedId;
    const keepOrBlank = () => {
      if (id !== state.selectedId) return;
      // Keep last good snapshot (esp. contextPercent) rather than blanking the bar.
      if (
        state.usage &&
        state.usage.contextPercent != null &&
        Number.isFinite(Number(state.usage.contextPercent))
      ) {
        updateUsageBar(state.usage);
      } else {
        updateUsageBar(null);
      }
    };
    try {
      const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(id)}/usage`));
      if (!res.ok) {
        keepOrBlank();
        return;
      }
      const data = await res.json();
      if (id !== state.selectedId) return;
      state.usage = data;
      // Always paint bar when context or plan signals exist (plan.error still shows popover).
      updateUsageBar(data);
    } catch (_) {
      keepOrBlank();
    }
  }

  function startUsagePoll() {
    if (state.usagePollTimer) return;
    state.usagePollTimer = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (!state.selectedId) return;
      refreshUsage();
    }, 6000);
  }

  async function bootstrap() {
    bindEvents();
    bindUsageBarEvents();
    bindMetaPopoverEvents();
    setupViewport();
    loadComposerDrafts();
    updateStatusPill();
    updateVersionBadge();
    updateSessionBanner();
    setComposerEnabled(false);
    updateTurnStrip();
    hideUsageBar();
    startHistoryPoll();
    startUsagePoll();

    try {
      if (localStorage.getItem("grh.railCollapsed") === "1" && !isMobile()) {
        const app = els.app || document.getElementById("app");
        if (app) app.classList.add("rail-collapsed");
        if (els.rail) els.rail.setAttribute("aria-hidden", "true");
      }
    } catch (_) {}
    try {
      state.pinnedSessions = loadPins();
    } catch (_) {
      state.pinnedSessions = [];
    }
    try {
      let kind = sessionStorage.getItem("grh.sessionKindFilter");
      if (kind === "standard") kind = "working";
      if (kind === "all" || kind === "working" || kind === "subagent") {
        state.sessionKindFilter = kind;
        if (kind !== sessionStorage.getItem("grh.sessionKindFilter")) {
          try {
            sessionStorage.setItem("grh.sessionKindFilter", kind);
          } catch (_) {}
        }
      }
    } catch (_) {}
    const kindFilter = document.querySelector(".session-kind-filter");
    if (kindFilter) {
      $$(".kind-chip", kindFilter).forEach((c) => {
        c.classList.toggle("active", c.getAttribute("data-kind") === state.sessionKindFilter);
      });
    }
    updateMenuButton();
    syncBrowseSessionsVisibility();
    window.addEventListener("resize", () => {
      updateMenuButton();
      syncBrowseSessionsVisibility();
    });

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

    await refreshSkills();
    connectWs();
  }

  // Read-only/debug inject for e2e stream visibility (no auth bypass).
  // Uses the same handleAcpMessage path as live ACP session/update.
  window.__hubTestHooks = {
    injectAcpSessionUpdate(kind, contentText, sessionId) {
      const sid =
        String(sessionId || state.selectedId || "test-session").trim() || "test-session";
      if (!state.selectedId) {
        state.selectedId = sid;
      }
      showEmptyMain(false);
      const paintId = state.selectedId;
      showSessionPane(paintId);
      getSessionPane(paintId);
      if (!state.streamBuffers) {
        state.streamBuffers = emptyStreamBuffers();
      }
      handleAcpMessage(paintId, {
        method: "session/update",
        params: {
          update: {
            sessionUpdate: kind,
            content: { type: "text", text: String(contentText || "") },
          },
        },
      });
    },
    turnStripText() {
      return (els.turnStripText && els.turnStripText.textContent) || "";
    },
    transcriptHasRole(role) {
      const root = transcriptRoot();
      return !!(root && root.querySelector(`.term-line.${role}`));
    },
    transcriptTextIncludes(substr) {
      const root = transcriptRoot();
      return ((root && root.textContent) || "").includes(substr);
    },
    setSelectedForTest(sessionId) {
      const sid = String(sessionId || "").trim();
      if (!sid) return;
      state.selectedId = sid;
      showEmptyMain(false);
      showSessionPane(sid);
    },
    setSessionsForTest(items) {
      state.sessions = Array.isArray(items) ? items : [];
    },
    getSessionIdsForTest() {
      return {
        selectedId: state.selectedId,
        livePromptSessionId: state.livePromptSessionId,
        turnRunning: state.turnRunning,
      };
    },
    reloadResumeSession,
    openSession,
    onProjectChosen,
    showEntryChoice,
    sessionsMatchingCwd,
    entryRequiresResumeChoice,
    stopTurnForSession,
    pickUserPromptIndex,
    syncStickyUserFromScroll,
    scheduleStickyUserFromScroll,
  };

  bootstrap();
})();
