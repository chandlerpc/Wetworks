// Wetworks ops console. Polls the Command API; no framework, no build step.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
    del(k) { try { localStorage.removeItem(k); } catch {} },
  };

  let token = store.get("wetworks.token") || "";
  let channel = store.get("wetworks.channel") || "clair";
  let lastEvent = 0;
  const lastMsg = { clair: 0, architect: 0 };
  let topicFilter = "all";
  let findingsCache = [];
  const timers = [];

  const NAMES = { architect: "The Architect", clair: "clAIr", intel: "Intel", splinter: "Splinter", patrick: "You", console: "You" };
  const STATUS = { queued: "Queued", running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled" };
  let openMission = null;
  const JOBS = {
    morning_brief: "Morning brief",
    evening_brief: "Evening brief",
  };

  // ---- API ------------------------------------------------------------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json", ...(opts.headers || {}) },
    });
    if (res.status === 401) { lock("That access code didn't work."); throw new Error("unauthorized"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  // ---- Time helpers ---------------------------------------------------
  const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  function when(iso) {
    const d = new Date(iso), now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const tmr = new Date(now); tmr.setDate(now.getDate() + 1);
    if (sameDay) return `today ${clock(iso)}`;
    if (d.toDateString() === tmr.toDateString()) return `tomorrow ${clock(iso)}`;
    return d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  }
  function ago(iso) {
    const m = Math.round((Date.now() - new Date(iso)) / 60000);
    if (m < 1) return "just now";
    if (m < 60) return `${m} min ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h} h ago`;
    return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
  }
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  // ---- Status ---------------------------------------------------------
  async function loadStatus() {
    const s = await api("/api/status");
    const { month_to_date: spent, budget } = s.spend;
    $("#ro-spend").textContent = `$${spent.toFixed(2)} of $${budget.toFixed(0)}`;
    const pct = Math.min(100, (spent / budget) * 100);
    const bar = $("#ro-spend-bar");
    bar.style.width = `${pct}%`;
    bar.className = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";

    const free = s.provider === "local";
    $("#ro-model").textContent = free ? `${s.model} · free` : `${s.model} · paid`;
    const h = s.host;
    $("#ro-host").textContent = `CPU ${h.cpu_percent}%\nRAM ${h.mem_used_gb} of ${h.mem_total_gb} GB\nDisk ${h.disk_free_gb} GB free\nUp ${h.uptime_hours} h`;

    // Splinter teams work under The Architect, so they light his lens
    const busy = (a) => s.busy.includes(a) || (a === "architect" && s.busy.includes("splinter"));
    $$(".topbar .lens").forEach((l) => {
      l.classList.toggle("busy", busy(l.dataset.agent));
      l.classList.toggle("on", !busy(l.dataset.agent));
    });
    const working = s.busy.map((b) => NAMES[b] || b);
    $("#statusline").textContent = working.length ? `${working.join(" and ")} working` : `All quiet · ${s.intel_topics} Intel topics watched`;

    const sched = $("#schedule");
    sched.replaceChildren();
    s.schedule.slice(0, 4).forEach((j) => {
      const li = el("li");
      const label = JOBS[j.id] || (j.id.startsWith("intel_") ? "Intel sweep" : j.id);
      li.append(el("span", null, label), el("span", null, j.next_run ? when(j.next_run) : "paused"));
      sched.append(li);
    });
    if (!s.schedule.length) sched.append(el("li", null, "Nothing scheduled"));

    // Keep the comms typing hint honest even between message polls
    showTyping(s.busy.includes(channel));
  }

  // ---- Activity feed --------------------------------------------------
  async function loadEvents() {
    const { events } = await api(`/api/events?after=${lastEvent}`);
    const feed = $("#feed");
    if (!events.length) { if (!feed.children.length) feed.append(el("li", "empty", "No activity yet. Run an Intel sweep to wake things up.")); return; }
    $(".empty", feed)?.remove();
    const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
    events.forEach((e) => {
      const li = el("li", e.kind);
      li.append(el("span", "t", clock(e.ts)), el("span", "src", NAMES[e.source] || e.source), el("span", "msg", e.text));
      feed.append(li);
      lastEvent = Math.max(lastEvent, e.id);
    });
    while (feed.children.length > 300) feed.firstElementChild.remove();
    if (nearBottom || events.length === feed.children.length) feed.scrollTop = feed.scrollHeight;
  }

  // ---- Comms ----------------------------------------------------------
  function showTyping(on) {
    const t = $("#typing");
    t.hidden = !on;
    if (on) t.textContent = `${NAMES[channel]} is replying`;
  }

  function selectChannel(c) {
    channel = c;
    store.set("wetworks.channel", c);
    $$(".channel").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.channel === c)));
    $("#msg").placeholder = `Message ${NAMES[c]}`;
    $("#thread").replaceChildren();
    lastMsg[c] = 0;
    loadMessages();
  }

  async function loadMessages() {
    const c = channel;
    const { messages, typing } = await api(`/api/comms/${c}?after=${lastMsg[c]}`);
    if (c !== channel) return;
    const thread = $("#thread");
    if (!messages.length && !thread.children.length) {
      const hint = c === "clair"
        ? "Ask clAIr what's new, or hand her something to pass along."
        : "Ask The Architect for a status read or what to focus on.";
      thread.append(el("p", "empty", hint));
    }
    if (messages.length) $(".empty", thread)?.remove();
    messages.forEach((m) => {
      const mine = m.sender === "patrick";
      const b = el("div", `msg ${mine ? "me" : "them"}`);
      b.append(el("span", "who", `${NAMES[m.sender] || m.sender} · ${clock(m.ts)}`), document.createTextNode(m.text));
      thread.append(b);
      lastMsg[c] = Math.max(lastMsg[c], m.id);
    });
    if (messages.length) thread.scrollTop = thread.scrollHeight;
    showTyping(typing);
  }

  async function send(e) {
    e.preventDefault();
    const box = $("#msg");
    const text = box.value.trim();
    if (!text) return;
    const btn = $("#compose button");
    btn.disabled = true;
    try {
      await api(`/api/comms/${channel}`, { method: "POST", body: JSON.stringify({ text }) });
      box.value = ""; autosize();
      showTyping(true);
      await loadMessages();
    } catch (err) {
      if (err.message !== "unauthorized") showTyping(false), alert(err.message);
    } finally { btn.disabled = false; box.focus(); }
  }

  function autosize() { const t = $("#msg"); t.style.height = "auto"; t.style.height = `${Math.min(t.scrollHeight, 140)}px`; }

  // ---- Intel ----------------------------------------------------------
  async function loadFindings() {
    const { findings } = await api("/api/findings");
    findingsCache = findings;
    const topics = [...new Set(findings.map((f) => f.topic))];
    const chips = $("#topic-filter");
    chips.replaceChildren();
    ["all", ...topics].forEach((t) => {
      const b = el("button", "chip", t === "all" ? "Everything" : t);
      b.type = "button";
      b.setAttribute("aria-pressed", String(t === topicFilter));
      b.addEventListener("click", () => { topicFilter = t; loadFindingsRender(); $$(".chip").forEach((c) => c.setAttribute("aria-pressed", String(c === b))); });
      chips.append(b);
    });
    loadFindingsRender();
  }

  function loadFindingsRender() {
    const list = $("#findings");
    list.replaceChildren();
    const rows = findingsCache.filter((f) => topicFilter === "all" || f.topic === topicFilter);
    if (!rows.length) { list.append(el("li", "finding", "No findings yet. Intel sweeps run before each brief, or start one from Ops.")); return; }
    rows.forEach((f) => {
      const li = el("li", `finding i${f.importance}`);
      const h = el("h4");
      if (f.url) { const a = el("a", null, f.title); a.href = f.url; a.target = "_blank"; a.rel = "noopener noreferrer"; h.append(a); }
      else h.textContent = f.title;
      const meta = el("div", "meta");
      const tag = f.importance >= 5 ? ["urgent", "Act today"] : f.importance === 4 ? ["soon", "This week"] : null;
      if (tag) { meta.append(el("span", tag[0], tag[1]), document.createTextNode(" · ")); }
      meta.append(document.createTextNode(`${f.topic} · ${ago(f.ts)}`));
      li.append(h, el("p", null, f.summary), meta);
      list.append(li);
    });
  }

  // ---- Missions -------------------------------------------------------
  async function loadMissions() {
    const { missions } = await api("/api/missions");
    const list = $("#missions");
    list.replaceChildren();
    if (!missions.length) { list.append(el("li", "note", "No missions yet. Describe one above, or ask clAIr in Comms.")); return; }
    missions.forEach((m) => {
      const li = el("li");
      const b = el("button", "mission");
      b.type = "button";
      b.append(el("span", `status-line ${m.status}`, `#${m.id} · ${STATUS[m.status] || m.status}${m.source && m.source !== "console" ? ` · via ${NAMES[m.source] || m.source}` : ""}`));
      b.append(el("span", "m-title", m.title), el("span", "m-when", ago(m.updated)));
      if (m.result_preview && m.status === "done") {
        const plain = m.result_preview.replace(/^#+\s*/gm, "").replace(/^[-*•]\s+/gm, "").replace(/\*\*/g, "").replace(/\s*\[\d+\]/g, "").replace(/\s+/g, " ").trim();
        b.append(el("span", "m-preview", plain));
      }
      b.addEventListener("click", () => showMission(m.id));
      li.append(b);
      list.append(li);
    });
    if (openMission) refreshSheet();
  }

  // Minimal, safe renderer for write-ups: headings, bullets, paragraphs, [n] citations, Next step.
  function renderResult(text, sources) {
    const box = $("#ms-result");
    box.replaceChildren();
    const byN = Object.fromEntries((sources || []).map((s) => [String(s.n), s]));
    const inline = (parent, str) => {
      str.split(/(\[\d+\])/g).forEach((part) => {
        const m = part.match(/^\[(\d+)\]$/);
        if (m && byN[m[1]]) {
          const sup = el("sup"); const a = el("a", null, `[${m[1]}]`);
          a.href = byN[m[1]].url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.title = byN[m[1]].title;
          sup.append(a); parent.append(sup);
        } else if (part) parent.append(document.createTextNode(part.replace(/\*\*(.+?)\*\*/g, "$1")));
      });
    };
    let ul = null;
    text.split("\n").forEach((raw) => {
      const line = raw.trim();
      if (!line) { ul = null; return; }
      if (/^#{1,4}\s/.test(line)) { ul = null; const h = el("h4"); inline(h, line.replace(/^#+\s*/, "")); box.append(h); return; }
      if (/^[-*•]\s+/.test(line)) { if (!ul) { ul = el("ul"); box.append(ul); } const li = el("li"); inline(li, line.replace(/^[-*•]\s+/, "")); ul.append(li); return; }
      ul = null;
      const p = el("p", /^next step:/i.test(line) ? "next" : null);
      inline(p, line);
      box.append(p);
    });
  }

  async function refreshSheet() {
    if (!openMission) return;
    const m = await api(`/api/missions/${openMission}`);
    const st = $("#ms-status");
    st.className = `status-line ${m.status}`;
    st.textContent = `Mission #${m.id} · ${STATUS[m.status] || m.status}`;
    $("#ms-title").textContent = m.title;
    $("#ms-request").textContent = m.request;
    if (m.status === "done" || m.status === "failed") renderResult(m.result || "", m.sources);
    else $("#ms-result").replaceChildren(el("p", "pending", m.status === "running" ? "A Splinter team is on it. This page updates as it works." : m.status === "queued" ? "Waiting for the current mission to finish." : "This mission was cancelled."));
    const src = $("#ms-sources");
    src.replaceChildren();
    (m.status === "done" ? m.sources : []).forEach((s) => {
      const li = el("li"); const a = el("a", null, s.title || s.url);
      a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      li.append(a); src.append(li);
    });
    const steps = $("#ms-steps");
    steps.replaceChildren(...m.steps.map((x) => el("li", null, `${clock(x.ts)}  ${x.text}`)));
    $(".steps-wrap").hidden = !m.steps.length;
    $("#ms-cancel").hidden = !(m.status === "queued" || m.status === "running");
  }

  async function showMission(id) {
    openMission = id;
    await refreshSheet();
    const d = $("#mission-sheet");
    if (!d.open) d.showModal();
  }

  async function startMission(e) {
    e.preventDefault();
    const box = $("#mission-text");
    const request = box.value.trim();
    if (!request) { box.focus(); return; }
    const btn = $("#new-mission button");
    btn.disabled = true;
    try {
      const m = await api("/api/missions", { method: "POST", body: JSON.stringify({ request }) });
      box.value = "";
      await loadMissions();
      showMission(m.id);
    } catch (err) {
      if (err.message !== "unauthorized") alert(err.message);
    } finally { btn.disabled = false; }
  }

  // ---- Actions --------------------------------------------------------
  async function runAction(btn) {
    const note = $("#action-note");
    btn.disabled = true;
    note.className = "note";
    try {
      await api(`/api/actions/${btn.dataset.action}`, { method: "POST" });
      note.textContent = btn.dataset.action === "intel"
        ? "Intel sweep started. Findings show up in Intel when it's done."
        : "Brief is being compiled and will land in Discord shortly.";
      setTimeout(loadEvents, 800);
    } catch (err) {
      if (err.message !== "unauthorized") { note.textContent = err.message; note.className = "note err"; }
    } finally { setTimeout(() => (btn.disabled = false), 3000); }
  }

  // ---- Navigation -----------------------------------------------------
  function showPane(name) {
    $$(".pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${name}`));
    $$(".tabs button").forEach((b) => { if (b.dataset.pane === name) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
    store.set("wetworks.pane", name);
    if (name === "comms") setTimeout(() => { const t = $("#thread"); t.scrollTop = t.scrollHeight; }, 0);
  }

  // ---- Session --------------------------------------------------------
  const safe = (fn) => () => fn().catch((e) => { if (e.message !== "unauthorized") $("#statusline").textContent = "Lost contact with the MSI. Retrying…"; });

  function start() {
    $("#gate").hidden = true;
    $("#app").hidden = false;
    $("#tabs").hidden = false;
    $("#logout").hidden = false;
    showPane(store.get("wetworks.pane") || "ops");
    selectChannel(channel);
    const s = safe(loadStatus), ev = safe(loadEvents), m = safe(loadMessages), f = safe(loadFindings), mi = safe(loadMissions);
    s(); ev(); f(); mi();
    timers.push(setInterval(s, 8000), setInterval(ev, 4000), setInterval(m, 2500), setInterval(f, 30000), setInterval(mi, 5000));
  }

  function lock(message = "") {
    timers.splice(0).forEach(clearInterval);
    token = ""; store.del("wetworks.token");
    $("#app").hidden = true; $("#tabs").hidden = true; $("#logout").hidden = true;
    $("#gate").hidden = false;
    $("#gate-error").textContent = message;
    $("#statusline").textContent = "Locked";
    $("#code").focus();
  }

  async function unlock(e) {
    e.preventDefault();
    token = $("#code").value.trim();
    $("#gate-error").textContent = "";
    try {
      await api("/api/status");
      store.set("wetworks.token", token);
      $("#code").value = "";
      start();
    } catch (err) {
      if (err.message !== "unauthorized") $("#gate-error").textContent = "Can't reach the MSI. Check that Tailscale is on.";
    }
  }

  // ---- Wire up --------------------------------------------------------
  $("#gate-form").addEventListener("submit", unlock);
  $("#logout").addEventListener("click", () => lock());
  $("#compose").addEventListener("submit", send);
  $("#msg").addEventListener("input", autosize);
  $("#msg").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && window.matchMedia("(min-width: 1000px)").matches) { e.preventDefault(); $("#compose").requestSubmit(); } });
  $$(".channel").forEach((b) => b.addEventListener("click", () => selectChannel(b.dataset.channel)));
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showPane(b.dataset.pane)));
  $$("[data-action]").forEach((b) => b.addEventListener("click", () => runAction(b)));
  $("#new-mission").addEventListener("submit", startMission);
  $("#ms-close").addEventListener("click", () => $("#mission-sheet").close());
  $("#mission-sheet").addEventListener("close", () => { openMission = null; });
  $("#mission-sheet").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });
  $("#ms-cancel").addEventListener("click", async () => {
    if (!openMission) return;
    try { await api(`/api/missions/${openMission}/cancel`, { method: "POST" }); await refreshSheet(); loadMissions(); }
    catch (err) { if (err.message !== "unauthorized") alert(err.message); }
  });

  if (token) api("/api/status").then(start).catch(() => {}); else lock();
})();
