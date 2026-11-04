// The Floor: an isometric ops-center view of Wetworks. Purely a window onto /api/floor;
// nothing here drives the work. Everything is drawn in code (no image assets).
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const TW = 64, TH = 32, WALL = 46;               // tile size and wall height (px at zoom 1)
  const C = {
    night: "#0A0F0B", hull: "#121A14", seam: "#243028", fog: "#7E8A80", paper: "#D7DFD5",
    phosphor: "#9BE37D", dim: "#3E5C33", amber: "#E8A33D", signal: "#FF4D3D", steel: "#6FA8B8", sepia: "#B39B72",
  };
  const VENDOR = { local: C.phosphor, google: "#7FB2E5", anthropic: C.amber, xai: "#C9C9C9", gated: "#E9C46A" };
  const VENDOR_NAME = { local: "Local model (free)", google: "Gemini", anthropic: "Claude", xai: "Grok", gated: "Approval-only model" };
  const STATE_NAME = { idle: "Idle", working: "Working", thinking: "Thinking", blocked: "Blocked", asleep: "Asleep" };

  // Room layout on the tile grid: [gx, gy, width, depth], tint, props.
  const LAYOUT = {
    intel:    { r: [0, 0, 5, 5],  tint: C.phosphor, props: ["radar", "desk"] },
    revenue:  { r: [6, 0, 6, 5],  tint: C.amber,    props: ["board", "table"] },
    ready:    { r: [13, 0, 6, 5], tint: C.steel,    props: ["lockers", "bench"] },
    archives: { r: [0, 6, 5, 5],  tint: C.sepia,    props: ["cabinets", "bunk"] },
    command:  { r: [6, 6, 6, 5],  tint: C.phosphor, props: ["table", "phone", "screens"] },
    comms:    { r: [13, 6, 4, 5], tint: "#7FB2E5",  props: ["booth"] },
    codebay:  { r: [0, 12, 5, 5], tint: C.steel,    props: ["terminals", "racks"] },
    forge:    { r: [6, 12, 6, 5], tint: C.signal,   props: ["gpu"] },
    dayjob:   { r: [13, 12, 2, 4], sealed: true },
    finance:  { r: [15, 12, 2, 4], sealed: true },
    gamedev:  { r: [17, 12, 2, 4], sealed: true },
    content:  { r: [19, 12, 2, 4], sealed: true },
  };
  // Where each agent stands (tile coords inside its room); splinters fill numbered slots.
  const HOME = { architect: [9, 8.2], clair: [15, 8.4], intel: [2.6, 2.2], revenue: [9, 2.6], learner: [2.4, 8.6], forge: [8.4, 14.6] };
  const BUNK = { intel: [1.2, 3.6], revenue: [7.2, 3.8], learner: [1.2, 9.6] };
  // Where they go to work (walk there when busy, back home when idle).
  const STATION = { architect: [8.4, 7.1], intel: [1.9, 3.0], revenue: [8.6, 1.3], learner: [3.4, 7.2], forge: [7.6, 13.9] };
  const SLOTS = { ready: [[14.4, 2], [16, 2], [17.6, 2], [14.4, 3.6], [16, 3.6], [17.6, 3.6]],
                  codebay: [[1.4, 13.6], [2.8, 13.6], [1.4, 15.2], [2.8, 15.2]] };

  let canvas, ctx, dpr = 1, view = { x: 0, y: 0, z: 1 }, data = null, hover = null, selected = null;
  let hits = [], t0 = performance.now(), raf = 0, poll = 0, dragging = null, lastApprovals = 0, sound = false;
  const motion = {};               // agent id -> current [gx, gy] (they walk toward their target)
  let pings = [], seenEvent = 0, lastFrame = 0, sparks = [];
  const timelines = {};            // mission id -> { at, mission }
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- projection ---------------------------------------------------------------------
  const iso = (gx, gy, h = 0) => [(gx - gy) * TW / 2, (gx + gy) * TH / 2 - h];
  function toScreen(gx, gy, h = 0) {
    const [x, y] = iso(gx, gy, h);
    return [x * view.z + view.x, y * view.z + view.y];
  }
  function fit() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    const xs = [iso(0, 17)[0], iso(21, 0)[0]], ys = [iso(0, 0, WALL)[1], iso(21, 17)[1]];
    const sw = xs[1] - xs[0], sh = ys[1] - ys[0];
    view.z = Math.min((w - 80) / sw, (h - 150) / sh, 1.6);
    view.x = w / 2 - ((xs[0] + xs[1]) / 2) * view.z;
    view.y = (h + 20) / 2 - ((ys[0] + ys[1]) / 2) * view.z;
  }

  // ---- drawing primitives ---------------------------------------------------------------
  function poly(pts, fill, stroke) {
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke(); }
  }
  function mix(hex, a) { // hex + alpha
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
  }
  // An isometric box: base at (gx, gy) size (w, d) in tiles, height h px.
  function box(gx, gy, w, d, h, top, left, right) {
    const a = toScreen(gx, gy, h), b = toScreen(gx + w, gy, h), c = toScreen(gx + w, gy + d, h), e = toScreen(gx, gy + d, h);
    const b0 = toScreen(gx + w, gy), c0 = toScreen(gx + w, gy + d), e0 = toScreen(gx, gy + d);
    poly([e, c, c0, e0], left);
    poly([b, c, c0, b0], right);
    poly([a, b, c, e], top);
    return [a, b, c, e];
  }

  // ---- rooms ----------------------------------------------------------------------------
  function drawRoom(id, now) {
    const L = LAYOUT[id], [gx, gy, w, d] = L.r;
    const room = data.rooms.find((r) => r.id === id) || { name: id };
    const corners = [toScreen(gx, gy), toScreen(gx + w, gy), toScreen(gx + w, gy + d), toScreen(gx, gy + d)];
    const isHover = hover && hover.type === "room" && hover.id === id;
    const isSel = selected && selected.type === "room" && selected.id === id;

    if (L.sealed) {
      poly(corners, "#0C110D", isHover || isSel ? C.amber : C.seam);
      // closed door on the back-right wall with hazard stripes
      const p = [toScreen(gx + 0.3, gy, 0), toScreen(gx + w - 0.3, gy, 0), toScreen(gx + w - 0.3, gy, WALL * 0.85), toScreen(gx + 0.3, gy, WALL * 0.85)];
      poly(p, "#15100A", C.amber);
      ctx.save(); ctx.beginPath(); p.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.clip();
      ctx.strokeStyle = mix(C.amber, 0.55); ctx.lineWidth = 4 * view.z;
      for (let k = -6; k < 12; k++) { const [x, y] = toScreen(gx + k * 0.35, gy, 0); ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + 30 * view.z, y - 40 * view.z); ctx.stroke(); }
      ctx.restore();
      // status LED over the door
      const [lx, ly] = toScreen(gx + w / 2, gy, WALL * 0.85 + 7);
      const on = reduced || Math.floor((now + gx * 300) / 900) % 2 === 0;
      ctx.fillStyle = on ? C.amber : mix(C.amber, 0.25); ctx.fillRect(lx - 2 * view.z, ly - 2 * view.z, 4 * view.z, 4 * view.z);
      label(room.name, gx + w / 2, gy + d / 2, C.amber, 0.75, "SEALED");
      hits.push({ type: "room", id, poly: corners });
      return;
    }

    // floor
    poly(corners, mix(L.tint, 0.06), null);
    ctx.strokeStyle = mix(L.tint, 0.08); ctx.lineWidth = 1;
    for (let i = 1; i < w; i++) { const [x1, y1] = toScreen(gx + i, gy), [x2, y2] = toScreen(gx + i, gy + d); ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); }
    for (let j = 1; j < d; j++) { const [x1, y1] = toScreen(gx, gy + j), [x2, y2] = toScreen(gx + w, gy + j); ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); }
    // back walls (top-left and top-right edges)
    const wl = [toScreen(gx, gy + d), toScreen(gx, gy), toScreen(gx, gy, WALL), toScreen(gx, gy + d, WALL)];
    const wr = [toScreen(gx, gy), toScreen(gx + w, gy), toScreen(gx + w, gy, WALL), toScreen(gx, gy, WALL)];
    poly(wl, "#111813", null); poly(wr, "#141C16", null);
    ctx.strokeStyle = mix(L.tint, isHover || isSel ? 0.9 : 0.35); ctx.lineWidth = isSel ? 2 : 1;
    ctx.beginPath(); ctx.moveTo(...wl[3]); ctx.lineTo(...wl[2]); ctx.lineTo(...wr[2]); ctx.stroke();
    poly(corners, null, isHover || isSel ? mix(L.tint, 0.8) : mix(L.tint, 0.18));

    // overhead lamp: a soft pool of light; red alert wash if someone in here is blocked
    const [px, py] = toScreen(gx + w / 2, gy + d / 2);
    const alert = data.agents.some((a) => a.room === id && a.state === "blocked");
    const busy = data.agents.some((a) => a.room === id && ["working", "thinking"].includes(a.state));
    const lamp = ctx.createRadialGradient(px, py, 4, px, py, 120 * view.z);
    const tone = alert ? C.signal : L.tint, pulse = alert && !reduced ? 0.5 + 0.5 * Math.sin(now / 260) : 1;
    lamp.addColorStop(0, mix(tone, (alert ? 0.16 : busy ? 0.1 : 0.05) * pulse)); lamp.addColorStop(1, mix(tone, 0));
    ctx.save(); ctx.beginPath(); corners.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.closePath(); ctx.clip();
    ctx.fillStyle = lamp; ctx.fillRect(px - 140 * view.z, py - 80 * view.z, 280 * view.z, 160 * view.z);
    ctx.restore();

    // forge heat glow tied to real GPU load
    if (id === "forge") {
      const util = (data.hud.gpu && data.hud.gpu.util) || 0, train = data.hud.gpu && data.hud.gpu.training;
      const heat = Math.max(util / 100, train ? 0.6 : 0) * (reduced ? 1 : 0.85 + 0.15 * Math.sin(now / 400));
      const [cx, cy] = toScreen(gx + w / 2, gy + d / 2);
      const g = ctx.createRadialGradient(cx, cy, 4, cx, cy, 170 * view.z);
      g.addColorStop(0, mix(C.signal, 0.08 + 0.4 * heat)); g.addColorStop(1, mix(C.signal, 0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.ellipse(cx, cy, 170 * view.z, 85 * view.z, 0, 0, Math.PI * 2); ctx.fill();
    }
    L.props.forEach((p) => prop(p, id, gx, gy, w, d, now));
    label(room.name, gx + w / 2, gy, mix(L.tint, 0.95), 1, null, true);
    hits.push({ type: "room", id, poly: corners });
  }

  let labels = [];
  function label(...args) { labels.push(args); } // drawn last so rooms in front never cover them
  function drawLabel(text, gx, gy, color, scale = 1, sub = null, onWall = false) {
    const [x, y] = onWall ? toScreen(gx, gy, WALL + 12) : toScreen(gx, gy);
    ctx.font = `600 ${Math.max(9, 12 * view.z * scale)}px "Chakra Petch", sans-serif`;
    const tw = ctx.measureText(text).width, fh = Math.max(9, 12 * view.z * scale);
    ctx.fillStyle = "rgba(8,12,9,0.72)";
    roundRect(x - tw / 2 - 6, y - fh - 2, tw + 12, fh + 8, 3); ctx.fill();
    ctx.font = `600 ${Math.max(9, 12 * view.z * scale)}px "Chakra Petch", sans-serif`;
    ctx.textAlign = "center"; ctx.fillStyle = color;
    ctx.fillText(text, x, y);
    if (sub) {
      ctx.font = `500 ${Math.max(8, 10 * view.z * scale)}px "IBM Plex Mono", monospace`;
      ctx.fillStyle = mix(C.amber, 0.8); ctx.fillText(sub, x, y + 14 * view.z);
    }
  }

  function screen(gx, gy, h, glow) { // a little monitor on a desk
    const p = box(gx, gy, 0.6, 0.12, h + 16, "#0E1410", "#0B100C", "#0D130F");
    if (glow) { poly(p, glow, null); }
  }

  function prop(kind, room, gx, gy, w, d, now) {
    const blink = reduced ? 1 : 0.6 + 0.4 * Math.sin(now / 300);
    const lead = (data.agents.find((a) => a.room === room) || {});
    const active = ["working", "thinking"].includes(lead.state);
    switch (kind) {
      case "desk":
        box(gx + 2, gy + 1, 1.6, 0.8, 14, "#1A241C", "#121A14", "#151E17");
        screen(gx + 2.4, gy + 1, 14, active ? mix(C.phosphor, 0.35 * blink + 0.2) : mix(C.phosphor, 0.08));
        break;
      case "radar": {
        const [cx, cy] = toScreen(gx + 1.3, gy + 3.2);
        ctx.strokeStyle = mix(C.phosphor, 0.35); ctx.lineWidth = 1;
        [1, 0.66, 0.33].forEach((r) => { ctx.beginPath(); ctx.ellipse(cx, cy, 40 * r * view.z, 20 * r * view.z, 0, 0, Math.PI * 2); ctx.stroke(); });
        const a = reduced ? 0.7 : (now / 900) % (Math.PI * 2);
        ctx.strokeStyle = mix(C.phosphor, active ? 0.9 : 0.4); ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(a) * 40 * view.z, cy + Math.sin(a) * 20 * view.z); ctx.stroke();
        break;
      }
      case "board": { // pipeline board on the back wall: one card per new lead
        const n = Math.min(((lead.pipeline || {}).new) || 0, 12);
        const p = [toScreen(gx + 1, gy, 14), toScreen(gx + 5, gy, 14), toScreen(gx + 5, gy, WALL - 6), toScreen(gx + 1, gy, WALL - 6)];
        poly(p, "#1B1A12", mix(C.amber, 0.4));
        for (let i = 0; i < n; i++) {
          const col = i % 6, row = Math.floor(i / 6);
          const [x, y] = toScreen(gx + 1.35 + col * 0.6, gy, WALL - 14 - row * 12);
          ctx.fillStyle = mix(C.amber, 0.85); ctx.fillRect(x - 5 * view.z, y - 4 * view.z, 9 * view.z, 7 * view.z);
        }
        break;
      }
      case "table":
        box(gx + 1.6, gy + 1.6, 2.6, 1.4, 12, "#1C251E", "#121A14", "#161F18");
        break;
      case "phone": { // the red phone on the Command Deck rings when something needs Patrick
        const ringing = data.hud.approvals.length > 0;
        box(gx + 3.6, gy + 2.1, 0.4, 0.3, 18, ringing ? mix(C.signal, 0.5 + 0.5 * blink) : "#3A1814", "#2A110E", "#331310");
        if (ringing) {
          const [x, y] = toScreen(gx + 3.8, gy + 2.2, 34);
          ctx.strokeStyle = mix(C.signal, 0.8 * blink); ctx.lineWidth = 2;
          [8, 14].forEach((r) => { ctx.beginPath(); ctx.arc(x, y, r * view.z, Math.PI * 1.15, Math.PI * 1.85); ctx.stroke(); });
          hits.push({ type: "approvals", poly: [toScreen(gx + 3.4, gy + 1.9), toScreen(gx + 4.2, gy + 1.9), toScreen(gx + 4.2, gy + 2.7), toScreen(gx + 3.4, gy + 2.7)] });
        }
        break;
      }
      case "screens": // wall of screens behind the Architect
        for (let i = 0; i < 3; i++) {
          const p = [toScreen(gx + 1.2 + i * 1.3, gy, 16), toScreen(gx + 2.2 + i * 1.3, gy, 16), toScreen(gx + 2.2 + i * 1.3, gy, 36), toScreen(gx + 1.2 + i * 1.3, gy, 36)];
          poly(p, mix(C.phosphor, 0.06 + (active ? 0.12 * blink : 0)), mix(C.phosphor, 0.3));
        }
        break;
      case "booth": {
        box(gx + 0.8, gy + 1.2, 2.4, 0.9, 12, "#16202A", "#101820", "#131C24");
        // live waveform while C.L.a.i.R talks
        const [x0, y0] = toScreen(gx + 1, gy, 30), [x1] = toScreen(gx + 3.4, gy, 30);
        ctx.strokeStyle = mix("#7FB2E5", active ? 0.9 : 0.3); ctx.lineWidth = 1.5; ctx.beginPath();
        for (let i = 0; i <= 24; i++) {
          const x = x0 + (x1 - x0) * i / 24, amp = active ? 8 * Math.sin(now / 90 + i * 0.9) * Math.sin(i / 24 * Math.PI) : 1;
          const y = y0 + (i / 24) * ((toScreen(gx + 3.4, gy, 30)[1]) - y0) + amp * view.z;
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        }
        ctx.stroke();
        break;
      }
      case "lockers":
        for (let i = 0; i < 5; i++) box(gx + 0.3 + i * 0.7, gy + 0.2, 0.6, 0.4, 34, "#18211B", "#111813", "#141C16");
        break;
      case "bench":
        box(gx + 1, gy + 4, 3, 0.5, 8, "#1A231C", "#121A14", "#151E17");
        break;
      case "cabinets":
        for (let i = 0; i < 3; i++) box(gx + 2.6 + i * 0.7, gy + 0.2, 0.6, 0.5, 28, "#221E16", "#17140F", "#1C1912");
        break;
      case "bunk":
        break; // bunks are drawn with sleeping agents
      case "terminals": {
        const code = data.agents.some((a) => a.room === "codebay" && a.state === "working");
        for (let i = 0; i < 3; i++) {
          box(gx + 0.6 + i * 1.3, gy + 0.6, 1, 0.6, 12, "#17202A", "#10171E", "#131B22");
          screen(gx + 0.8 + i * 1.3, gy + 0.6, 12, code ? mix(C.steel, 0.25 + 0.3 * blink) : mix(C.steel, 0.07));
        }
        break;
      }
      case "racks":
        for (let i = 0; i < 2; i++) {
          box(gx + 3.6, gy + 1.6 + i * 1.2, 0.8, 0.9, 40, "#141A1E", "#0E1316", "#11171A");
        }
        break;
      case "gpu": {
        const util = (data.hud.gpu && data.hud.gpu.util) || 0;
        for (let i = 0; i < 3; i++) {
          const top = box(gx + 1 + i * 1.5, gy + 0.6, 1.1, 1, 30, "#1E1512", "#140E0C", "#19110E");
          const hot = Math.min(1, util / 100 + (data.hud.gpu.training ? 0.4 : 0));
          poly(top, mix(C.signal, 0.08 + 0.45 * hot * blink), null);
        }
        break;
      }
    }
  }

  // ---- agents ---------------------------------------------------------------------------
  function placeAgents(dt) {
    const slot = { ready: 0, codebay: 0 };
    const seen = new Set();
    const out = data.agents.map((a) => {
      let target = HOME[a.id];
      if (a.kind === "splinter") { const s = SLOTS[a.room]; target = s[slot[a.room]++ % s.length]; }
      if (["working", "thinking"].includes(a.state) && STATION[a.id]) target = STATION[a.id];
      if (a.state === "asleep" && BUNK[a.id]) target = BUNK[a.id];
      target = target || [10, 10];
      seen.add(a.id);
      let cur = motion[a.id];
      if (!cur) {  // new splinters walk in from the Ready Room door; everyone else starts in place
        cur = motion[a.id] = a.kind === "splinter" ? [a.room === "codebay" ? 2.5 : 16, a.room === "codebay" ? 16.8 : 4.8] : [...target];
      }
      const dx = target[0] - cur[0], dy = target[1] - cur[1], dist = Math.hypot(dx, dy);
      const step = reduced ? dist : Math.min(dist, 1.8 * dt);  // tiles per second
      if (dist > 0.001) { cur[0] += (dx / dist) * step; cur[1] += (dy / dist) * step; }
      const walking = dist > 0.05;
      return { ...a, pos: [cur[0], cur[1]], walking, facing: dx >= 0 ? 1 : -1, inBunk: a.state === "asleep" && BUNK[a.id] && !walking };
    });
    Object.keys(motion).forEach((id) => { if (!seen.has(id)) delete motion[id]; });
    return out;
  }

  function drawAgent(a, now) {
    const [gx, gy] = a.pos;
    const [x, y] = toScreen(gx, gy);
    const z = view.z, sel = selected && selected.type === "agent" && selected.id === a.id;
    const hov = hover && hover.type === "agent" && hover.id === a.id;
    const bob = reduced ? 0 : a.walking ? -Math.abs(Math.sin(now / 111 + gx)) * 1.5 : a.state === "idle" ? Math.sin(now / 700 + gx) * 1.2 : 0;
    const patch = VENDOR[a.vendor] || C.fog;

    // shadow + selection ring
    ctx.fillStyle = "rgba(0,0,0,0.45)"; ctx.beginPath(); ctx.ellipse(x, y, 15 * z, 7.5 * z, 0, 0, Math.PI * 2); ctx.fill();
    if (sel || hov) { ctx.strokeStyle = sel ? C.amber : mix(C.paper, 0.6); ctx.lineWidth = 2; ctx.beginPath(); ctx.ellipse(x, y, 17 * z, 8.5 * z, 0, 0, Math.PI * 2); ctx.stroke(); }

    if (a.inBunk) { // lying in a bunk
      box(gx - 0.5, gy - 0.25, 1.4, 0.6, 8, "#1E2620", "#141B16", "#18201A");
      const [hx, hy] = toScreen(gx - 0.25, gy, 12);
      ctx.fillStyle = "#2A332C"; ctx.beginPath(); ctx.ellipse(hx + 14 * z, hy + 2 * z, 16 * z, 5 * z, -0.45, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#3A443B"; ctx.beginPath(); ctx.arc(hx, hy, 5 * z, 0, Math.PI * 2); ctx.fill();
      if (!reduced) {
        const k = (now / 1400) % 1;
        ctx.fillStyle = mix(C.paper, 0.7 * (1 - k)); ctx.font = `600 ${11 * z}px "IBM Plex Mono", monospace`;
        ctx.fillText("z", hx + 8 * z, hy - 10 * z - k * 16 * z);
      }
      hits.push({ type: "agent", id: a.id, rect: [hx - 12 * z, hy - 14 * z, 40 * z, 24 * z] });
      return;
    }

    if (a.id === "clair") { // C.L.a.i.R: a mint-green flame wisp, as on Patrick's portfolio
      const fl = reduced ? 0 : Math.sin(now / 160) * 2;
      const cy = y - 30 * z + bob;
      const g = ctx.createRadialGradient(x, cy, 2, x, cy, 30 * z);
      g.addColorStop(0, "rgba(200,255,225,0.95)"); g.addColorStop(0.35, "rgba(120,230,170,0.55)"); g.addColorStop(1, "rgba(120,230,170,0)");
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, cy, 30 * z, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "rgba(170,250,205,0.9)"; ctx.beginPath();
      ctx.moveTo(x, cy - (20 + fl) * z); ctx.quadraticCurveTo(x + 11 * z, cy - 4 * z, x + 6 * z, cy + 8 * z);
      ctx.quadraticCurveTo(x, cy + 13 * z, x - 6 * z, cy + 8 * z); ctx.quadraticCurveTo(x - 11 * z, cy - 4 * z, x, cy - (20 + fl) * z); ctx.fill();
      ctx.fillStyle = patch; ctx.fillRect(x + 12 * z, cy + 6 * z, 5 * z, 5 * z);
      stateMark(a, x, cy - 30 * z, now);
      hits.push({ type: "agent", id: a.id, rect: [x - 18 * z, cy - 30 * z, 36 * z, 50 * z] });
      nameTag(a, x, y + 14 * z, sel || hov);
      return;
    }

    const by = y + bob * z;
    drawFigure(a, x, by, now);
    const tall = (a.kind === "commander" ? 1.08 : 1) * FIG;
    stateMark(a, x, by - 54 * z * tall, now);
    hits.push({ type: "agent", id: a.id, rect: [x - 14 * z, by - 50 * z * tall, 28 * z, 52 * z * tall] });
    nameTag(a, x, y + 14 * z, sel || hov || a.kind === "commander");
  }

  // People are drawn in local units (origin at their feet, 1 unit = 1px at zoom 1), facing right.
  const FIG = 1.3;  // figure scale relative to the tiles
  const LOOK = {
    commander: { coat: "#26352A", trim: C.phosphor, skin: "#C9B8A3", hair: "#2A2A26", legs: "#141A16", boots: "#0B0F0C", long: true },
    sleeper:   { coat: "#2A3530", trim: C.fog,      skin: "#BFA48C", hair: "#1E2620", legs: "#1A201C", boots: "#0E120F", hood: true },
    splinter:  { coat: "#20282B", trim: C.steel,    skin: "#B49C86", hair: "#151B1E", legs: "#161C1F", boots: "#0A0D0F", visor: true },
    forge:     { coat: "#3A2A22", trim: C.signal,   skin: "#C7A88E", hair: "#2B211B", legs: "#1A1513", boots: "#0E0B0A", apron: true },
  };
  function drawFigure(a, x, y, now) {
    const L = LOOK[a.kind] || LOOK.sleeper, z = view.z, t = now / 1000;
    const walk = a.walking && !reduced ? Math.sin(t * 9 + a.pos[0]) : 0;
    const typing = a.state === "working" && !a.walking && !reduced;
    const tall = (a.kind === "commander" ? 1.08 : 1) * FIG;
    ctx.save();
    ctx.translate(x, y); ctx.scale(a.facing * z * tall, z * tall);
    // legs and boots (alternate when walking)
    [[-4, walk], [1, -walk]].forEach(([lx, ph]) => {
      ctx.fillStyle = L.legs; ctx.fillRect(lx, -14 - Math.max(0, ph) * 2, 4, 13 + Math.max(0, ph) * 2 - Math.max(0, ph) * 2);
      ctx.fillStyle = L.boots; ctx.fillRect(lx - 0.5 + ph * 1.5, -2 - Math.max(0, ph) * 2, 5.5, 3);
    });
    // torso: shoulders wider than the waist
    ctx.fillStyle = L.coat; ctx.beginPath();
    ctx.moveTo(-9, -32); ctx.lineTo(9, -32); ctx.lineTo(7, L.long ? -6 : -13); ctx.lineTo(-7, L.long ? -6 : -13); ctx.closePath(); ctx.fill();
    if (L.long) { ctx.strokeStyle = "rgba(0,0,0,0.35)"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0.5, -24); ctx.lineTo(0.5, -6); ctx.stroke(); }
    if (L.apron) { ctx.fillStyle = "#5A4232"; ctx.fillRect(-5.5, -27, 11, 15); ctx.fillStyle = mix(C.signal, 0.25); ctx.fillRect(-5.5, -27, 11, 1.5); }
    if (L.visor) {  // tactical vest with pouches
      ctx.fillStyle = "#2C373B"; ctx.fillRect(-6.5, -29, 13, 13);
      ctx.fillStyle = "#1A2124"; [-5, -0.5, 4].forEach((px) => ctx.fillRect(px, -21, 3, 4));
    }
    if (L.hood) { ctx.strokeStyle = "rgba(0,0,0,0.3)"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(-5, -19); ctx.lineTo(5, -19); ctx.stroke(); }
    // collar light strip (rank / role color)
    ctx.fillStyle = mix(L.trim, a.kind === "commander" ? 0.9 : 0.5); ctx.fillRect(-4, -32, 8, 1.6);
    // arms: swing when walking, forward and tapping when working
    ctx.fillStyle = L.coat;
    if (typing) {
      const tap = Math.sin(t * 22) * 1.2;
      ctx.save(); ctx.translate(8, -30); ctx.rotate(-0.9); ctx.fillRect(0, -1.5, 3.2, 12); ctx.restore();
      ctx.save(); ctx.translate(-8, -30); ctx.rotate(-0.6); ctx.fillRect(0, -1.5, 3.2, 12); ctx.restore();
      ctx.fillStyle = L.skin; ctx.fillRect(15, -24 + tap, 2.6, 2.6); ctx.fillRect(4, -21 - tap, 2.6, 2.6);
    } else {
      [[-11.5, walk], [8.5, -walk]].forEach(([ax, ph]) => {
        ctx.save(); ctx.translate(ax + 1.5, -31); ctx.rotate(ph * 0.35); ctx.fillStyle = L.coat; ctx.fillRect(-1.5, 0, 3.2, 14);
        ctx.fillStyle = L.skin; ctx.fillRect(-1.3, 13.5, 2.8, 2.6); ctx.restore();
      });
    }
    // vendor patch on the shoulder
    ctx.fillStyle = VENDOR[a.vendor] || C.fog; ctx.fillRect(-9, -31, 4, 4);
    // head
    if (L.hood) { ctx.fillStyle = L.coat; ctx.beginPath(); ctx.arc(0, -38, 8.6, Math.PI * 0.95, Math.PI * 2.05); ctx.fill(); }
    ctx.fillStyle = L.skin; ctx.beginPath(); ctx.arc(1, -38, 6, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = L.hair; ctx.beginPath(); ctx.arc(1, -39.5, 6.2, Math.PI * 1.02, Math.PI * 1.98); ctx.fill();
    if (L.visor) {  // helmet and a single glowing visor band
      ctx.fillStyle = "#1B2326"; ctx.beginPath(); ctx.arc(1, -39.5, 7, Math.PI, Math.PI * 2); ctx.fill();
      const glow = a.state === "working" ? 0.95 : 0.55;
      ctx.fillStyle = mix(C.phosphor, glow); ctx.fillRect(-2, -39.5, 9.5, 2.4);
      if (a.state === "working") { ctx.fillStyle = mix(C.phosphor, 0.18); ctx.fillRect(-3, -42, 12, 7); }
    }
    if (L.apron) {  // goggles pushed up on the forehead
      ctx.fillStyle = "#2B211B"; ctx.fillRect(-5, -43.5, 12, 2);
      ctx.fillStyle = mix(C.signal, 0.8); ctx.beginPath(); ctx.arc(-1, -42.5, 2, 0, Math.PI * 2); ctx.arc(4.5, -42.5, 2, 0, Math.PI * 2); ctx.fill();
    }
    if (a.kind === "commander") {  // earpiece and mic boom
      ctx.fillStyle = "#121714"; ctx.fillRect(5.5, -40, 2.6, 4.5);
      ctx.strokeStyle = "#121714"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(7, -36); ctx.lineTo(4, -33.5); ctx.stroke();
      ctx.fillStyle = mix(C.phosphor, 0.9); ctx.fillRect(7, -39, 1.2, 1.2);
    }
    ctx.restore();
  }

  function stateMark(a, x, y, now) {
    const z = view.z;
    if (a.state === "thinking") {
      const r = reduced ? 6 : 5 + 2 * Math.sin(now / 250);
      ctx.strokeStyle = mix(C.phosphor, 0.85); ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, r * z, 0, Math.PI * 2); ctx.stroke();
    } else if (a.state === "blocked") {
      const on = reduced || Math.floor(now / 450) % 2 === 0;
      ctx.fillStyle = on ? C.signal : mix(C.signal, 0.3); ctx.beginPath(); ctx.arc(x, y, 4.5 * z, 0, Math.PI * 2); ctx.fill();
      if (on) { const g = ctx.createRadialGradient(x, y, 1, x, y, 16 * z); g.addColorStop(0, mix(C.signal, 0.5)); g.addColorStop(1, mix(C.signal, 0)); ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, 16 * z, 0, Math.PI * 2); ctx.fill(); }
    }
  }

  function nameTag(a, x, y, show) {
    if (!show) return;
    ctx.font = `600 ${Math.max(9, 11 * view.z)}px "Chakra Petch", sans-serif`;
    const w = ctx.measureText(a.name).width + 10;
    ctx.fillStyle = "rgba(7,10,8,0.85)"; ctx.fillRect(x - w / 2, y - 2, w, 16 * Math.max(1, view.z * 0.9));
    ctx.fillStyle = C.paper; ctx.textAlign = "center"; ctx.fillText(a.name, x, y + 10 * Math.max(1, view.z * 0.9));
  }

  function roundRect(x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  // ---- frame ----------------------------------------------------------------------------
  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (!data || document.hidden) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const bg = ctx.createRadialGradient(w / 2, h * 0.35, 40, w / 2, h / 2, Math.max(w, h));
    bg.addColorStop(0, "#132018"); bg.addColorStop(1, C.night);
    ctx.fillStyle = bg; ctx.fillRect(0, 0, w, h);
    hits = []; labels = [];
    const dt = lastFrame ? Math.min(0.1, (now - lastFrame) / 1000) : 0; lastFrame = now;
    // rooms back-to-front (by gx + gy), then agents back-to-front
    Object.keys(LAYOUT).sort((a, b) => (LAYOUT[a].r[0] + LAYOUT[a].r[1]) - (LAYOUT[b].r[0] + LAYOUT[b].r[1])).forEach((id) => drawRoom(id, now));
    const placed = placeAgents(dt);
    placed.sort((a, b) => (a.pos[0] + a.pos[1]) - (b.pos[0] + b.pos[1])).forEach((a) => drawAgent(a, now));
    drawSparks(dt);
    drawPings(placed, now);
    labels.forEach((l) => drawLabel(...l));
    // vignette and faint scanlines: the ops-center monitor look
    const v = ctx.createRadialGradient(w / 2, h / 2, Math.min(w, h) * 0.35, w / 2, h / 2, Math.max(w, h) * 0.75);
    v.addColorStop(0, "rgba(0,0,0,0)"); v.addColorStop(1, "rgba(0,0,0,0.55)");
    ctx.fillStyle = v; ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = scanlines(); ctx.fillRect(0, 0, w, h);
    if (data.hud.halted) {
      ctx.fillStyle = "rgba(255,77,61,0.06)"; ctx.fillRect(0, 0, w, h);
    }
  }

  let scan = null;
  function scanlines() {
    if (scan) return scan;
    const c = document.createElement("canvas"); c.width = 4; c.height = 3;
    const g = c.getContext("2d"); g.fillStyle = "rgba(0,0,0,0.16)"; g.fillRect(0, 2, 4, 1);
    scan = ctx.createPattern(c, "repeat");
    return scan;
  }

  // Sparks fly off the Forge's GPUs when they're working hard.
  function drawSparks(dt) {
    const g = data.hud.gpu || {}, heat = Math.max((g.util || 0) / 100, g.training ? 0.8 : 0);
    if (!reduced && heat > 0.25 && Math.random() < heat * 0.6) {
      const i = Math.floor(Math.random() * 3);
      sparks.push({ gx: 7.5 + i * 1.5, gy: 13.1, h: 32, vx: (Math.random() - 0.5) * 1.2, vy: (Math.random() - 0.5) * 0.6, vh: 40 + Math.random() * 50, life: 1 });
    }
    sparks = sparks.filter((p) => (p.life -= dt * 1.4) > 0);
    sparks.forEach((p) => {
      p.gx += p.vx * dt; p.gy += p.vy * dt; p.vh -= 160 * dt; p.h = Math.max(0, p.h + p.vh * dt);
      const [x, y] = toScreen(p.gx, p.gy, p.h);
      ctx.fillStyle = `rgba(255,${150 + Math.floor(80 * p.life)},80,${p.life})`;
      ctx.fillRect(x, y, 2 * view.z, 2 * view.z);
    });
  }

  // A ring pops on an agent when something happens (new activity from them).
  const PING = { done: C.phosphor, start: C.phosphor, error: C.signal, warn: C.amber, info: C.paper };
  function drawPings(placed, now) {
    pings = pings.filter((p) => now - p.t < 1600);
    pings.forEach((p) => {
      const a = placed.find((x) => x.id === p.agent) || (p.agent === "splinter" && placed.find((x) => x.kind === "splinter"));
      if (!a) return;
      const k = (now - p.t) / 1600, [x, y] = toScreen(a.pos[0], a.pos[1]);
      ctx.strokeStyle = mix(PING[p.kind] || C.paper, 0.8 * (1 - k)); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.ellipse(x, y, (14 + 26 * k) * view.z, (7 + 13 * k) * view.z, 0, 0, Math.PI * 2); ctx.stroke();
    });
  }

  // ---- hit testing & input --------------------------------------------------------------
  function inPoly(x, y, pts) {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [xi, yi] = pts[i], [xj, yj] = pts[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }
  function pick(x, y) {
    for (let i = hits.length - 1; i >= 0; i--) { // agents are drawn last, so they win
      const h = hits[i];
      if (h.rect) { const [rx, ry, rw, rh] = h.rect; if (x >= rx && x <= rx + rw && y >= ry && y <= ry + rh) return h; }
      else if (h.poly && inPoly(x, y, h.poly)) return h;
    }
    return null;
  }
  function pos(e) { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

  function bindInput() {
    canvas.addEventListener("pointermove", (e) => {
      const [x, y] = pos(e);
      if (dragging) { view.x += x - dragging[0]; view.y += y - dragging[1]; dragging = [x, y]; dragging.moved = true; return; }
      const h = pick(x, y);
      hover = h ? { type: h.type, id: h.id } : null;
      canvas.style.cursor = h ? "pointer" : "grab";
    });
    canvas.addEventListener("pointerdown", (e) => { dragging = pos(e); dragging.moved = false; canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener("pointerup", (e) => {
      const wasDrag = dragging && dragging.moved; dragging = null;
      if (wasDrag) return;
      const [x, y] = pos(e), h = pick(x, y);
      if (!h) { selected = null; closePanel(); return; }
      if (h.type === "approvals") { openApprovals(); return; }
      selected = { type: h.type, id: h.id };
      openPanel();
    });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const [x, y] = pos(e), k = Math.exp(-e.deltaY * 0.0015), z = Math.min(2.4, Math.max(0.45, view.z * k));
      view.x = x - (x - view.x) * (z / view.z); view.y = y - (y - view.y) * (z / view.z); view.z = z;
    }, { passive: false });
    $("#floor-recenter").addEventListener("click", fit);
    window.addEventListener("resize", () => { if (isOpen()) { size(); fit(); } });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen()) { selected = null; closePanel(); } });
  }

  function size() {
    dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(canvas.clientWidth * dpr); canvas.height = Math.round(canvas.clientHeight * dpr);
  }

  // ---- HUD & panels ---------------------------------------------------------------------
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const when = (iso) => {
    if (!iso) return "not scheduled";
    const d = new Date(iso), now = new Date();
    const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return d.toDateString() === now.toDateString() ? `today ${t}` : `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
  };

  function renderHud() {
    const h = data.hud;
    const pct = Math.min(100, (h.spend / h.budget) * 100);
    $("#fh-spend").textContent = `$${h.spend.toFixed(2)} of $${h.budget.toFixed(0)}`;
    $("#fh-spend-bar").style.width = `${pct}%`;
    $("#fh-spend-bar").className = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
    const running = h.missions.filter((m) => m.status === "running").length, queued = h.missions.length - running;
    $("#fh-missions").textContent = h.missions.length ? `${running} running · ${queued} queued` : "No missions running";
    const g = h.gpu || {};
    const ls = g.lessons;
    $("#fh-lessons").textContent = !ls ? "Forge offline"
      : ls.have >= ls.needed ? `${ls.have} banked · ready to train` : `${ls.have} of ${ls.needed} to first training run`;
    $("#fh-lessons-bar").style.width = ls ? `${Math.min(100, (ls.have / ls.needed) * 100)}%` : "0";
    $("#fh-lessons-bar").className = ls && ls.have >= ls.needed ? "ready" : "";
    const gpu = g.online ? `GPU ${g.util ?? "–"}% · ${g.temp ?? "–"}°C${g.training ? " · training" : ""}` : "GPU: Forge offline";
    $("#fh-health").textContent = `CPU ${h.host.cpu_percent}% · RAM ${h.host.mem_used_gb}/${h.host.mem_total_gb} GB · ${gpu}`;
    $("#fh-link").textContent = { cloudflare: "C.L.a.i.R on Gemini", fallback: "C.L.a.i.R on local backup", local: "C.L.a.i.R local" }[h.clair_link] || "";
    const ap = $("#fh-approvals");
    ap.hidden = !h.approvals.length;
    ap.textContent = `${h.approvals.length} need${h.approvals.length === 1 ? "s" : ""} you`;
    if (sound && h.approvals.length > lastApprovals) ring();
    lastApprovals = h.approvals.length;
    // radio log, newest first
    const log = $("#fh-log");
    log.replaceChildren(...[...data.events].reverse().slice(0, 4).map((e) => {
      const li = el("li", e.kind);
      li.append(el("span", "t", new Date(e.ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })), el("span", "m", e.text));
      return li;
    }));
    document.body.classList.toggle("halted", h.halted);
    $("#fh-halt-banner").hidden = !h.halted;
    $("#kill").hidden = h.halted;
    if (selected) openPanel(true);
  }

  function closePanel() { $("#inspect").hidden = true; }

  function openPanel(refresh = false) {
    const p = $("#inspect"), body = $("#inspect-body");
    if (refresh && p.hidden) return;
    body.replaceChildren();
    if (selected.type === "agent") {
      const a = data.agents.find((x) => x.id === selected.id);
      if (!a) { closePanel(); return; }
      $("#inspect-title").textContent = a.name;
      $("#inspect-sub").textContent = a.role;
      const dl = el("dl", "facts");
      const add = (k, v) => { if (v == null || v === "") return; dl.append(el("dt", null, k), el("dd", null, v)); };
      add("State", STATE_NAME[a.state] || a.state);
      add("Thinks with", `${a.model} (${VENDOR_NAME[a.vendor] || a.vendor})`);
      if (a.next !== undefined) add("Next run", when(a.next));
      if (a.waiting_on_you) add("Waiting on you", `${a.waiting_on_you} approval${a.waiting_on_you === 1 ? "" : "s"}`);
      if (a.pipeline) add("Pipeline", `${a.pipeline.new || 0} new · ${a.pipeline.contacted || 0} contacted · ${a.pipeline.won || 0} won`);
      if (a.link) add("Link", { cloudflare: "Cloudflare (Gemini)", fallback: "Local backup", local: "Local" }[a.link]);
      body.append(dl);
      if (a.mission) body.append(timeline(a.mission));
      else if (a.last) { body.append(el("h4", null, "Last activity")); body.append(el("p", `last ${a.last.kind}`, a.last.text)); }
      const row = el("div", "btnrow");
      if (a.id === "clair" || a.id === "architect") {
        const b = el("button", "small", `Message ${a.name}`); b.type = "button";
        b.addEventListener("click", () => window.WWFloor.toTactical("comms", a.id));
        row.append(b);
      }
      if (a.id === "intel" || a.id === "revenue") {
        const b = el("button", "ghost small", a.id === "intel" ? "Run Intel sweep" : "Hunt now"); b.type = "button";
        b.addEventListener("click", () => act(a.id, b));
        row.append(b);
      }
      if (a.id === "architect") {
        const b = el("button", "ghost small", "Check in now"); b.type = "button";
        b.addEventListener("click", () => act("cycle", b));
        row.append(b);
      }
      if (a.id === "forge") {
        const b = el("button", "ghost small", "Run a drill"); b.type = "button";
        b.addEventListener("click", () => act("drill", b));
        row.append(b);
      }
      if (a.mission) {
        const b = el("button", "ghost small", "Open mission"); b.type = "button";
        b.addEventListener("click", () => window.WWFloor.toTactical("missions", null, a.mission));
        row.append(b);
      }
      if (row.children.length) body.append(row);
    } else {
      const r = data.rooms.find((x) => x.id === selected.id);
      $("#inspect-title").textContent = r.name;
      $("#inspect-sub").textContent = r.sealed ? "Sealed" : "Room";
      if (r.sealed) {
        body.append(el("p", null, "Sealed. This room opens when a new cell for it is approved."));
      } else {
        const here = data.agents.filter((a) => a.room === r.id);
        const ul = el("ul", "roster");
        here.forEach((a) => {
          const li = el("li"); const b = el("button", "linklike", `${a.name} · ${STATE_NAME[a.state]}`); b.type = "button";
          b.addEventListener("click", () => { selected = { type: "agent", id: a.id }; openPanel(); });
          li.append(b); ul.append(li);
        });
        if (here.length) body.append(ul); else body.append(el("p", "muted", "Nobody here right now."));
        if (r.id === "forge") {
          const g = data.hud.gpu || {};
          const dl = el("dl", "facts");
          [["GPU", g.gpu], ["Load", g.util != null ? `${g.util}%` : null], ["Temperature", g.temp != null ? `${g.temp}°C` : null],
           ["Memory", g.mem_used != null ? `${Math.round(g.mem_used)} of ${Math.round(g.mem_total)} MB` : null],
           ["Lessons", g.lessons ? `${g.lessons.have} of ${g.lessons.needed} needed` : null],
           ["Drills today", g.drills ? (g.drills.paused_until ? `Paused until ${g.drills.paused_until} (Gemini limit)`
             : `${g.drills.steps_today} of ${g.drills.daily_steps} Gemini calls`) : null], ["Training", g.job ? `${g.job.id}: ${g.job.message}` : (g.queued || []).length ? `${g.queued.length} queued for tonight` : "Nothing running"]]
            .forEach(([k, v]) => { if (v) dl.append(el("dt", null, k), el("dd", null, v)); });
          body.append(dl);
        }
        if (r.id === "ready" || r.id === "codebay") {
          const ms = data.hud.missions.filter((m) => (m.kind === "code") === (r.id === "codebay"));
          body.append(el("h4", null, ms.length ? "Missions" : "No missions here"));
          ms.forEach((m) => {
            const b = el("button", "linklike tl-open", `#${m.id} ${m.title} (${m.status})`); b.type = "button";
            b.addEventListener("click", () => { selected = { type: "agent", id: `splinter-${m.id}` }; openPanel(); });
            body.append(b);
          });
        }
      }
    }
    p.hidden = false;
  }

  // Mission timeline: every step the Splinter logged, newest last.
  function timeline(mid) {
    const box = el("div", "timeline");
    const cached = timelines[mid];
    if (!cached || performance.now() - cached.at > 2500) {
      timelines[mid] = { at: performance.now(), mission: cached && cached.mission };
      window.WW.api(`/api/missions/${mid}`).then((m) => {
        timelines[mid] = { at: performance.now(), mission: m };
        const old = document.querySelector(`.timeline[data-mid="${mid}"]`);
        if (old) old.replaceWith(timeline(mid));
      }).catch(() => {});
    }
    box.dataset.mid = mid;
    const m = cached && cached.mission;
    box.append(el("h4", null, m ? `Mission #${m.id} · ${m.status}` : `Mission #${mid}`));
    if (!m) { box.append(el("p", "muted", "Loading the timeline…")); return box; }
    box.append(el("p", "tl-request", m.request.length > 220 ? m.request.slice(0, 220) + "…" : m.request));
    const ol = el("ol", "tl");
    const steps = [{ ts: m.created, text: `Opened by ${m.source}` }, ...m.steps.slice(-12)];
    steps.forEach((st, i) => {
      const li = el("li", i === steps.length - 1 && m.status === "running" ? "now" : "");
      li.append(el("span", "tl-t", new Date(st.ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })), el("span", "tl-x", st.text));
      ol.append(li);
    });
    if (m.status === "done") { const li = el("li", "end"); li.append(el("span", "tl-t", ""), el("span", "tl-x", "Done. The write-up is in Missions.")); ol.append(li); }
    box.append(ol);
    return box;
  }

  async function act(name, btn) {
    btn.disabled = true;
    try { await window.WW.api(`/api/actions/${name}`, { method: "POST" }); btn.textContent = "Started"; }
    catch (e) { btn.textContent = e.message.slice(0, 40); }
    setTimeout(() => { btn.disabled = false; }, 3000);
  }

  function openApprovals() {
    selected = null;
    const p = $("#inspect"), body = $("#inspect-body");
    $("#inspect-title").textContent = "Needs you"; $("#inspect-sub").textContent = "Approval requests";
    body.replaceChildren();
    if (!data.hud.approvals.length) body.append(el("p", "muted", "Nothing waiting."));
    data.hud.approvals.forEach((a) => {
      const card = el("div", "approval-card");
      card.append(el("p", "what", `#${a.id} ${a.summary}`), el("p", "why", a.reason || ""));
      const row = el("div", "btnrow");
      [["Approve", "approve", "small"], ["Decline", "decline", "ghost small"]].forEach(([label, decision, cls]) => {
        const b = el("button", cls, label); b.type = "button";
        b.addEventListener("click", async () => {
          b.disabled = true;
          try { await window.WW.api(`/api/approvals/${a.id}`, { method: "POST", body: JSON.stringify({ decision }) }); await load(); openApprovals(); }
          catch (e) { b.textContent = e.message.slice(0, 40); }
        });
        row.append(b);
      });
      card.append(row); body.append(card);
    });
    p.hidden = false;
  }

  // Kill switch: lift the cover, then confirm.
  function bindKill() {
    const cover = $("#kill-cover"), btn = $("#kill-btn");
    cover.addEventListener("click", () => { $("#kill").classList.add("open"); btn.focus(); setTimeout(() => $("#kill").classList.remove("open"), 6000); });
    btn.addEventListener("click", async () => {
      if (!confirm("Halt all autonomous work? Missions, check-ins, Sleepers and training stop until you resume. Chat keeps working.")) return;
      try { await window.WW.api("/api/halt", { method: "POST", body: JSON.stringify({ halted: true, confirm: "HALT" }) }); await load(); }
      catch (e) { alert(e.message); }
    });
    $("#resume-btn").addEventListener("click", async () => {
      try { await window.WW.api("/api/halt", { method: "POST", body: JSON.stringify({ halted: false }) }); await load(); }
      catch (e) { alert(e.message); }
    });
    $("#fh-approvals").addEventListener("click", openApprovals);
    $("#inspect-close").addEventListener("click", () => { selected = null; closePanel(); });
    const snd = $("#fh-sound");
    try { sound = localStorage.getItem("wetworks.sound") === "on"; } catch {}
    snd.setAttribute("aria-pressed", String(sound));
    snd.addEventListener("click", () => {
      sound = !sound; snd.setAttribute("aria-pressed", String(sound));
      try { localStorage.setItem("wetworks.sound", sound ? "on" : "off"); } catch {}
      if (sound) ring();
    });
  }

  let actx = null;
  function ring() { // two short phone trills, synthesized
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      [0, 0.32].forEach((off) => {
        const o = actx.createOscillator(), g = actx.createGain(), t = actx.currentTime + off;
        o.type = "square"; o.frequency.setValueAtTime(880, t); o.frequency.setValueAtTime(1046, t + 0.06);
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.06, t + 0.01); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
        o.connect(g).connect(actx.destination); o.start(t); o.stop(t + 0.24);
      });
    } catch {}
  }

  // ---- phone: top-down map ---------------------------------------------------------------
  function renderMap() {
    const map = $("#floor-map");
    if (!map) return;
    map.replaceChildren();
    const order = ["intel", "revenue", "ready", "archives", "command", "comms", "codebay", "forge", "dayjob", "finance", "gamedev", "content"];
    order.forEach((id) => {
      const r = data.rooms.find((x) => x.id === id); if (!r) return;
      const here = data.agents.filter((a) => a.room === id);
      const b = el("button", `tile${r.sealed ? " sealed" : ""}${id === "command" && data.hud.approvals.length ? " ringing" : ""}`);
      b.type = "button";
      b.append(el("span", "tile-name", r.name));
      const dots = el("span", "tile-dots");
      here.forEach((a) => { const d = el("span", `dot ${a.state}`); d.title = `${a.name}: ${STATE_NAME[a.state]}`; dots.append(d); });
      if (r.sealed) dots.append(el("span", "sealed-tag", "Sealed"));
      b.append(dots);
      b.addEventListener("click", () => { selected = { type: "room", id }; openPanel(); });
      map.append(b);
    });
  }

  // ---- lifecycle --------------------------------------------------------------------------
  async function load() {
    try {
      data = await window.WW.api("/api/floor");
      const t = performance.now();
      (data.events || []).forEach((e) => {
        if (seenEvent && e.id > seenEvent) pings.push({ agent: e.source === "drill" ? "forge" : e.source, kind: e.kind, t });
      });
      seenEvent = Math.max(seenEvent, ...(data.events || []).map((e) => e.id), 0) || seenEvent;
      renderHud(); renderMap();
    } catch (e) { /* the console shows connection problems already */ }
  }
  function isOpen() { return document.body.classList.contains("view-floor"); }

  function start() {
    canvas = $("#floor-canvas"); ctx = canvas.getContext("2d");
    bindInput(); bindKill();
    $("#fh-missions").addEventListener("click", () => { selected = { type: "room", id: "ready" }; openPanel(); });
    load();
    poll = setInterval(() => { if (isOpen() || $("#floor-map")?.offsetParent) load(); }, 3000);
  }
  function show() {
    size(); fit();
    if (!raf) raf = requestAnimationFrame(frame);
  }
  function hide() { cancelAnimationFrame(raf); raf = 0; }

  function inspect(type, id) { selected = { type, id }; openPanel(); }
  window.WWFloor = { start, show, hide, load, inspect, toTactical: () => {} };
})();
