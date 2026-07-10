(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

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
    sessions: [],
    filter: "",
    selectedId: null,
    selectedMeta: null,
    commands: [],
    turnRunning: false,
    stickToBottom: true,
    historyLoadedFor: null,
    streamBuffers: {
      // role keys for live merge: user, assistant, thought, tools map
      assistantEl: null,
      thoughtEl: null,
      thoughtOpen: false,
      tools: new Map(),
    },
    slashOpen: false,
    slashIndex: 0,
    slashItems: [],
    projects: [],
  };

  const els = {
    rail: $("#rail"),
    backdrop: $("#rail-backdrop"),
    sessionList: $("#session-list"),
    sessionEmpty: $("#session-empty"),
    sessionSearch: $("#session-search"),
    transcript: $("#transcript"),
    emptyMain: $("#empty-main"),
    chatTitle: $("#chat-title"),
    chatModel: $("#chat-model"),
    chatCwd: $("#chat-cwd"),
    statusPill: $("#status-pill"),
    statusLabel: $("#status-label"),
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
    toastHost: $("#toast-host"),
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
  }

  function setComposerEnabled(on) {
    const canSend = on && !state.turnRunning && state.selectedId;
    els.input.disabled = !on || !state.selectedId || state.turnRunning;
    els.btnSend.disabled = !canSend;
    els.btnStop.classList.toggle("hidden", !state.turnRunning || !state.selectedId);
    if (!state.selectedId) {
      els.composerHint.textContent = "Load a session to chat. Slash commands appear after load.";
    } else if (state.turnRunning) {
      els.composerHint.textContent = "Turn running…";
    } else if (!state.commands.length) {
      els.composerHint.textContent = "Session loaded. Slash commands appear when the agent sends them.";
    } else {
      els.composerHint.textContent = `${state.commands.length} slash commands available. Type / to open palette.`;
    }
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

  function clearTranscript() {
    els.transcript.innerHTML = "";
    state.streamBuffers = {
      assistantEl: null,
      thoughtEl: null,
      thoughtOpen: false,
      tools: new Map(),
    };
  }

  function showEmptyMain(show) {
    if (show) {
      if (!$("#empty-main", els.transcript)) {
        const wrap = document.createElement("div");
        wrap.id = "empty-main";
        wrap.className = "empty-main";
        wrap.innerHTML = `
          <div class="empty-card">
            <h2>Ops control for Grok Build</h2>
            <p>Open a session from the rail, or start a new one in a project folder.</p>
            <div class="empty-actions">
              <button type="button" id="btn-empty-sessions" class="btn btn-ghost">Browse sessions</button>
              <button type="button" id="btn-empty-new" class="btn btn-accent">New session</button>
            </div>
          </div>`;
        els.transcript.appendChild(wrap);
        $("#btn-empty-sessions", wrap).addEventListener("click", openRail);
        $("#btn-empty-new", wrap).addEventListener("click", openNewModal);
      }
    } else {
      const em = $("#empty-main", els.transcript);
      if (em) em.remove();
    }
  }

  function scrollIfSticky() {
    if (!state.stickToBottom) return;
    els.transcript.scrollTop = els.transcript.scrollHeight;
  }

  function appendMessage(msg, opts = {}) {
    const role = msg.role || "system";
    const text = msg.text || "";
    const meta = msg.meta || {};

    if (role === "thought") {
      const details = document.createElement("details");
      details.className = "msg thought";
      if (opts.open) details.open = true;
      const summary = document.createElement("summary");
      summary.textContent = "Thinking";
      const body = document.createElement("div");
      body.className = "msg-body";
      body.textContent = text;
      details.append(summary, body);
      els.transcript.appendChild(details);
      if (opts.stream) state.streamBuffers.thoughtEl = details;
      scrollIfSticky();
      return details;
    }

    if (role === "tool") {
      const details = document.createElement("details");
      details.className = "msg tool";
      const summary = document.createElement("summary");
      const name = document.createElement("span");
      name.textContent = text || "tool";
      const st = document.createElement("span");
      st.className = "tool-status";
      const status = (meta.status || "pending").toLowerCase();
      st.textContent = status;
      if (status.includes("complete") || status === "ok" || status === "success") st.classList.add("ok");
      else if (status === "pending" || status === "running") st.classList.add("pending");
      summary.append(name, st);
      const detail = document.createElement("pre");
      detail.className = "tool-detail";
      detail.textContent = meta.detail || "";
      if (!meta.detail) detail.classList.add("hidden");
      details.append(summary, detail);
      els.transcript.appendChild(details);
      if (meta.toolCallId) state.streamBuffers.tools.set(meta.toolCallId, details);
      scrollIfSticky();
      return details;
    }

    const div = document.createElement("div");
    div.className = `msg ${role}`;
    if (role !== "system") {
      const roleEl = document.createElement("div");
      roleEl.className = "msg-role";
      roleEl.textContent = role === "user" ? "You" : "Grok";
      div.appendChild(roleEl);
    }
    const body = document.createElement("div");
    body.className = "msg-body";
    body.textContent = text;
    div.appendChild(body);
    els.transcript.appendChild(div);
    if (opts.stream && role === "assistant") state.streamBuffers.assistantEl = div;
    if (opts.stream && role === "user") {
      /* no buffer needed */
    }
    scrollIfSticky();
    return div;
  }

  function renderHistory(messages) {
    clearTranscript();
    showEmptyMain(false);
    if (!messages || !messages.length) {
      appendMessage({ role: "system", text: "No prior transcript on disk for this session." });
      return;
    }
    for (const m of messages) appendMessage(m);
    state.stickToBottom = true;
    scrollIfSticky();
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

  function handleAcpMessage(sessionId, message) {
    if (!sessionId || sessionId !== state.selectedId) return;
    const method = message.method || "";
    if (method !== "session/update" && method !== "_x.ai/session/update") {
      // Final RPC results ignored for transcript
      return;
    }
    const update = (message.params && message.params.update) || {};
    const kind = update.sessionUpdate || "";

    if (kind === "user_message_chunk") {
      const text = extractText(update.content);
      if (!text) return;
      // Prefer merge into last user bubble if streaming
      const last = els.transcript.lastElementChild;
      if (last && last.classList.contains("user")) {
        const body = last.querySelector(".msg-body");
        if (body) body.textContent += text;
      } else {
        appendMessage({ role: "user", text }, { stream: true });
      }
      // Reset assistant stream buffer on new user turn
      state.streamBuffers.assistantEl = null;
      state.streamBuffers.thoughtEl = null;
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
      }
      const body = el.querySelector(".msg-body");
      if (body) body.textContent += text;
      // Close thought when answer starts
      if (state.streamBuffers.thoughtEl && state.streamBuffers.thoughtEl.open) {
        // leave as-is if user opened; auto-close after stream if was auto
      }
      scrollIfSticky();
      return;
    }

    if (kind === "agent_thought_chunk") {
      const text = extractText(update.content);
      if (!text) return;
      let el = state.streamBuffers.thoughtEl;
      if (!el || !el.isConnected) {
        el = appendMessage({ role: "thought", text: "" }, { stream: true, open: true });
        state.streamBuffers.thoughtEl = el;
      } else {
        el.open = true;
      }
      const body = el.querySelector(".msg-body");
      if (body) body.textContent += text;
      scrollIfSticky();
      return;
    }

    if (kind === "tool_call") {
      const id = update.toolCallId || "";
      const title = update.title || update.tool || "tool";
      let detail = "";
      try {
        if (update.rawInput != null) detail = JSON.stringify(update.rawInput, null, 2);
      } catch (_) {}
      appendMessage(
        {
          role: "tool",
          text: title,
          meta: { toolCallId: id, status: "pending", detail },
        },
        { stream: true }
      );
      state.streamBuffers.assistantEl = null;
      return;
    }

    if (kind === "tool_call_update") {
      const id = update.toolCallId || "";
      const el = state.streamBuffers.tools.get(id);
      if (!el) {
        appendMessage({
          role: "tool",
          text: update.title || "tool",
          meta: {
            toolCallId: id,
            status: (update.status && update.status.status) || update.status || "updated",
            detail: "",
          },
        });
        return;
      }
      const st = el.querySelector(".tool-status");
      const status =
        (typeof update.status === "object" && update.status && update.status.status) ||
        update.status ||
        "updated";
      if (st) {
        st.textContent = String(status).toLowerCase();
        st.classList.remove("ok", "pending");
        const s = String(status).toLowerCase();
        if (s.includes("complete") || s === "ok" || s === "success") st.classList.add("ok");
        else st.classList.add("pending");
      }
      if (update.title) {
        const name = el.querySelector("summary span");
        if (name && !name.classList.contains("tool-status")) name.textContent = update.title;
      }
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

    state.selectedId = session.sessionId;
    state.selectedMeta = session;
    state.commands = [];
    state.streamBuffers.assistantEl = null;
    state.streamBuffers.thoughtEl = null;
    state.streamBuffers.tools = new Map();

    els.chatTitle.textContent = session.title || "Untitled session";
    if (session.modelId) {
      els.chatModel.textContent = session.modelId;
      els.chatModel.classList.remove("hidden");
    } else {
      els.chatModel.classList.add("hidden");
    }
    els.chatCwd.textContent = session.cwd || "";
    renderSessions();
    closeRail();
    setComposerEnabled(state.wsState === "open" && state.status.agent === "up");

    // History first
    try {
      const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(session.sessionId)}/history`));
      const data = await res.json();
      renderHistory(data.messages || []);
      state.historyLoadedFor = session.sessionId;
    } catch (err) {
      clearTranscript();
      showEmptyMain(false);
      appendMessage({ role: "system", text: "Failed to load history: " + err });
    }

    // Subscribe + load
    sendWs({ type: "subscribe", sessionId: session.sessionId });
    try {
      const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(session.sessionId)}/load`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cwd: session.cwd || "" }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.error || "Load failed", "danger");
      }
    } catch (err) {
      toast("Load failed: " + err, "danger");
    }

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
      if (msg.turnRunning != null) {
        state.turnRunning = !!msg.turnRunning && msg.turnSessionId === state.selectedId;
      }
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
        renderHistory(msg.messages || []);
      }
      return;
    }
    if (type === "acp") {
      handleAcpMessage(msg.sessionId, msg.message || {});
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
        state.turnRunning = msg.state === "running";
        setComposerEnabled(state.wsState === "open" && state.status.agent === "up");
        if (msg.error) toast(msg.error, "danger");
      }
      return;
    }
    if (type === "error") {
      toast(msg.message || "Error", "danger");
    }
  }

  function autoGrow() {
    const ta = els.input;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 8 * 22) + "px";
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
    sendWs({
      type: "prompt",
      sessionId: state.selectedId,
      text,
      cwd: (state.selectedMeta && state.selectedMeta.cwd) || "",
    });
    els.input.value = "";
    autoGrow();
    closeSlash();
    state.turnRunning = true;
    setComposerEnabled(true);
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
    try {
      const res = await fetch(apiUrl("/api/projects"));
      const data = await res.json();
      state.projects = data.items || [];
    } catch {
      state.projects = [];
    }
    renderProjects();
    els.projectSearch.focus();
  }

  function closeNewModal() {
    els.modalNew.classList.add("hidden");
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
      // Refresh list then open
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

  // visualViewport / safe area for iOS keyboard
  function setupViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
    const apply = () => {
      const offset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      document.documentElement.style.setProperty("--vv-offset", offset + "px");
      if (state.stickToBottom) scrollIfSticky();
    };
    vv.addEventListener("resize", apply);
    vv.addEventListener("scroll", apply);
    apply();
  }

  function bindEvents() {
    els.sessionSearch.addEventListener("input", () => {
      state.filter = els.sessionSearch.value;
      renderSessions();
    });

    els.btnMenu.addEventListener("click", openRail);
    els.backdrop.addEventListener("click", closeRail);
    els.btnNew.addEventListener("click", openNewModal);
    els.btnEmptyNew.addEventListener("click", openNewModal);
    els.btnEmptySessions.addEventListener("click", openRail);

    $$("[data-close]").forEach((el) => {
      el.addEventListener("click", () => {
        const id = el.getAttribute("data-close");
        if (id === "modal-new") closeNewModal();
      });
    });

    els.projectSearch.addEventListener("input", renderProjects);

    els.form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (state.slashOpen && state.slashItems[state.slashIndex]) {
        selectSlash(state.slashItems[state.slashIndex]);
        return;
      }
      submitPrompt();
    });

    els.input.addEventListener("input", onComposerInput);
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
      const el = els.transcript;
      const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
      state.stickToBottom = dist < 80;
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && (!state.ws || state.ws.readyState !== WebSocket.OPEN)) {
        connectWs();
      }
    });
  }

  async function bootstrap() {
    bindEvents();
    setupViewport();
    updateStatusPill();
    setComposerEnabled(false);

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
