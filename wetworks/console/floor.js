// The Floor: a top-down 3/4 pixel-art ops center. Purely a window onto /api/floor; nothing here
// drives the work. Room art comes from the licensed Retro Cybercity pack in /static/art (not in
// git; see README). Without it, the Floor draws plain stand-in blocks so it always works.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const T = 16;                                   // tile size in art pixels
  const C = {
    night: "#07060E", hull: "#121A14", seam: "#243028", fog: "#7E8A80", paper: "#D7DFD5",
    phosphor: "#9BE37D", dim: "#3E5C33", amber: "#E8A33D", signal: "#FF4D3D", steel: "#6FA8B8", sepia: "#B39B72",
    ink: "#141226", walltop: "#3A3366", walledge: "#5E54A0", corridor: "#17142A",
  };
  const VENDOR = { local: C.phosphor, google: "#7FB2E5", anthropic: C.amber, xai: "#C9C9C9", gated: "#E9C46A" };
  const VENDOR_NAME = { local: "Local model (free)", google: "Gemini", anthropic: "Claude", xai: "Grok", gated: "Approval-only model" };
  const STATE_NAME = { idle: "Idle", working: "Working", thinking: "Thinking", blocked: "Blocked", asleep: "Asleep" };

  // ---- art ---------------------------------------------------------------------------------
  const SHEETS = { A: "interior_A5.png", B: "interior_B.png", C: "interior_C.png", D: "interior_D.png", E: "interior_E.png" };
  const img = {};
  let artReady = false, artTried = false;
  function loadArt() {
    if (artTried) return; artTried = true;
    let left = Object.keys(SHEETS).length, ok = true;
    Object.entries(SHEETS).forEach(([k, f]) => {
      const i = new Image();
      i.onload = () => { if (--left === 0 && ok) { artReady = true; staticLayer = null; } };
      i.onerror = () => { ok = false; --left; };
      i.src = `/static/art/${f}`;
      img[k] = i;
    });
  }
  // Sprites: [sheet, x, y, w, h] in art pixels, plus where light comes from (relative, optional).
  const S = {
    deskPC: ["B", 182, 180, 36, 28, { glow: [10, 4, "#6FD3FF"] }], deskPCv: ["B", 182, 212, 36, 28, { glow: [10, 4, "#B89CFF"] }],
    desk: ["B", 134, 120, 36, 24], deskMug: ["B", 182, 119, 36, 25], deskV: ["B", 134, 216, 36, 24],
    sideTable: ["B", 230, 120, 20, 20], sideItems: ["B", 230, 182, 20, 22], sideVase: ["B", 230, 213, 20, 23],
    chairR: ["B", 128, 53, 16, 25], chairG: ["B", 192, 53, 16, 25], chairB: ["B", 128, 85, 16, 25], chairP: ["B", 192, 85, 16, 25],
    chairRside: ["B", 161, 53, 12, 25], chairGside: ["B", 225, 53, 12, 25],
    cabPurple: ["B", 128, 0, 32, 48], cabNavy: ["B", 160, 0, 32, 48], cabRed: ["B", 192, 0, 32, 48], cabSteel: ["B", 224, 0, 32, 48],
    chart: ["B", 49, 161, 30, 21, { glow: [15, 10, "#FF8FB0"] }], globe: ["B", 81, 161, 14, 15, { glow: [7, 7, "#6FD3FF"] }],
    data1: ["B", 97, 174, 14, 26, { glow: [7, 13, "#9BE37D"] }], data2: ["B", 113, 162, 14, 26, { glow: [7, 13, "#9BE37D"] }],
    clock: ["B", 81, 180, 14, 15], skyline: ["B", 87, 100, 34, 26], sunset: ["B", 87, 132, 34, 26], abstract: ["B", 87, 36, 34, 26],
    shelfTV: ["B", 5, 87, 23, 19], shelfTrophy: ["B", 5, 119, 23, 19], shelfPics: ["B", 5, 151, 23, 19],
    pod: ["B", 49, 113, 14, 39], pod2: ["B", 65, 113, 14, 39],
    drawers: ["C", 195, 6, 26, 20], drawersB: ["C", 195, 38, 26, 20], drawersItems: ["C", 195, 66, 26, 24], drawersPics: ["C", 195, 98, 26, 24],
    termP: ["C", 196, 128, 23, 16, { glow: [6, 6, "#B89CFF"] }], termB: ["C", 228, 128, 23, 16, { glow: [6, 6, "#6FD3FF"] }],
    holo: ["C", 195, 183, 9, 22, { glow: [4, 6, "#FF9BD8"] }],
    uDesk: ["C", 128, 26, 64, 54], uDeskB: ["C", 128, 90, 64, 54], uDeskP: ["C", 128, 218, 64, 38],
    deskOlivePC: ["C", 0, 213, 31, 21, { glow: [10, 6, "#6FD3FF"] }], deskBlue: ["C", 32, 216, 31, 18], deskRedStuff: ["C", 64, 217, 31, 17],
    rugG: ["D", 7, 7, 35, 36], rugP: ["D", 55, 7, 35, 36], rugB: ["D", 7, 55, 35, 36], rugR: ["D", 55, 55, 35, 36],
    conf: ["E", 144, 4, 32, 91], confNavy: ["E", 208, 4, 32, 91], tableP: ["D", 16, 106, 32, 48], tableN: ["D", 80, 106, 32, 48],
  };
  const WALLFACE = { lavender: [16, 0], teal: [16, 48], wood: [16, 96] };          // 48x48 panels on sheet A
  const FLOOR = { tan: [80, 0, 48], purple: [80, 96, 48], navyTile: [80, 208, 48], maroonTile: [80, 160, 48],
                  checkNavy: [0, 208, 16], dotsNavy: [16, 240, 16], diamondNavy: [0, 192, 16] };

  // ---- the map (tiles) -----------------------------------------------------------------------
  // Each room: rect incl. walls [x, y, w, h], wall style, floor, door (which wall, offset), tint for its lamp.
  const ROOM = {
    intel:    { r: [1, 1, 13, 10],  wall: "teal",     floor: "checkNavy",  door: ["s", 5], tint: C.phosphor },
    revenue:  { r: [15, 1, 14, 10], wall: "wood",     floor: "purple",        door: ["s", 6], tint: C.amber },
    ready:    { r: [30, 1, 17, 10], wall: "lavender", floor: "navyTile",   door: ["s", 7], tint: C.steel },
    archives: { r: [1, 14, 11, 10], wall: "wood",     floor: "dotsNavy",     door: ["n", 4], tint: C.sepia },
    command:  { r: [13, 14, 16, 10], wall: "teal",    floor: "maroonTile", door: ["n", 7], tint: C.phosphor },
    comms:    { r: [30, 14, 7, 10], wall: "lavender", floor: "dotsNavy",   door: ["n", 2], tint: "#7FB2E5" },
    codebay:  { r: [38, 14, 9, 10], wall: "teal",     floor: "diamondNavy", door: ["n", 3], tint: C.steel },
    forge:    { r: [1, 26, 15, 9],  wall: "wood",     floor: "checkNavy",  door: ["n", 6], tint: C.signal },
    dayjob:   { r: [17, 26, 7, 9], wall: "teal", floor: "checkNavy", door: ["n", 2], tint: "#6FD3FF" },
    finance:  { r: [24, 26, 7, 9], wall: "wood", floor: "navyTile", door: ["n", 2], tint: "#E9C46A" },
    gamedev:  { r: [31, 26, 8, 9], sealed: true },  content: { r: [39, 26, 8, 9], sealed: true },
  };
  const MAP_W = 48, MAP_H = 36, FACE = 3;   // wall faces are 3 tiles tall
  const CORRIDORS = [[1, 11, 46, 3], [1, 24, 46, 2]];
  // Interior floor origin of a room (top-left floor tile, under the wall face)
  const inner = (id) => { const [x, y, w, h] = ROOM[id].r; return { x: x + 1, y: y + 1 + FACE, w: w - 2, h: h - 2 - FACE }; };

  // Props per room: [sprite, tx, ty] in tiles from the room's floor origin. Negative ty = on the wall face.
  const PROPS = {
    intel: [["data1", 0.5, -2.4], ["globe", 2, -2.1], ["data2", 3.5, -2.4], ["shelfTV", 7.5, -2.3], ["deskPC", 0.5, 0.4], ["chairB", 1.4, 1.7],
            ["deskPCv", 5, 0.4], ["chairP", 5.9, 1.7], ["drawersItems", 9, 0.2]],
    revenue: [["chart", 1, -2.3], ["chart", 4, -2.3], ["clock", 7.5, -2.2], ["data2", 9.5, -2.4], ["uDesk", 2, 0.6], ["chairR", 3.4, 3],
              ["deskRedStuff", 8, 0.3], ["chairRside", 10.2, 0.6], ["sideVase", 10.6, 3.2]],
    ready: [["cabNavy", 0.3, -2], ["cabSteel", 2.3, -2], ["cabNavy", 4.3, -2], ["cabSteel", 6.3, -2], ["skyline", 10.2, -2.3],
            ["pod", 13.4, 0.2], ["pod2", 14.3, 0.2], ["rugB", 6, 1.6]],
    archives: [["shelfPics", 0.6, -2.3], ["shelfTrophy", 3, -2.3], ["sunset", 5.6, -2.4], ["drawers", 0.2, 0.2], ["drawersB", 2, 0.2],
               ["drawersPics", 3.8, 0.2], ["deskMug", 5.4, 1.6], ["chairGside", 7.7, 1.8]],
    command: [["data1", 1, -2.4], ["chart", 3, -2.3], ["globe", 6.2, -2.1], ["chart", 7.6, -2.3], ["data2", 10, -2.4], ["data1", 11.2, -2.4],
              ["rugG", 4.6, 0.6], ["confNavy", 5.6, 0.1, true], ["holo", 1.2, 0.4], ["holo", 12.6, 0.4]],
    comms: [["abstract", 0.4, -2.4], ["deskOlivePC", 0.8, 0.8], ["chairG", 1.5, 2.1], ["holo", 4.3, 0.3]],
    codebay: [["data2", 0.5, -2.4], ["data1", 2, -2.4], ["globe", 4.5, -2.1], ["uDeskB", 1.2, 0.6], ["termB", 1.6, 0.8], ["termP", 4.3, 0.8], ["chairB", 2.6, 3]],
    dayjob: [["data1", 0.3, -2.4], ["globe", 1.8, -2.1], ["data2", 3.4, -2.4], ["deskPCv", 0.4, 0.5], ["chairB", 1.3, 1.8], ["termB", 3.2, 0.3]],
    finance: [["chart", 0.2, -2.3], ["data2", 3.1, -2.4], ["deskPC", 0.4, 0.5], ["chairG", 1.3, 1.8], ["drawersItems", 3.3, 0.2]],
    forge: [["cabRed", 0.5, -2], ["cabPurple", 2.5, -2], ["cabRed", 4.5, -2], ["cabPurple", 6.5, -2], ["cabRed", 8.5, -2], ["termP", 10.5, 0.2], ["chairRside", 11.8, 0.6]],
  };
  // Where people stand (tiles from floor origin). Stations when working, home when idle, bunk when asleep.
  const SPOT = {
    architect: { room: "command", home: [7.4, 4.5], station: [2.4, 1.8] },
    clair:     { room: "comms",   home: [3.3, 3.3], station: [2.2, 2.6] },
    intel:     { room: "intel",   home: [8, 3.5], station: [2, 2.4], bunk: [9.2, 3.7] },
    revenue:   { room: "revenue", home: [6, 3.6], station: [3.9, 3.4], bunk: [10, 3.7] },
    learner:   { room: "archives", home: [6.6, 3.5], station: [2.4, 2], bunk: [7.4, 3.8] },
    forge:     { room: "forge",   home: [8, 3.5], station: [4.5, 2.2] },
    observer:  { room: "archives", home: [2.2, 3.3], station: [4.6, 2.4] },
    dayjob:    { room: "dayjob", home: [3.6, 3.2], station: [1.8, 2.3], bunk: [4.1, 3.4] },
    finance:   { room: "finance", home: [3.5, 3.2], station: [1.8, 2.3], bunk: [4.1, 3.4] },
  };
  const SLOTS = { ready: [[2, 2.2], [4, 2.2], [6, 2.2], [9, 2.4], [11, 2.4], [13, 2.4]], codebay: [[3.3, 3.6], [5, 3.6], [1, 3.6], [6, 2]] };

  let canvas, ctx, dpr = 1, view = { x: 0, y: 0, z: 1 }, data = null, hover = null, selected = null;
  let hits = [], raf = 0, poll = 0, dragging = null, lastApprovals = 0, sound = false;
  const motion = {};               // agent id -> current [tx, ty] in map tiles
  let pings = [], seenEvent = 0, lastFrame = 0, sparks = [];
  const timelines = {};            // mission id -> { at, mission }
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let world = null, wctx = null, staticLayer = null, light = null, lctx = null;

  // Colour grades: a blend over the lit scene. Picked in the Look panel, remembered per device.
  const GRADES = {
    original: { name: "Original" },
    nightops: { name: "Night ops", ops: [["multiply", "#3B4C9A", 0.45], ["saturation", "#808080", 0.25]] },
    nightvision: { name: "Night vision", ops: [["color", "#46FF7E", 0.55], ["multiply", "#6FAF7A", 0.35]] },
    amber: { name: "Amber noir", ops: [["color", "#E8A33D", 0.4], ["multiply", "#B07A4A", 0.3]] },
    neon: { name: "Neon", ops: [["overlay", "#5A2A8A", 0.35], ["saturation", "#FF00AA", 0.25]] },
    ghost: { name: "Ghost", ops: [["saturation", "#808080", 0.85], ["multiply", "#7E8CA8", 0.25]] },
  };
  const look = { grade: "original", dark: 80, glow: 100 };
  try { Object.assign(look, JSON.parse(localStorage.getItem("wetworks.look") || "{}")); } catch {}
  if (!GRADES[look.grade]) look.grade = "original";
  function saveLook() { try { localStorage.setItem("wetworks.look", JSON.stringify(look)); } catch {} }
  function applyGrade(g, W, H) {
    const gr = GRADES[look.grade];
    (gr.ops || []).forEach(([mode, color, a]) => {
      g.globalCompositeOperation = mode; g.globalAlpha = a; g.fillStyle = color; g.fillRect(0, 0, W, H);
    });
    g.globalCompositeOperation = "source-over"; g.globalAlpha = 1;
  }

  function mix(hex, a) { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; }
  function roundRect(x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  // world pixels -> screen pixels
  const toScreen = (px, py) => [px * view.z + view.x, py * view.z + view.y];

  function fit() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    const z = Math.min((w - 24) / (MAP_W * T), (h - 16) / (MAP_H * T));
    view.z = z;
    view.x = Math.round((w - MAP_W * T * view.z) / 2);
    view.y = Math.round((h - MAP_H * T * view.z) / 2 + 6);
  }

  // ---- static layer: floors, walls, furniture (redrawn only when art loads) ------------------------
  function sprite(g, name, x, y, flip = false) {
    const s = S[name]; if (!s) return;
    const [sh, sx, sy, sw, shh] = s;
    if (artReady) {
      if (flip) { g.save(); g.translate(Math.round(x) + sw, Math.round(y)); g.scale(-1, 1); g.drawImage(img[sh], sx, sy, sw, shh, 0, 0, sw, shh); g.restore(); }
      else g.drawImage(img[sh], sx, sy, sw, shh, Math.round(x), Math.round(y), sw, shh);
    } else {  // stand-in block until the art pack is installed
      g.fillStyle = "#2B2547"; g.fillRect(Math.round(x), Math.round(y), sw, shh);
      g.strokeStyle = C.ink; g.strokeRect(Math.round(x) + 0.5, Math.round(y) + 0.5, sw - 1, shh - 1);
    }
  }
  function fillFloor(g, kind, x, y, w, h) {
    const f = FLOOR[kind];
    if (!artReady || !f) { g.fillStyle = "#231E3B"; g.fillRect(x, y, w, h); return; }
    const [fx, fy, n] = f;
    for (let yy = y; yy < y + h; yy += n) for (let xx = x; xx < x + w; xx += n) {
      g.drawImage(img.A, fx, fy, Math.min(n, x + w - xx), Math.min(n, y + h - yy), xx, yy, Math.min(n, x + w - xx), Math.min(n, y + h - yy));
    }
  }
  function wallFace(g, style, x, y, w) {
    const f = WALLFACE[style];
    if (!artReady || !f) { g.fillStyle = "#3A3366"; g.fillRect(x, y, w, FACE * T); return; }
    for (let xx = x; xx < x + w; xx += 48) g.drawImage(img.A, f[0], f[1], Math.min(48, x + w - xx), 48, xx, y, Math.min(48, x + w - xx), 48);
  }
  function wallTop(g, x, y, w, h) {
    g.fillStyle = C.walltop; g.fillRect(x, y, w, h);
    g.fillStyle = C.walledge; g.fillRect(x, y, w, 1); g.fillRect(x, y, 1, h);
    g.fillStyle = C.ink; g.fillRect(x, y + h - 1, w, 1); g.fillRect(x + w - 1, y, 1, h);
  }
  function hazard(g, x, y, w, h) {
    g.fillStyle = "#2A1E0C"; g.fillRect(x, y, w, h);
    g.save(); g.beginPath(); g.rect(x, y, w, h); g.clip();
    g.strokeStyle = "#C98A2E"; g.lineWidth = 3;
    for (let k = -h; k < w + h; k += 8) { g.beginPath(); g.moveTo(x + k, y + h); g.lineTo(x + k + h, y); g.stroke(); }
    g.restore();
    g.strokeStyle = C.ink; g.lineWidth = 1; g.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  }
  function buildStatic() {
    const g = staticLayer.getContext("2d");
    g.imageSmoothingEnabled = false;
    g.fillStyle = C.night; g.fillRect(0, 0, MAP_W * T, MAP_H * T);
    // corridors: dark plating
    CORRIDORS.forEach(([x, y, w, h]) => {
      fillFloor(g, "checkNavy", x * T, y * T, w * T, h * T);
      g.fillStyle = "rgba(8,6,20,0.45)"; g.fillRect(x * T, y * T, w * T, h * T);
    });
    Object.entries(ROOM).forEach(([id, R]) => {
      const [x, y, w, h] = R.r.map((v) => v * T);
      if (R.sealed) {
        g.fillStyle = "#0B0A14"; g.fillRect(x, y, w, h);
        wallTop(g, x, y, w, T); wallTop(g, x, y, T, h); wallTop(g, x + w - T, y, T, h); wallTop(g, x, y + h - T, w, T);
        g.fillStyle = "#15122A"; g.fillRect(x + T, y + T, w - 2 * T, FACE * T);
        hazard(g, x + w / 2 - 20, y + T + 6, 40, FACE * T - 6);       // closed blast door
        return;
      }
      const fl = inner(id);
      fillFloor(g, R.floor, fl.x * T, fl.y * T, fl.w * T, fl.h * T);
      wallFace(g, R.wall, x + T, y + T, w - 2 * T);
      g.fillStyle = "rgba(0,0,0,0.25)"; g.fillRect(x + T, y + T + FACE * T, w - 2 * T, 4);          // shadow under the face
      wallTop(g, x, y, w, T); wallTop(g, x, y, T, h); wallTop(g, x + w - T, y, T, h); wallTop(g, x, y + h - T, w, T);
      const [side, off] = R.door;
      if (side === "s") {  // opening in the bottom wall
        fillFloor(g, R.floor, x + off * T, y + h - T, 2 * T, T);
      } else {              // opening through the top wall and its face
        fillFloor(g, R.floor, x + off * T, y, 2 * T, (1 + FACE) * T);
        g.fillStyle = "rgba(0,0,0,0.35)"; g.fillRect(x + off * T, y, 2 * T, (1 + FACE) * T);
      }
      (PROPS[id] || []).forEach(([name, tx, ty]) => sprite(g, name, (fl.x + tx) * T, (fl.y + ty) * T));
    });
    // beds: the pack has none, so these are drawn here in its style until the AI art lands
    Object.values(SPOT).forEach((sp) => { if (sp.bunk) bed(g, (inner(sp.room).x + sp.bunk[0] - 0.6) * T, (inner(sp.room).y + sp.bunk[1] - 1.4) * T); });
  }
  function bed(g, x, y) {
    x = Math.round(x); y = Math.round(y);
    g.fillStyle = C.ink; g.fillRect(x, y, 22, 30);
    g.fillStyle = "#4A4270"; g.fillRect(x + 1, y + 1, 20, 28);
    g.fillStyle = "#D7DCEF"; g.fillRect(x + 3, y + 2, 16, 6);         // pillow
    g.fillStyle = "#2F3A5E"; g.fillRect(x + 1, y + 11, 20, 18);       // blanket
    g.fillStyle = "#3D4A74"; g.fillRect(x + 1, y + 11, 20, 2);
  }

  // ---- people (drawn in the pack's style: dark outline, chunky heads) --------------------------
  const LOOK = {
    commander: { coat: "#2C4A3A", coat2: "#1F3529", skin: "#E0B894", hair: "#2A2426", pants: "#1B1B2E", accent: C.phosphor, long: true },
    sleeper:   { coat: "#3B3F66", coat2: "#2B2E4E", skin: "#D6A47E", hair: "#3A2A22", pants: "#22223A", accent: "#9BA3D9", hood: true },
    splinter:  { coat: "#283238", coat2: "#1B2328", skin: "#C9967A", hair: "#151A1E", pants: "#1A2024", accent: C.phosphor, visor: true },
    forge:     { coat: "#5A3A2A", coat2: "#3F281D", skin: "#E3B08C", hair: "#2B1E18", pants: "#2A1E1A", accent: C.signal, goggles: true },
  };
  const spriteCache = {};
  // One 16x24 frame; pose: idle | walk0..3 | work0..1
  function person(kind, pose) {
    const key = kind + pose;
    if (spriteCache[key]) return spriteCache[key];
    const L = LOOK[kind] || LOOK.sleeper, c = document.createElement("canvas"); c.width = 16; c.height = 24;
    const g = c.getContext("2d");
    const px = (x, y, w, h, col) => { g.fillStyle = col; g.fillRect(x, y, w, h); };
    const step = pose.startsWith("walk") ? [0, 1, 0, -1][+pose.slice(4)] : 0;
    const work = pose.startsWith("work") ? +pose.slice(4) : -1;
    // outline pass: draw everything 1px bigger in ink first
    const body = (o) => {
      // legs
      px(5 - o, 17 - o, 3 + 2 * o, 5 + 2 * o - Math.max(0, step), o ? C.ink : L.pants);
      px(9 - o, 17 - o, 3 + 2 * o, 5 + 2 * o - Math.max(0, -step), o ? C.ink : L.pants);
      // torso / coat
      px(4 - o, 10 - o, 9 + 2 * o, (L.long ? 10 : 8) + 2 * o, o ? C.ink : L.coat);
      // arms
      if (work >= 0) { px(12 - o, 11 - o, 3 + 2 * o, 3 + 2 * o, o ? C.ink : L.coat2); px(2 - o, 11 - o, 3 + 2 * o, 3 + 2 * o, o ? C.ink : L.coat2); }
      else { px(2 - o, 10 - o + step, 2 + 2 * o, 7 + 2 * o, o ? C.ink : L.coat2); px(13 - o, 10 - o - step, 2 + 2 * o, 7 + 2 * o, o ? C.ink : L.coat2); }
      // head (big, Streets-of-Rogue chunky)
      px(4 - o, 1 - o, 9 + 2 * o, 9 + 2 * o, o ? C.ink : L.skin);
    };
    body(1); body(0);
    // boots
    px(5, 21 - Math.max(0, step), 3, 1, C.ink); px(9, 21 - Math.max(0, -step), 3, 1, C.ink);
    // coat details
    px(4, 16, 9, 1, L.coat2); px(8, 10, 1, L.long ? 10 : 8, L.coat2);
    if (L.long) px(4, 18, 9, 2, L.coat);
    px(6, 10, 5, 1, L.accent);                                   // collar light
    // hands
    if (work >= 0) { px(13 + work, 13, 2, 1, L.skin); px(2 - work + 1, 13, 2, 1, L.skin); }
    else { px(2, 16 + step, 2, 1, L.skin); px(13, 16 - step, 2, 1, L.skin); }
    // hair / hood / helmet
    if (L.hood) { px(3, 0, 11, 4, L.coat); px(3, 0, 2, 9, L.coat); px(12, 0, 2, 9, L.coat); px(4, 1, 9, 2, L.coat2); }
    else px(4, 1, 9, 3, L.hair);
    if (L.visor) { px(3, 0, 11, 4, "#20282C"); px(4, 4, 9, 2, L.accent); }
    else { px(6, 5, 1, 2, C.ink); px(10, 5, 1, 2, C.ink); }      // eyes
    if (L.goggles) { px(4, 2, 9, 1, "#2B1E18"); px(5, 1, 3, 2, L.accent); px(9, 1, 3, 2, L.accent); }
    if (L.long) { px(12, 4, 1, 3, C.ink); px(11, 7, 2, 1, C.ink); }  // earpiece and mic
    spriteCache[key] = c;
    return c;
  }

  function placeAgents(dt) {
    const slot = { ready: 0, codebay: 0 }, seen = new Set();
    const out = data.agents.map((a) => {
      const sp = SPOT[a.id];
      const room = a.room in ROOM ? a.room : (sp ? sp.room : "ready");
      const fl = inner(room);
      let rel = sp ? sp.home : null;
      if (a.kind === "splinter") { const s = SLOTS[room] || SLOTS.ready, i = slot[room] || 0; slot[room] = i + 1; rel = s[i % s.length]; }
      if (sp && ["working", "thinking"].includes(a.state)) rel = sp.station;
      if (sp && sp.bunk && a.state === "asleep") rel = sp.bunk;
      const target = [fl.x + (rel ? rel[0] : 3), fl.y + (rel ? rel[1] : 2)];
      seen.add(a.id);
      let cur = motion[a.id];
      if (!cur) {  // new Splinters walk in through their room's door
        const R = ROOM[room];
        cur = motion[a.id] = a.kind === "splinter" && R && R.door ? [R.r[0] + R.door[1] + 1, R.door[0] === "s" ? R.r[1] + R.r[3] : R.r[1] + 1 + FACE] : [...target];
      }
      const dx = target[0] - cur[0], dy = target[1] - cur[1], dist = Math.hypot(dx, dy);
      const step = reduced ? dist : Math.min(dist, 2.6 * dt);
      if (dist > 0.001) { cur[0] += (dx / dist) * step; cur[1] += (dy / dist) * step; }
      const walking = dist > 0.05;
      const prev = a.id in facingMem ? facingMem[a.id] : 1;
      const facing = Math.abs(dx) > 0.05 ? (dx > 0 ? 1 : -1) : prev; facingMem[a.id] = facing;
      return { ...a, pos: [cur[0], cur[1]], walking, facing, inBunk: sp && sp.bunk && a.state === "asleep" && !walking };
    });
    Object.keys(motion).forEach((id) => { if (!seen.has(id)) delete motion[id]; });
    return out;
  }
  const facingMem = {};

  function drawAgent(g, a, now) {
    const x = Math.round(a.pos[0] * T), y = Math.round(a.pos[1] * T);   // feet
    if (a.inBunk) {  // asleep under the blanket
      g.fillStyle = C.ink; g.fillRect(x - 6, y - 21, 11, 10);
      g.fillStyle = (LOOK[a.kind] || LOOK.sleeper).skin; g.fillRect(x - 5, y - 20, 9, 8);
      g.fillStyle = (LOOK[a.kind] || LOOK.sleeper).coat; g.fillRect(x - 5, y - 20, 9, 3);
      return { x: x - 8, y: y - 24, w: 18, h: 26 };
    }
    g.fillStyle = "rgba(0,0,0,0.45)"; g.fillRect(x - 6, y - 1, 12, 3);
    if (a.id === "clair") {  // C.L.a.i.R: a mint flame wisp
      const t = now / 1000, bob = reduced ? 0 : Math.round(Math.sin(t * 2.4) * 1.5), cy = y - 16 + bob;
      const fl = reduced ? 0 : Math.round(Math.sin(t * 9) * 1);
      g.fillStyle = "#1E6B4A"; g.fillRect(x - 5, cy - 2, 11, 10); g.fillRect(x - 3, cy - 6 - fl, 7, 4); g.fillRect(x - 1, cy - 9 - fl, 3, 3);
      g.fillStyle = "#7CF0B4"; g.fillRect(x - 4, cy - 1, 9, 8); g.fillRect(x - 2, cy - 5 - fl, 5, 4); g.fillRect(x, cy - 8 - fl, 1, 3);
      g.fillStyle = "#D8FFEA"; g.fillRect(x - 2, cy + 1, 5, 4);
      g.fillStyle = C.ink; g.fillRect(x - 2, cy + 2, 1, 2); g.fillRect(x + 2, cy + 2, 1, 2);
      g.fillStyle = VENDOR[a.vendor] || C.fog; g.fillRect(x + 6, cy + 6, 2, 2);
      return { x: x - 8, y: cy - 12, w: 17, h: 24 };
    }
    const t = now / 1000;
    let pose = "idle";
    if (a.walking) pose = "walk" + (reduced ? 0 : Math.floor(t * 8) % 4);
    else if (a.state === "working" || a.state === "thinking") pose = "work" + (reduced ? 0 : Math.floor(t * 6) % 2);
    const spr = person(a.kind, pose);
    const breathe = !reduced && pose === "idle" && Math.floor(t * 1.5) % 2 ? 1 : 0;
    g.save();
    if (a.facing < 0) { g.translate(x, 0); g.scale(-1, 1); g.drawImage(spr, -8, y - 23 + breathe); }
    else g.drawImage(spr, x - 8, y - 23 + breathe);
    g.restore();
    g.fillStyle = VENDOR[a.vendor] || C.fog; g.fillRect(x + (a.facing < 0 ? 3 : -6), y - 12, 2, 2);   // vendor patch
    return { x: x - 8, y: y - 24, w: 16, h: 25 };
  }

  // ---- lights -----------------------------------------------------------------------------------
  function lights(now) {
    const out = [], g = data.hud.gpu || {};
    const pulse = (p) => (reduced ? 1 : 0.85 + 0.15 * Math.sin(now / p));
    Object.entries(ROOM).forEach(([id, R]) => {
      if (R.sealed) return;
      const fl = inner(id), here = data.agents.filter((a) => a.room === id);
      const busy = here.some((a) => ["working", "thinking"].includes(a.state));
      const alert = here.some((a) => a.state === "blocked");
      const asleep = here.length && here.every((a) => a.state === "asleep");
      const cx = (fl.x + fl.w / 2) * T, cy = (fl.y + fl.h / 2 - 0.5) * T;
      out.push({ x: cx, y: cy, r: Math.max(fl.w, fl.h) * T * (busy ? 0.75 : 0.6), a: asleep ? 0.35 : busy ? 0.95 : 0.7, color: R.tint, tint: busy ? 0.1 : 0.05 });
      if (alert) out.push({ x: cx, y: cy, r: fl.w * T * 0.6, a: 0, color: C.signal, tint: reduced ? 0.22 : 0.12 + 0.12 * Math.sin(now / 260) });
      (PROPS[id] || []).forEach(([name, tx, ty]) => {
        const glow = S[name] && S[name][5] && S[name][5].glow;
        if (glow) out.push({ x: (fl.x + tx) * T + glow[0], y: (fl.y + ty) * T + glow[1], r: 26, a: 0.55 * (busy ? 1 : 0.7) * pulse(500 + tx * 60), color: glow[2], tint: 0.18 });
      });
    });
    // the Forge glows with real GPU load
    const heat = Math.max((g.util || 0) / 100, g.training ? 0.8 : 0);
    const ff = inner("forge");
    if (heat > 0.05) out.push({ x: (ff.x + 5) * T, y: (ff.y - 0.5) * T, r: 110, a: 0.6 * heat, color: C.signal, tint: 0.3 * heat * pulse(300) });
    // sealed doors: a little amber status light
    Object.values(ROOM).forEach((R) => { if (R.sealed) out.push({ x: (R.r[0] + R.r[2] / 2) * T, y: (R.r[1] + 2.5) * T, r: 44, a: 0.55, color: C.amber, tint: 0.16 * pulse(700) }); });
    // corridor strip lights
    CORRIDORS.forEach(([x, y, w, h]) => { for (let k = 3; k < w; k += 6) out.push({ x: (x + k) * T, y: (y + h / 2) * T, r: 40, a: 0.5, color: "#7FB2E5", tint: 0.05 }); });
    return out;
  }
  function drawLighting(placed, now) {
    const W = MAP_W * T, H = MAP_H * T;
    lctx.globalCompositeOperation = "source-over";
    lctx.fillStyle = `rgba(6,5,16,${look.dark / 100})`; lctx.fillRect(0, 0, W, H);
    lctx.globalCompositeOperation = "destination-out";
    const ls = lights(now);
    placed.forEach((a) => { if (a.id === "clair") ls.push({ x: a.pos[0] * T, y: a.pos[1] * T - 14, r: 40, a: 0.8, color: "#7CF0B4", tint: 0.25 }); });
    ls.forEach((L) => {
      if (!L.a) return;
      const gr = lctx.createRadialGradient(L.x, L.y, 2, L.x, L.y, L.r);
      gr.addColorStop(0, `rgba(0,0,0,${L.a})`); gr.addColorStop(1, "rgba(0,0,0,0)");
      lctx.fillStyle = gr; lctx.fillRect(L.x - L.r, L.y - L.r, 2 * L.r, 2 * L.r);
    });
    wctx.drawImage(light, 0, 0);
    // coloured bloom on top
    wctx.globalCompositeOperation = "lighter";
    ls.forEach((L) => {
      if (!L.tint) return;
      const gr = wctx.createRadialGradient(L.x, L.y, 1, L.x, L.y, L.r);
      gr.addColorStop(0, mix(L.color, Math.min(1, L.tint * look.glow / 100))); gr.addColorStop(1, mix(L.color, 0));
      wctx.fillStyle = gr; wctx.fillRect(L.x - L.r, L.y - L.r, 2 * L.r, 2 * L.r);
    });
    wctx.globalCompositeOperation = "source-over";
  }

  // ---- marks drawn in screen space (crisp at any zoom) ----------------------------------------------
  function stateMark(a, sx, sy, now) {
    const z = view.z;
    if (a.state === "thinking") {
      const k = reduced ? 0 : Math.floor(now / 300) % 3;
      for (let i = 0; i < 3; i++) { ctx.fillStyle = i === k ? C.phosphor : mix(C.phosphor, 0.35); ctx.fillRect(sx - 7 * z + i * 5 * z, sy, 3 * z, 3 * z); }
    } else if (a.state === "blocked") {
      const on = reduced || Math.floor(now / 450) % 2 === 0;
      ctx.fillStyle = on ? C.signal : mix(C.signal, 0.3); ctx.fillRect(sx - 2 * z, sy - 4 * z, 4 * z, 6 * z); ctx.fillRect(sx - 2 * z, sy + 3 * z, 4 * z, 2 * z);
    } else if (a.state === "asleep" && !reduced) {
      const k = (now / 1400) % 1;
      ctx.fillStyle = mix(C.paper, 0.8 * (1 - k)); ctx.font = `600 ${Math.round(8 * z)}px "IBM Plex Mono", monospace`;
      ctx.textAlign = "left"; ctx.fillText("z", sx + 4 * z, sy - k * 10 * z);
    }
  }
  function nameTag(text, sx, sy, color = C.paper) {
    ctx.font = `600 ${Math.max(10, Math.round(4.5 * view.z))}px "Chakra Petch", sans-serif`;
    const w = ctx.measureText(text).width + 10, h = Math.max(15, Math.round(6.5 * view.z));
    ctx.fillStyle = "rgba(7,6,14,0.85)"; ctx.fillRect(sx - w / 2, sy, w, h);
    ctx.fillStyle = color; ctx.textAlign = "center"; ctx.fillText(text, sx, sy + h - Math.round(h * 0.28));
  }

  // ---- frame ----------------------------------------------------------------------------------------
  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (!data || document.hidden) return;
    const W = MAP_W * T, H = MAP_H * T;
    if (!world) {
      world = document.createElement("canvas"); world.width = W; world.height = H; wctx = world.getContext("2d");
      light = document.createElement("canvas"); light.width = W; light.height = H; lctx = light.getContext("2d");
    }
    if (!staticLayer) { staticLayer = document.createElement("canvas"); staticLayer.width = W; staticLayer.height = H; buildStatic(); }
    const dt = lastFrame ? Math.min(0.1, (now - lastFrame) / 1000) : 0; lastFrame = now;
    hits = [];
    wctx.imageSmoothingEnabled = false;
    wctx.drawImage(staticLayer, 0, 0);
    // red phone on the Command Deck table: rings when something needs Patrick
    const cf = inner("command"), ringing = data.hud.approvals.length > 0;
    const phone = [(cf.x + 9.2) * T, (cf.y + 1.3) * T];
    const blink = reduced ? 1 : Math.floor(now / 250) % 2;
    wctx.fillStyle = C.ink; wctx.fillRect(phone[0] - 1, phone[1] - 1, 9, 7);
    wctx.fillStyle = ringing && blink ? "#FF5A47" : "#8E2A22"; wctx.fillRect(phone[0], phone[1], 7, 5);
    wctx.fillStyle = "#3A0F0B"; wctx.fillRect(phone[0] + 1, phone[1] + 1, 5, 1);
    const placed = placeAgents(dt);
    const boxes = {};
    placed.sort((a, b) => a.pos[1] - b.pos[1]).forEach((a) => { boxes[a.id] = drawAgent(wctx, a, now); });
    drawSparks(dt);
    drawLighting(placed, now);
    applyGrade(wctx, W, H);
    if (data.hud.halted) { wctx.fillStyle = "rgba(255,77,61,0.07)"; wctx.fillRect(0, 0, W, H); }

    // blit the world, pixel-sharp
    const w = canvas.clientWidth, h = canvas.clientHeight;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = C.night; ctx.fillRect(0, 0, w, h);
    ctx.drawImage(world, view.x, view.y, W * view.z, H * view.z);

    // hit areas, labels and marks in screen space
    Object.entries(ROOM).forEach(([id, R]) => {
      const [x, y, rw, rh] = R.r.map((v) => v * T);
      const [sx, sy] = toScreen(x, y), sw = rw * view.z, sh = rh * view.z;
      hits.push({ type: "room", id, poly: [[sx, sy], [sx + sw, sy], [sx + sw, sy + sh], [sx, sy + sh]] });
      const isSel = selected && selected.type === "room" && selected.id === id, isHov = hover && hover.type === "room" && hover.id === id;
      if (isSel || isHov) { ctx.strokeStyle = isSel ? C.amber : mix(C.paper, 0.5); ctx.lineWidth = 2; ctx.strokeRect(sx + 1, sy + 1, sw - 2, sh - 2); }
      const name = (data.rooms.find((r) => r.id === id) || { name: id }).name;
      nameTag(R.sealed ? `${name} · sealed` : name, sx + sw / 2, sy - Math.max(8, 3 * view.z), R.sealed ? C.amber : R.tint);
    });
    if (ringing) { const [px, py] = toScreen(phone[0] - 6, phone[1] - 6); hits.push({ type: "approvals", rect: [px, py, 20 * view.z, 18 * view.z] }); }
    placed.forEach((a) => {
      const b = boxes[a.id]; if (!b) return;
      const [sx, sy] = toScreen(b.x, b.y);
      hits.push({ type: "agent", id: a.id, rect: [sx, sy, b.w * view.z, b.h * view.z] });
      const sel = selected && selected.type === "agent" && selected.id === a.id, hov = hover && hover.type === "agent" && hover.id === a.id;
      if (sel) { ctx.strokeStyle = C.amber; ctx.lineWidth = 2; ctx.strokeRect(sx - 2, sy - 2, b.w * view.z + 4, b.h * view.z + 4); }
      stateMark(a, sx + (b.w * view.z) / 2, sy - 6 * view.z, now);
      if (sel || hov || a.kind === "commander") nameTag(a.name, sx + (b.w * view.z) / 2, sy + b.h * view.z + 2);
    });
    drawPings(placed, now);
    ctx.fillStyle = scanlines(); ctx.fillRect(0, 0, w, h);
  }

  let scan = null;
  function scanlines() {
    if (scan) return scan;
    const c = document.createElement("canvas"); c.width = 4; c.height = 3;
    const g = c.getContext("2d"); g.fillStyle = "rgba(0,0,0,0.14)"; g.fillRect(0, 2, 4, 1);
    scan = ctx.createPattern(c, "repeat");
    return scan;
  }

  // Sparks fly off the Forge's racks when the GPU works hard.
  function drawSparks(dt) {
    const g = data.hud.gpu || {}, heat = Math.max((g.util || 0) / 100, g.training ? 0.8 : 0), ff = inner("forge");
    if (!reduced && heat > 0.25 && Math.random() < heat * 0.5) {
      const i = Math.floor(Math.random() * 5);
      sparks.push({ x: (ff.x + 1.5 + i * 2) * T, y: (ff.y + 0.4) * T, vx: (Math.random() - 0.5) * 30, vy: -20 - Math.random() * 30, life: 1 });
    }
    sparks = sparks.filter((p) => (p.life -= dt * 1.6) > 0);
    sparks.forEach((p) => {
      p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 90 * dt;
      wctx.fillStyle = `rgba(255,${150 + Math.floor(90 * p.life)},80,${p.life})`; wctx.fillRect(Math.round(p.x), Math.round(p.y), 1, 1);
    });
  }

  // A ring pops on an agent when something happens (new activity from them).
  const PING = { done: C.phosphor, start: C.phosphor, error: C.signal, warn: C.amber, info: C.paper };
  function drawPings(placed, now) {
    pings = pings.filter((p) => now - p.t < 1600);
    pings.forEach((p) => {
      const a = placed.find((x) => x.id === p.agent) || (p.agent === "splinter" && placed.find((x) => x.kind === "splinter"));
      if (!a) return;
      const k = (now - p.t) / 1600, [x, y] = toScreen(a.pos[0] * T, a.pos[1] * T - 10);
      ctx.strokeStyle = mix(PING[p.kind] || C.paper, 0.85 * (1 - k)); ctx.lineWidth = 2;
      const r = (8 + 18 * k) * view.z; ctx.strokeRect(x - r, y - r, 2 * r, 2 * r);
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
      const [x, y] = pos(e), k = Math.exp(-e.deltaY * 0.0015), z = Math.min(8, Math.max(0.75, view.z * k));
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
      if (a.watch) add("Watching", a.watch.paused ? "Paused" : a.watch.devices.length ? a.watch.devices.join(", ") : "No helper running");
      if (a.support) add("Support", `${a.support.open} open · ${a.support.fixes} fixes · ${a.support.docs} files`);
      if (a.money) add("Money", `${a.money.transactions} transactions · ${a.money.due_this_week} due this week`
        + (a.money.uncategorized ? ` · ${a.money.uncategorized} to sort` : ""));
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
      if (a.id === "finance" || a.id === "dayjob" || a.id === "observer") {
        const b = el("button", "small", { finance: "Open Finance", dayjob: "Open Day Job", observer: "Open Observer" }[a.id]); b.type = "button";
        b.addEventListener("click", () => window.WWFloor.toTactical(a.id));
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
        if (r.id === "finance" || r.id === "dayjob") {
          body.append(el("p", "muted", r.id === "finance" ? "Your money stays on the MSI: only the local model reads it."
            : "Work material stays on the MSI: patient details are stripped and only the local model reads it."));
          const b = el("button", "small", r.id === "finance" ? "Open Finance" : "Open Day Job"); b.type = "button";
          b.addEventListener("click", () => window.WWFloor.toTactical(r.id));
          body.append(b);
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
    loadArt();
    bindInput(); bindKill(); bindLook();
    $("#fh-missions").addEventListener("click", () => { selected = { type: "room", id: "ready" }; openPanel(); });
    load();
    poll = setInterval(() => { if (isOpen() || $("#floor-map")?.offsetParent) load(); }, 3000);
  }
  function show() {
    size(); fit();
    if (!raf) raf = requestAnimationFrame(frame);
  }
  function hide() { cancelAnimationFrame(raf); raf = 0; }

  function bindLook() {
    const btn = $("#fh-look"), panel = $("#fh-look-panel"), box = $("#look-grades");
    btn.addEventListener("click", () => { panel.hidden = !panel.hidden; btn.setAttribute("aria-expanded", String(!panel.hidden)); });
    Object.entries(GRADES).forEach(([id, g]) => {
      const b = el("button", "chip", g.name); b.type = "button"; b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(look.grade === id));
      b.addEventListener("click", () => {
        look.grade = id; saveLook();
        box.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
      });
      box.append(b);
    });
    [["#look-dark", "dark"], ["#look-glow", "glow"]].forEach(([sel, key]) => {
      const r = $(sel); r.value = look[key];
      r.addEventListener("input", () => { look[key] = +r.value; saveLook(); });
    });
  }

  function inspect(type, id) { selected = { type, id }; openPanel(); }
  window.WWFloor = { start, show, hide, load, inspect, toTactical: () => {} };
})();
