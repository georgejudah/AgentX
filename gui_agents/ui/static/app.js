(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const GESTURES = {
    PAUSE_AGENT: ["Open palm", "Paused the agent"],
    RESUME_AGENT: ["Fist", "Resumed the agent"],
    APPROVE_ACTION: ["Thumbs up", "Approved"],
    CANCEL_TASK: ["Thumbs down", "Cancelled the task"],
    SCREENSHOT: ["Peace sign", "Screenshot"],
    NEXT_STEP: ["Point up", "Next step"],
  };
  const FEATURE_LABELS = {
    wake_word: "Wake word",
    voice: "Voice input",
    vision_narrate: "Vision narration",
    narrate: "Narration",
    gesture: "Gestures",
    tts: "Speech",
  };

  let state = { status: "idle", task: null, queue: [], config: {}, voice: {}, gesture: null };
  let history = [];
  let elapsedTimer = null;

  // ---------- token ----------
  function readToken() {
    const m = location.hash.match(/token=([\w-]+)/);
    if (m) {
      try { sessionStorage.setItem("agentx-token", m[1]); } catch (e) {}
      try { window.history.replaceState(null, "", location.pathname + location.search); } catch (e) {}
      return m[1];
    }
    try { return sessionStorage.getItem("agentx-token") || ""; } catch (e) { return ""; }
  }
  const token = readToken();
  if (!token) $("token-banner").hidden = false;

  async function api(path, body) {
    const res = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: Object.assign({ "X-AgentX-Token": token }, body ? { "Content-Type": "application/json" } : {}),
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  }

  // ---------- theme ----------
  $("theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const current = root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("agentx-theme", next); } catch (e) {}
  });

  // ---------- reveal + parallax ----------
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const groups = new Map();
  document.querySelectorAll(".reveal").forEach((el) => {
    const parent = el.parentElement;
    const i = groups.get(parent) || 0;
    groups.set(parent, i + 1);
    el.style.setProperty("--d", Math.min(i * 70, 420) + "ms");
  });
  if ("IntersectionObserver" in window && !reduceMotion) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });
    document.querySelectorAll(".reveal").forEach((el) => io.observe(el));
  } else {
    document.querySelectorAll(".reveal").forEach((el) => el.classList.add("in"));
  }

  if (!reduceMotion) {
    const orb = $("orb-wrap"), hero = $("hero-content");
    let ticking = false;
    addEventListener("scroll", () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        const y = Math.min(scrollY, innerHeight);
        orb.style.transform = `translate3d(0, ${y * 0.35}px, 0) scale(${1 + y / innerHeight * 0.15})`;
        hero.style.transform = `translate3d(0, ${y * 0.12}px, 0)`;
        hero.style.opacity = String(Math.max(0, 1 - y / (innerHeight * 0.75)));
        ticking = false;
      });
    }, { passive: true });
  }

  // ---------- rendering ----------
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function timeOf(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  function fmtDuration(s) {
    if (s == null) return "";
    s = Math.round(s);
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
  }

  function renderStatus() {
    const s = state.status;
    $("orb").dataset.state = s;
    const phrase = (state.config && state.config.wake_phrase) || "hey agent";
    const wake = state.config && state.config.features && state.config.features.wake_word;
    const lines = {
      idle: wake ? `Ready. Say “${phrase}” or type a task.` : "Ready. Type a task to begin.",
      listening: "Listening…",
      working: state.task && state.task.total > 1 ? `Working on task ${state.task.index} of ${state.task.total}` : "Working…",
      paused: "Paused. Resume here or show a fist.",
    };
    $("status-line").textContent = lines[s] || s;
    $("pause-btn").disabled = !(s === "working" || s === "paused");
    $("pause-btn").textContent = s === "paused" ? "Resume" : "Pause";
    $("cancel-btn").disabled = !state.task;
  }

  function renderTask() {
    const t = state.task;
    clearInterval(elapsedTimer);
    if (!t) {
      $("now-task").textContent = "Waiting for a task";
      $("now-meta").textContent = "Idle";
      $("now-bar").style.width = "0%";
      $("now-progress").setAttribute("aria-valuenow", "0");
      $("now-action").textContent = "";
      return;
    }
    $("now-task").textContent = t.instruction;
    const pct = Math.min(100, (t.step / (t.max_steps || 15)) * 100);
    $("now-bar").style.width = pct + "%";
    $("now-progress").setAttribute("aria-valuemax", String(t.max_steps || 15));
    $("now-progress").setAttribute("aria-valuenow", String(t.step));
    $("now-action").textContent = t.action || "Looking at the screen…";
    const meta = () => {
      const parts = [];
      if (t.total > 1) parts.push(`Task ${t.index} of ${t.total}`);
      parts.push(`Step ${t.step} of ${t.max_steps || 15}`);
      if (t.started) parts.push(fmtDuration(Date.now() / 1000 - t.started));
      $("now-meta").textContent = parts.join(" · ");
    };
    meta();
    elapsedTimer = setInterval(meta, 1000);
  }

  function addNarration(item, animate) {
    const li = el("li");
    li.append(el("time", null, timeOf(item.ts)), document.createTextNode(item.text));
    if (!animate) li.style.animation = "none";
    $("feed").prepend(li);
    while ($("feed").children.length > 40) $("feed").lastChild.remove();
    $("feed-empty").hidden = true;
  }

  function renderQueue() {
    const q = $("queue");
    q.replaceChildren(...(state.queue || []).map((t) => el("li", null, t)));
    $("queue-empty").hidden = (state.queue || []).length > 0;
  }

  function renderVoice() {
    const v = state.voice || {};
    const labels = { off: "Off", idle: "Standing by", listening: "Listening", heard: "Heard you" };
    $("voice-state").textContent = labels[v.state] || "Off";
    $("voice-heard").textContent = v.heard ? `“${v.heard}”` : "";
  }

  function renderGesture() {
    const g = state.gesture;
    if (!g) return;
    const [name, action] = GESTURES[g.name] || [g.name, ""];
    $("gesture-name").textContent = name;
    $("gesture-action").textContent = `${action} at ${timeOf(g.ts)}`;
  }

  function renderConfig() {
    const c = state.config || {};
    $("model-name").textContent = c.model || "—";
    $("model-ground").textContent = [c.provider, c.ground_model && `grounding: ${c.ground_model}`].filter(Boolean).join(" · ");
    const f = c.features || {};
    $("features").replaceChildren(...Object.keys(FEATURE_LABELS).map((k) => el("li", f[k] ? "" : "off", FEATURE_LABELS[k])));
  }

  function renderHistory() {
    const tl = $("timeline");
    tl.replaceChildren(...history.map((h) => {
      const li = el("li");
      li.dataset.result = h.result || "";
      const when = h.ts ? new Date(h.ts * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : "";
      const resultText = { done: "Completed", failed: "Couldn't finish", cancelled: "Cancelled", stopped: "Stopped at step limit" }[h.result] || h.result;
      li.append(
        el("p", "t-when", when),
        el("p", "t-task", h.instruction || ""),
        el("p", "t-meta", [resultText, h.steps != null && `${h.steps} steps`, fmtDuration(h.duration)].filter(Boolean).join(" · "))
      );
      return li;
    }));
    $("history-empty").hidden = history.length > 0;

    const startOfDay = new Date(); startOfDay.setHours(0, 0, 0, 0);
    const today = history.filter((h) => h.ts && h.ts * 1000 >= startOfDay.getTime());
    $("stat-tasks").textContent = String(today.length);
    $("stat-rate").textContent = today.length ? Math.round(today.filter((h) => h.result === "done").length / today.length * 100) + "%" : "—";
    const stepped = today.filter((h) => h.steps != null);
    $("stat-steps").textContent = stepped.length ? (stepped.reduce((a, h) => a + h.steps, 0) / stepped.length).toFixed(1) : "—";
  }

  function renderAll() {
    renderConfig(); renderStatus(); renderTask(); renderQueue(); renderVoice(); renderGesture();
  }

  async function loadHistory() {
    if (!token) return;
    try { history = await api("/api/history"); renderHistory(); } catch (e) {}
  }

  // ---------- live connection ----------
  let retry = 0;
  function setConn(s, label) {
    $("conn").dataset.state = s;
    $("conn").lastElementChild.textContent = label;
  }

  function connect() {
    if (!token) { setConn("offline", "No token"); $("status-line").textContent = "Open the link from your terminal to connect."; return; }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
    ws.onopen = () => { retry = 0; setConn("online", "Connected"); };
    ws.onclose = (ev) => {
      if (ev.code === 4401) { setConn("offline", "Bad token"); $("token-banner").hidden = false; return; }
      setConn("offline", "Offline");
      $("status-line").textContent = "Agent not running. Reconnecting…";
      setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
    };
    ws.onmessage = (msg) => {
      let e; try { e = JSON.parse(msg.data); } catch (_) { return; }
      handle(e);
    };
  }

  function handle(e) {
    switch (e.type) {
      case "snapshot":
        state = Object.assign(state, e.state);
        $("feed").replaceChildren();
        (e.state.narration || []).forEach((n) => addNarration(n, false));
        renderAll();
        break;
      case "status": state.status = e.state; renderStatus(); break;
      case "config": state.config = e.config; renderConfig(); renderStatus(); break;
      case "task_started":
        state.task = { id: e.id, instruction: e.instruction, index: e.index, total: e.total, step: 0, max_steps: e.max_steps || 15, action: null, started: e.ts };
        state.status = "working";
        renderTask(); renderStatus();
        break;
      case "step":
        if (state.task) { state.task.step = e.step; state.task.action = e.action; renderTask(); }
        break;
      case "task_finished":
        state.task = null; renderTask(); renderStatus();
        setTimeout(loadHistory, 150);
        break;
      case "queue": state.queue = e.items; renderQueue(); break;
      case "narration": addNarration({ text: e.text, ts: e.ts }, true); break;
      case "gesture": state.gesture = { name: e.name, ts: e.ts }; renderGesture(); break;
      case "voice": state.voice = { state: e.state, heard: e.heard }; renderVoice(); break;
    }
  }

  // ---------- commands ----------
  async function send(name, text) {
    try {
      await api("/api/command", { name, text: text || "" });
      return true;
    } catch (err) {
      $("status-line").textContent = `Couldn't send that: ${err.message}`;
      return false;
    }
  }

  $("command-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("command-input");
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    if (await send("submit", text)) {
      input.value = "";
      if (!state.task) $("status-line").textContent = state.status === "idle" ? "Sent. Starting shortly…" : "Queued.";
    }
  });
  document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
    $("command-input").value = c.dataset.task;
    $("command-input").focus();
  }));
  $("pause-btn").addEventListener("click", () => send(state.status === "paused" ? "resume" : "pause"));
  $("cancel-btn").addEventListener("click", () => send("cancel"));

  // Browser speech input (Chrome / Edge / Safari) as an alternative to the wake word.
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) {
    const mic = $("mic-btn");
    mic.hidden = false;
    let rec = null;
    mic.addEventListener("click", () => {
      if (rec) { rec.stop(); return; }
      rec = new SR();
      rec.lang = navigator.language || "en-US";
      rec.interimResults = true;
      rec.onresult = (ev) => {
        $("command-input").value = Array.from(ev.results).map((r) => r[0].transcript).join("");
      };
      rec.onend = () => {
        mic.classList.remove("listening");
        rec = null;
        if ($("command-input").value.trim()) $("command-form").requestSubmit();
      };
      rec.onerror = () => { mic.classList.remove("listening"); };
      mic.classList.add("listening");
      rec.start();
    });
  }

  connect();
  loadHistory();
})();
