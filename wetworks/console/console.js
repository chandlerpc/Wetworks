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

  const NAMES = { architect: "The Architect", clair: "C.L.a.i.R", intel: "Intel", revenue: "Revenue", learner: "Learner", splinter: "Splinter", patrick: "You", console: "You" };
  const STATUS = { queued: "Queued", running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled" };
  let openMission = null;
  const JOBS = {
    morning_brief: "Morning brief",
    evening_brief: "Evening brief",
    commander_cycle: "Architect check-in",
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
    const busy = (a) => s.busy.includes(a) || (a === "architect" && s.busy.includes("splinter"))
      || (a === "intel" && (s.busy.includes("revenue") || s.busy.includes("learner")));  // Sleepers share the right-hand lens
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
      const prefix = { intel_: "Intel sweep", revenue_: "Revenue hunt", learner_: "Nightly learning" };
      const label = JOBS[j.id] || Object.entries(prefix).find(([k]) => j.id.startsWith(k))?.[1] || j.id;
      li.append(el("span", null, label), el("span", null, j.next_run ? when(j.next_run) : "paused"));
      sched.append(li);
    });
    if (!s.schedule.length) sched.append(el("li", null, "Nothing scheduled"));

    clairMode = s.clair || "local";
    updateLinkNote();

    // Keep the comms typing hint honest even between message polls
    showTyping(s.busy.includes(channel));
  }

  // ---- Activity feed --------------------------------------------------
  async function loadEvents() {
    const { events } = await api(`/api/events?after=${lastEvent}`);
    const feed = $("#feed");
    if (!events.length) { if (!feed.children.length) feed.append(el("li", "empty", "No activity yet. Run an Intel sweep to wake things up.")); return; }
    $(".empty", feed)?.remove();
    const atTop = feed.scrollTop < 40;
    // Newest on top: events arrive oldest-first, so insert each one above the previous.
    events.forEach((e) => {
      const li = el("li", e.kind);
      li.append(el("span", "t", clock(e.ts)), el("span", "src", NAMES[e.source] || e.source), el("span", "msg", e.text));
      feed.prepend(li);
      lastEvent = Math.max(lastEvent, e.id);
    });
    while (feed.children.length > 300) feed.lastElementChild.remove();
    if (atTop) feed.scrollTop = 0;
  }

  // ---- Comms ----------------------------------------------------------
  // Pin the thread to the bottom unless the reader has scrolled up to read history.
  let stick = true;
  const toBottom = () => { const t = $("#thread"); t.scrollTop = t.scrollHeight; };
  function setTopbarHeight() {
    document.documentElement.style.setProperty("--topbar-h", `${$(".topbar").offsetHeight}px`);
  }

  function showTyping(on) {
    const t = $("#typing");
    const was = !t.hidden;
    t.hidden = !on;
    if (on) t.textContent = `${NAMES[channel]} is replying`;
    if (on !== was && stick) requestAnimationFrame(toBottom);
  }

  let clairMode = "local";
  const LINK = {
    cloudflare: "Connected to C.L.a.i.R on Cloudflare",
    fallback: "Cloudflare unreachable, so C.L.a.i.R is answering locally",
    local: "C.L.a.i.R is running locally on the MSI",
  };
  function updateLinkNote() {
    const n = $("#clair-link");
    n.hidden = channel !== "clair";
    const vb = $("#voicebar"); if (vb && vb.dataset.ready) vb.hidden = channel !== "clair";
    n.className = `link-note ${clairMode}`;
    n.textContent = LINK[clairMode] || LINK.local;
  }

  function selectChannel(c) {
    channel = c;
    store.set("wetworks.channel", c);
    $$(".channel").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.channel === c)));
    $("#msg").placeholder = `Message ${NAMES[c]}`;
    updateLinkNote();
    $("#thread").replaceChildren();
    lastMsg[c] = 0;
    stick = true;
    loadMessages();
  }

  async function loadMessages() {
    const c = channel;
    const { messages, typing } = await api(`/api/comms/${c}?after=${lastMsg[c]}`);
    if (c !== channel) return;
    const thread = $("#thread");
    if (!messages.length && !thread.children.length) {
      const hint = c === "clair"
        ? "Ask C.L.a.i.R what's new, or hand her something to pass along."
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
    showTyping(typing);
    if (messages.length && stick) requestAnimationFrame(toBottom);
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
      stick = true;
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
    if (!missions.length) { list.append(el("li", "note", "No missions yet. Describe one above, or ask C.L.a.i.R in Comms.")); return; }
    missions.forEach((m) => {
      const li = el("li");
      const b = el("button", "mission");
      b.type = "button";
      const kindTag = m.kind === "code" ? " · Code" : "";
      b.append(el("span", `status-line ${m.status}`, `#${m.id} · ${STATUS[m.status] || m.status}${kindTag}${m.source && m.source !== "console" ? ` · via ${NAMES[m.source] || m.source}` : ""}`));
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

  let missionKind = "research";
  const KIND_HINT = {
    research: "What should a Splinter team do? For example: find three Orlando clinics that might pay for AI intake automation",
    code: "What should change in Wetworks? For example: add a button on Ops that runs the morning brief. You'll get a pull request to review.",
  };
  function setKind(k) {
    missionKind = k;
    $$(".kind").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.kind === k)));
    $("#mission-text").placeholder = KIND_HINT[k];
  }

  async function startMission(e) {
    e.preventDefault();
    const box = $("#mission-text");
    const request = box.value.trim();
    if (!request) { box.focus(); return; }
    const btn = $("#new-mission button");
    btn.disabled = true;
    try {
      const m = await api("/api/missions", { method: "POST", body: JSON.stringify({ request, kind: missionKind }) });
      box.value = "";
      await loadMissions();
      showMission(m.id);
    } catch (err) {
      if (err.message !== "unauthorized") alert(err.message);
    } finally { btn.disabled = false; }
  }

  // ---- Approvals ------------------------------------------------------
  async function loadApprovals() {
    const { pending } = await api("/api/approvals");
    const box = $("#needs-you");
    box.hidden = !pending.length;
    const list = $("#approvals");
    list.replaceChildren();
    pending.forEach((a) => {
      const li = el("li");
      li.append(el("p", "what", `#${a.id} ${a.summary}`));
      li.append(el("p", "why", `${NAMES[a.agent] || a.agent} asks${a.reason ? `: ${a.reason}` : "."}`));
      const row = el("div", "btnrow");
      const yes = el("button", "small", "Approve"); yes.type = "button";
      const no = el("button", "ghost small", "Decline"); no.type = "button";
      const decide = async (decision) => {
        yes.disabled = no.disabled = true;
        try {
          const r = await api(`/api/approvals/${a.id}`, { method: "POST", body: JSON.stringify({ decision }) });
          if (r.status === "failed") alert(`That didn't work: ${r.result}`);
        } catch (err) { if (err.message !== "unauthorized") alert(err.message); }
        safe(loadApprovals)(); safe(loadEvents)();
      };
      yes.addEventListener("click", () => decide("approve"));
      no.addEventListener("click", () => decide("decline"));
      row.append(yes, no);
      li.append(row);
      list.append(li);
    });
    const tab = $(".tabs button[data-pane=ops]");
    if (tab) tab.textContent = pending.length ? `Ops (${pending.length})` : "Ops";
  }

  // ---- Memory ---------------------------------------------------------
  const SRC = { profile: "Profile", console: "You, here", nightly: "Learned overnight", "chat:clair": "From C.L.a.i.R chat", "chat:architect": "From The Architect chat" };
  async function loadMemory() {
    const q = $("#mem-q").value.trim();
    const { memories, count } = await api(`/api/memory${q ? `?q=${encodeURIComponent(q)}` : ""}`);
    $("#mem-count").textContent = `${count} thing${count === 1 ? "" : "s"} remembered`;
    const list = $("#mem-list");
    list.replaceChildren();
    const rows = q ? memories : memories.filter((m) => !m.pinned).slice(0, 25);
    if (!rows.length) {
      list.append(el("li", "note", q ? "Nothing matches that." : "Nothing learned yet beyond your profile. It learns from chats overnight, or teach it below."));
      return;
    }
    rows.forEach((m) => {
      const li = el("li");
      const t = el("span", "mem-text", m.text);
      t.append(el("span", `mem-src${m.pinned ? " pin" : ""}`, `${SRC[m.source] || m.source} · ${ago(m.created)}`));
      li.append(t);
      if (!m.pinned) {
        const b = el("button", "ghost", "Forget"); b.type = "button";
        b.addEventListener("click", async () => {
          b.disabled = true;
          try { await api(`/api/memory/${m.id}`, { method: "DELETE" }); safe(loadMemory)(); }
          catch (err) { if (err.message !== "unauthorized") alert(err.message); b.disabled = false; }
        });
        li.append(b);
      }
      list.append(li);
    });
  }
  async function addMemory(e) {
    e.preventDefault();
    const box = $("#mem-text");
    const text = box.value.trim();
    if (!text) return;
    try { await api("/api/memory", { method: "POST", body: JSON.stringify({ text }) }); box.value = ""; safe(loadMemory)(); }
    catch (err) { if (err.message !== "unauthorized") alert(err.message); }
  }

  // ---- Leads ----------------------------------------------------------
  const LEAD_STATUS = { new: "New", contacted: "Contacted", replied: "Replied", won: "Won", lost: "Lost", dismissed: "Not a fit" };
  let leadFilter = "";
  async function loadLeads() {
    const { leads, counts, due } = await api(`/api/leads${leadFilter ? `?status=${leadFilter}` : ""}`);
    const c = (k) => counts[k] || 0;
    $("#lead-counts").textContent = `${c("new")} new · ${c("contacted")} contacted · ${c("replied")} replied · ${c("won")} won`
      + (due ? ` · ${due} to follow up` : "");
    const tab = $(".tabs button[data-pane=leads]");
    if (tab) tab.textContent = due ? `Leads (${due})` : "Leads";
    const chips = $("#lead-filter");
    if (!chips.children.length) {
      [["", "Active"], ["due", "Follow up"], ["new", "New"], ["contacted", "Contacted"], ["replied", "Replied"], ["won", "Won"], ["lost", "Lost"]].forEach(([k, label]) => {
        const b = el("button", "chip", label);
        b.type = "button";
        b.setAttribute("aria-pressed", String(k === leadFilter));
        b.addEventListener("click", () => {
          leadFilter = k;
          $$("#lead-filter .chip").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
          safe(loadLeads)();
        });
        chips.append(b);
      });
    }
    const list = $("#leads");
    const open = new Set($$("#leads details[open]").map((d) => d.dataset.id));
    list.replaceChildren();
    if (!leads.length) {
      list.append(el("li", "note", leadFilter ? "Nothing here yet." : "No leads yet. The Revenue cell hunts every morning before the brief, or tap Hunt now."));
      return;
    }
    leads.forEach((l) => list.append(leadItem(l, open.has(String(l.id)))));
  }

  async function leadAction(id, action, btn, body) {
    btn.disabled = true;
    try { await api(`/api/leads/${id}/${action}`, { method: "POST", body: JSON.stringify(body || {}) }); await loadLeads(); }
    catch (err) { if (err.message !== "unauthorized") alert(err.message); btn.disabled = false; }
  }

  function leadItem(l, isOpen) {
    const li = el("li", "lead");
    const top = el("div", "lead-top");
    const h = el("h4");
    if (l.url) { const a = el("a", null, l.business); a.href = l.url; a.target = "_blank"; a.rel = "noopener noreferrer"; h.append(a); }
    else h.textContent = l.business;
    top.append(h, el("span", `fit${l.fit >= 4 ? " hot" : ""}`, `Fit ${l.fit}/5`));
    li.append(top);
    if (l.signal) li.append(el("p", "signal", l.signal));
    const meta = el("div", "meta");
    if (l.offer) { meta.append(el("span", "offer", l.offer), document.createTextNode(" · ")); }
    meta.append(document.createTextNode([l.location, ago(l.created)].filter(Boolean).join(" · ")));
    li.append(meta);

    const actions = el("div", "lead-actions");
    const sel = el("select");
    sel.setAttribute("aria-label", `Status for ${l.business}`);
    Object.entries(LEAD_STATUS).forEach(([k, label]) => { const o = el("option", null, label); o.value = k; o.selected = k === l.status; sel.append(o); });
    sel.addEventListener("change", async () => {
      try { await api(`/api/leads/${l.id}`, { method: "POST", body: JSON.stringify({ status: sel.value }) }); safe(loadLeads)(); }
      catch (err) { if (err.message !== "unauthorized") alert(err.message); }
    });
    actions.append(sel);
    li.append(actions);

    // follow-ups: Wetworks reminds and drafts; Patrick sends
    if (l.status === "contacted" && l.followup_due) {
      const isDue = new Date(l.followup_due) <= new Date();
      const fu = el("div", `followup${isDue ? " due" : ""}`);
      fu.append(el("span", "fu-label", isDue ? `Follow-up ${l.followups + 1} due`
        : `Follow-up ${l.followups + 1} on ${new Date(l.followup_due).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}`));
      const done = el("button", "small", "I followed up"); done.type = "button";
      const later = el("button", "ghost small", "Snooze 3 days"); later.type = "button";
      done.addEventListener("click", () => leadAction(l.id, "followed-up", done));
      later.addEventListener("click", () => leadAction(l.id, "snooze", later, { days: 3 }));
      fu.append(done, later);
      li.append(fu);
      if (l.followup_draft) {
        const fd = el("details"); fd.dataset.id = `fu-${l.id}`; fd.open = isOpen;
        fd.append(el("summary", null, "Follow-up draft"), el("div", "draft", l.followup_draft));
        const cp = el("button", "small", "Copy follow-up"); cp.type = "button";
        cp.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(l.followup_draft); cp.textContent = "Copied"; } catch { cp.textContent = "Select and copy"; }
          setTimeout(() => (cp.textContent = "Copy follow-up"), 2000);
        });
        const r = el("div", "lead-actions"); r.append(cp); fd.append(r);
        li.append(fd);
      }
    } else if (l.status === "contacted" && l.followups) {
      li.append(el("p", "meta", `Followed up ${l.followups} time${l.followups > 1 ? "s" : ""}. No more nudges planned.`));
    }

    // research a Splinter did before outreach
    const rs = el("details"); rs.dataset.id = `rs-${l.id}`; rs.open = isOpen && !!l.research;
    const researching = l.research_mission && !l.research;
    rs.append(el("summary", null, researching ? `Researching… (mission #${l.research_mission})` : l.research ? "Research" : "Research this lead"));
    if (l.research) rs.append(el("div", "draft research", l.research));
    if (!researching) {
      const go = el("button", "ghost small", l.research ? "Research again" : "Send a Splinter"); go.type = "button";
      go.addEventListener("click", () => leadAction(l.id, "research", go));
      const r = el("div", "lead-actions"); r.append(go); rs.append(r);
    }
    li.append(rs);

    const det = el("details");
    det.dataset.id = l.id;
    det.open = isOpen;
    det.append(el("summary", null, l.draft ? "Outreach draft" : "Write an outreach draft"));
    if (l.draft) det.append(el("div", "draft", l.draft));
    const row = el("div", "lead-actions");
    if (l.draft) {
      const copy = el("button", "small", "Copy draft"); copy.type = "button";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(l.draft); copy.textContent = "Copied"; } catch { copy.textContent = "Select and copy"; }
        setTimeout(() => (copy.textContent = "Copy draft"), 2000);
      });
      row.append(copy);
    }
    const redo = el("button", "ghost small", l.draft ? "Rewrite" : "Write draft"); redo.type = "button";
    redo.addEventListener("click", async () => {
      redo.disabled = true; redo.textContent = "Writing…";
      try { await api(`/api/leads/${l.id}/draft`, { method: "POST" }); await loadLeads(); }
      catch (err) { if (err.message !== "unauthorized") alert(err.message); redo.disabled = false; redo.textContent = "Try again"; }
    });
    row.append(redo);
    det.append(row);
    const notes = el("input", "notes");
    notes.placeholder = "Notes (who you spoke to, follow-up date…)";
    notes.value = l.notes || "";
    notes.setAttribute("aria-label", `Notes for ${l.business}`);
    notes.addEventListener("change", () => api(`/api/leads/${l.id}`, { method: "POST", body: JSON.stringify({ notes: notes.value }) }).catch(() => {}));
    det.append(notes);
    li.append(det);
    return li;
  }

  // ---- Finance (local only) -------------------------------------------
  let finCat = "", finCats = [];
  const usd = (v) => `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const shortDate = (d) => new Date(`${d}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" });
  async function loadFinance() {
    const f = await api("/api/finance");
    finCats = f.categories || [];
    const m = f.month_detail;
    const box = $("#fin-month"); box.replaceChildren();
    if (!f.count) {
      box.append(el("p", "note", "No transactions yet. On your bank's or card's website, download transactions as CSV, "
        + "type the account name above, then Import CSV. Re-importing the same file is safe: duplicates are skipped."));
    } else {
      const head = el("div", "fin-totals");
      head.append(el("div", null, ""), el("div", null, ""));
      head.children[0].append(el("span", "fh-label", "Spent this month"), el("strong", "fin-big", usd(m.spent)));
      head.children[1].append(el("span", "fh-label", "Income"), el("strong", "fin-big in", usd(m.income)));
      box.append(head);
      m.by_category.forEach((c) => {
        const row = el("div", "fin-cat");
        const pct = c.budget ? Math.min(100, (c.spent / c.budget) * 100) : 0;
        row.append(el("span", null, c.category), el("span", "mono", c.budget ? `${usd(c.spent)} of ${usd(c.budget)}` : usd(c.spent)));
        const bar = el("div", "meter"); const fill = el("span"); fill.style.width = `${c.budget ? pct : 0}%`;
        fill.className = c.budget && c.spent >= c.budget ? "over" : c.budget && c.spent >= 0.8 * c.budget ? "warn" : "";
        bar.append(fill); row.append(bar);
        row.addEventListener("click", () => { finCat = c.category; safe(loadFinanceTx)(); });
        box.append(row);
      });
      if (f.wetworks_ai) box.append(el("p", "note", `Wetworks' own AI spend this month: ${usd(f.wetworks_ai)} (tracked separately).`));
      if (f.uncategorized) box.append(el("p", "note warn", `${f.uncategorized} merchant${f.uncategorized > 1 ? "s" : ""} not sorted yet. The local model sorts them each morning, or set them below.`));
    }
    const up = $("#fin-upcoming"); up.replaceChildren();
    const soon = (f.recurring || []).filter((r) => r.active && (new Date(r.next) - new Date()) / 864e5 <= 30);
    if (!soon.length) up.append(el("li", "note", f.count ? "Nothing expected." : ""));
    soon.forEach((r) => { const li = el("li"); li.append(el("span", "mono", shortDate(r.next)), el("span", null, r.label), el("span", "mono", usd(r.amount))); up.append(li); });
    const rec = $("#fin-recurring"); rec.replaceChildren();
    $("#fin-rec-total").textContent = f.recurring_monthly_total ? `· about ${usd(f.recurring_monthly_total)} a month` : "";
    (f.recurring || []).forEach((r) => {
      const li = el("li", r.active ? "" : "stale");
      li.append(el("span", null, r.label), el("span", "muted", `${r.cadence}${r.active ? "" : " · stopped?"}`), el("span", "mono", usd(r.amount)));
      rec.append(li);
    });
    const chips = $("#fin-filter");
    if (chips.children.length !== finCats.length + 1) {
      chips.replaceChildren();
      [["", "All"], ...finCats.map((c) => [c, c])].forEach(([k, label]) => {
        const b = el("button", "chip", label); b.type = "button"; b.dataset.cat = k;
        b.addEventListener("click", () => { finCat = k; safe(loadFinanceTx)(); });
        chips.append(b);
      });
    }
    const bl = $("#fin-batches"); bl.replaceChildren();
    (f.imports || []).forEach((b) => {
      const li = el("li");
      li.append(el("span", null, `${b.account} · ${b.added} added${b.skipped ? `, ${b.skipped} already there` : ""}${b.flipped ? " · signs flipped" : ""}`),
        el("span", "muted", ago(b.ts)));
      const u = el("button", "ghost small", "Undo"); u.type = "button";
      u.addEventListener("click", async () => { u.disabled = true; await api(`/api/finance/imports/${b.id}`, { method: "DELETE" }); safe(loadFinance)(); });
      li.append(u); bl.append(li);
    });
    await loadFinanceTx();
  }
  async function loadFinanceTx() {
    $$("#fin-filter .chip").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.cat === finCat)));
    const { transactions } = await api(`/api/finance/transactions?limit=150${finCat ? `&category=${encodeURIComponent(finCat)}` : ""}`);
    const ul = $("#fin-tx"); ul.replaceChildren();
    transactions.forEach((t) => {
      const li = el("li", t.amount > 0 ? "in" : "");
      const sel = el("select"); sel.setAttribute("aria-label", `Category for ${t.description}`);
      finCats.forEach((c) => { const o = el("option", null, c); o.value = c; o.selected = c === t.category; sel.append(o); });
      if (t.cat_source === "none") sel.classList.add("unsorted");
      sel.addEventListener("change", async () => {
        try { await api(`/api/finance/transactions/${t.id}`, { method: "POST", body: JSON.stringify({ category: sel.value, remember: true }) }); safe(loadFinance)(); }
        catch (err) { if (err.message !== "unauthorized") alert(err.message); }
      });
      li.append(el("span", "mono", shortDate(t.day)), el("span", "desc", t.description), el("span", "mono amt", usd(t.amount)), sel);
      ul.append(li);
    });
    if (!transactions.length) ul.append(el("li", "note", finCat ? "Nothing in this category." : ""));
  }
  function bindFinance() {
    const file = $("#fin-file"), note = $("#fin-note");
    try { $("#fin-account").value = localStorage.getItem("wetworks.finAccount") || ""; } catch {}
    file.addEventListener("change", async () => {
      const f = file.files[0]; if (!f) return;
      const account = $("#fin-account").value.trim();
      try { localStorage.setItem("wetworks.finAccount", account); } catch {}
      note.textContent = "Importing…"; note.className = "note";
      try {
        const j = await api(`/api/finance/import?account=${encodeURIComponent(account)}&filename=${encodeURIComponent(f.name)}`,
          { method: "POST", headers: { "Content-Type": "text/csv" }, body: await f.text() });
        note.textContent = `Imported ${j.added} into ${j.account}${j.skipped ? ` (${j.skipped} already there)` : ""}`
          + (j.flipped ? ". This looked like a card statement, so purchases were counted as money out." : ".");
        safe(loadFinance)();
      } catch (err) { note.textContent = err.message; note.className = "note err"; }
      file.value = "";
    });
  }

  // ---- Actions --------------------------------------------------------
  async function runAction(btn) {
    const note = $("#action-note");
    btn.disabled = true;
    note.className = "note";
    try {
      await api(`/api/actions/${btn.dataset.action}`, { method: "POST" });
      note.textContent = {
        intel: "Intel sweep started. Findings show up in Intel when it's done.",
        revenue: "Revenue is hunting. New leads show up in Leads when it's done.",
        cycle: "The Architect is reviewing everything. His report appears in Activity.",
      }[btn.dataset.action] || "Brief is being compiled and will land in Discord shortly.";
      if (btn.closest("#pane-leads")) $("#lead-counts").textContent = "Hunting… new leads appear here in a few minutes.";
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
    if (name === "comms") { stick = true; setTopbarHeight(); requestAnimationFrame(toBottom); }
  }

  // ---- Session --------------------------------------------------------
  const safe = (fn) => () => fn().catch((e) => { if (e.message !== "unauthorized") $("#statusline").textContent = "Lost contact with the MSI. Retrying…"; });

  // ---- Views: Floor (isometric) or Tactical (panels). Desktop only; phones use the map + tabs.
  let view = "floor";
  try { view = localStorage.getItem("wetworks.view") || "floor"; } catch {}
  const wide = () => window.matchMedia("(min-width: 1000px)").matches;
  function setView(v) {
    view = v;
    try { localStorage.setItem("wetworks.view", v); } catch {}
    const floor = v === "floor" && wide();
    document.body.classList.toggle("view-floor", floor);
    $("#floor").hidden = !floor;
    $$("#viewswitch button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === v)));
    setTopbarHeight();
    if (window.WWFloor) (floor ? window.WWFloor.show() : window.WWFloor.hide());
  }

  function start() {
    $("#gate").hidden = true;
    $("#app").hidden = false;
    $("#tabs").hidden = false;
    $("#logout").hidden = false;
    showPane(store.get("wetworks.pane") || "ops");
    selectChannel(channel);
    const s = safe(loadStatus), ev = safe(loadEvents), m = safe(loadMessages), f = safe(loadFindings), mi = safe(loadMissions), le = safe(loadLeads);
    s(); ev(); f(); mi(); le(); safe(loadMemory)(); safe(loadFinance)();
    const ap = safe(loadApprovals); ap(); timers.push(setInterval(ap, 10000));
    timers.push(setInterval(le, 30000), setInterval(safe(loadFinance), 60000));
    if (window.WWVoice) window.WWVoice.init();
    if (window.WWFloor) {
      window.WWFloor.toTactical = (pane, channel, missionId) => {
        setView("tactical");
        if (channel) selectChannel(channel);
        if (missionId) showMission(missionId);
        showPane(pane);
      };
      window.WWFloor.start();
      $("#viewswitch").hidden = false;
      setView(view);
    }
    timers.push(setInterval(s, 8000), setInterval(ev, 4000), setInterval(m, 2500), setInterval(f, 30000), setInterval(mi, 5000));
  }

  function lock(message = "") {
    timers.splice(0).forEach(clearInterval);
    if (window.WWVoice) window.WWVoice.stop();
    document.body.classList.remove("view-floor");
    $("#floor").hidden = true; $("#viewswitch").hidden = true; $("#inspect").hidden = true;
    if (window.WWFloor) window.WWFloor.hide();
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

  // Hooks for voice.js
  window.WW = {
    api,
    token: () => token,
    refresh: () => safe(loadMessages)(),
    useClair: () => { if (channel !== "clair") selectChannel("clair"); },
  };

  // ---- Wire up --------------------------------------------------------
  $("#gate-form").addEventListener("submit", unlock);
  $("#logout").addEventListener("click", () => lock());
  $("#compose").addEventListener("submit", send);
  $("#msg").addEventListener("input", autosize);
  $("#msg").addEventListener("focus", () => { if (stick) setTimeout(toBottom, 300); }); // after the keyboard opens
  $("#thread").addEventListener("scroll", () => {
    const t = $("#thread");
    stick = t.scrollHeight - t.scrollTop - t.clientHeight < 60;
  }, { passive: true });
  window.addEventListener("resize", () => { setTopbarHeight(); if (stick) toBottom(); });
  setTopbarHeight();
  $("#msg").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && window.matchMedia("(min-width: 1000px)").matches) { e.preventDefault(); $("#compose").requestSubmit(); } });
  $$(".channel").forEach((b) => b.addEventListener("click", () => selectChannel(b.dataset.channel)));
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showPane(b.dataset.pane)));
  $$("[data-action]").forEach((b) => b.addEventListener("click", () => runAction(b)));
  $("#new-mission").addEventListener("submit", startMission);
  $$("#viewswitch button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  window.matchMedia("(min-width: 1000px)").addEventListener("change", () => { if (token) setView(view); });
  $("#mem-add").addEventListener("submit", addMemory);
  $("#fin-import").addEventListener("submit", (e) => e.preventDefault());
  bindFinance();
  $("#mem-search").addEventListener("submit", (e) => { e.preventDefault(); safe(loadMemory)(); });
  let memT = null;
  $("#mem-q").addEventListener("input", () => { clearTimeout(memT); memT = setTimeout(() => safe(loadMemory)(), 300); });
  $$(".kind").forEach((b) => b.addEventListener("click", () => setKind(b.dataset.kind)));
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
