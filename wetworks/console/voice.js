// C.L.a.i.R's voice in the console: hold-to-talk, plus hands-free mode with a wake phrase,
// stop phrases and an idle timeout. Speech-to-text runs on the MSI (Whisper); her voice is
// Azure's en-IE-EmilyNeural, fetched from the MSI so the Azure key never reaches the phone.

// ---- Phrase matching (pure; also unit-tested in Node) -------------------------------
const NAME_VARIANTS = "clair|claire|clare|clear|klair|klaire|claira|clara|klara|cler|clare's|claires";

function normalize(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/[‘’]/g, "'")
    .replace(/c\.\s*l\.\s*a\.\s*i\.\s*r\.?/g, "clair")
    .replace(/[^a-z0-9' ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// Returns null if the wake phrase isn't in `text`, else { rest } with whatever was said after it.
function matchWake(text, wakePhrase) {
  const t = normalize(text);
  const w = normalize(wakePhrase);
  let idx = -1, len = 0;
  const exact = w ? t.match(new RegExp(`\\b${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`)) : null;
  if (exact) { idx = exact.index; len = exact[0].length; }
  else {
    const m = t.match(new RegExp(`\\b(?:hey|hi|hello|ok|okay|yo|oi)\\s+(?:${NAME_VARIANTS})\\b`));
    if (m) { idx = m.index; len = m[0].length; }
  }
  if (idx < 0) return null;
  return { rest: t.slice(idx + len).replace(/^[\s,]+/, "").trim() };
}

// True when the utterance is a stop phrase, said on its own (not inside a longer request).
function matchStop(text, phrases) {
  const t = normalize(text);
  if (!t) return false;
  const words = t.split(" ").length;
  return (phrases || []).some((p) => {
    const n = normalize(p);
    return n && t.includes(n) && words <= n.split(" ").length + 2;
  });
}

if (typeof module !== "undefined") module.exports = { normalize, matchWake, matchStop };

// ---- Browser side ----------------------------------------------------------------------
if (typeof window !== "undefined") (() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  let cfg = null;               // from /api/voice
  let ctx = null;               // AudioContext (unlocked on first touch, needed by iOS)
  let stream = null;            // microphone
  let analyser = null, vadTimer = null, idleTimer = null;
  let mode = "off";             // off | wake | convo
  let busy = false;             // transcribing / waiting / speaking: don't listen
  let recorder = null, chunks = [], recStart = 0;
  let floor = 0.01, loud = 0, quietSince = 0;
  let holdRec = null;

  const MIME = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus", "audio/webm"]
    .find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) || "";

  const say = (text, cls = "") => { const n = $("#voice-status"); n.textContent = text; n.className = `voice-status ${cls}`; };

  function token() { return window.WW ? window.WW.token() : ""; }
  async function rawFetch(path, opts = {}) {
    return fetch(path, { ...opts, headers: { Authorization: `Bearer ${token()}`, ...(opts.headers || {}) } });
  }

  // ---- Audio out ----
  function unlock() {
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state !== "running") ctx.resume().catch(() => {});
  }
  // iPhones pause ("interrupt") the audio engine when the microphone starts; wake it back up.
  async function wake() {
    if (ctx && ctx.state !== "running") { try { await ctx.resume(); } catch {} }
  }
  function decode(buf) {  // promise + callback forms, for older Safari
    return new Promise((ok, bad) => { const p = ctx.decodeAudioData(buf, ok, bad); if (p && p.then) p.then(ok, bad); });
  }
  function beep(up) {
    if (!ctx) return;
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine";
    const t = ctx.currentTime;
    o.frequency.setValueAtTime(up ? 660 : 880, t);
    o.frequency.linearRampToValueAtTime(up ? 990 : 520, t + 0.16);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
    o.connect(g).connect(ctx.destination);
    o.start(t); o.stop(t + 0.24);
  }
  let lastNote = "";
  async function speak(text) {
    if (!text) return;
    if (!cfg.speak) {  // maybe the Azure key was added since the page loaded
      try { cfg = await window.WW.api("/api/voice"); } catch {}
      if (!cfg.speak) { lastNote = "Her voice isn't set up on the MSI yet (Azure key), so replies are text only."; return; }
    }
    say("C.L.a.i.R is speaking", "live");
    let bytes;
    try {
      const r = await rawFetch("/api/voice/say", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      if (!r.ok) { const e = await r.json().catch(() => ({})); lastNote = e.error || "Voice unavailable; reply is on screen."; return; }
      bytes = await r.arrayBuffer();
    } catch { lastNote = "Couldn't reach her voice; reply is on screen."; return; }
    try {
      await wake();
      const buf = await decode(bytes.slice(0));
      await new Promise((done) => {
        const src = ctx.createBufferSource();
        src.buffer = buf; src.connect(ctx.destination);
        src.onended = done; src.start();
      });
    } catch {
      // Fallback: a plain audio element (some phones decode MP3 here but not in the audio engine)
      try {
        const url = URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" }));
        const a = new Audio(url);
        await a.play();
        await new Promise((done) => { a.onended = done; a.onerror = done; });
        URL.revokeObjectURL(url);
      } catch { lastNote = "Your phone blocked her audio. Tap the mic once, then try again."; }
    }
  }

  // ---- Audio in ----
  async function openMic() {
    if (stream) return stream;
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    return stream;
  }
  function closeMic() {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null; analyser = null;
  }
  function record(s) {
    const rec = new MediaRecorder(s, MIME ? { mimeType: MIME } : undefined);
    const parts = [];
    rec.ondataavailable = (e) => e.data && e.data.size && parts.push(e.data);
    const done = new Promise((res) => { rec.onstop = () => res(new Blob(parts, { type: rec.mimeType || MIME || "audio/webm" })); });
    rec.start();
    return { rec, done, started: performance.now() };
  }
  async function transcribe(blob) {
    say("Transcribing", "live");
    const r = await rawFetch("/api/voice/transcribe", { method: "POST", headers: { "Content-Type": blob.type || "audio/webm" }, body: blob });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "Couldn't transcribe that.");
    return (j.text || "").trim();
  }

  // ---- Talking to her ----
  async function send(text) {
    if (window.WW) window.WW.useClair();
    say("C.L.a.i.R is replying", "live");
    const r = await window.WW.api("/api/comms/clair", { method: "POST", body: JSON.stringify({ text, wait: true }) });
    window.WW.refresh();
    if (r.reply) await speak(r.reply.text);
  }

  // ---- Hold to talk ----
  async function holdStart(e) {
    e.preventDefault();
    if (busy || holdRec) return;
    unlock();
    try {
      const s = await openMic();
      await wake();
      holdRec = record(s);
      $("#mic").classList.add("rec");
      say("Listening. Let go to send", "rec");
    } catch {
      say("Microphone blocked. Allow it in your browser settings.", "warn");
    }
  }
  async function holdEnd(e) {
    if (e) e.preventDefault();
    if (!holdRec) return;
    const h = holdRec; holdRec = null;
    $("#mic").classList.remove("rec");
    h.rec.stop();
    const blob = await h.done;
    if (mode === "off") closeMic();
    if (performance.now() - h.started < 400) { say("Hold the button while you talk.", ""); return; }
    busy = true;
    try {
      const text = await transcribe(blob);
      if (!text) say("Didn't catch that. Try again?", "warn");
      else await send(text);
    } catch (err) { say(err.message, "warn"); }
    finally { busy = false; idleReset(); settle(); }
  }

  // ---- Hands-free ----
  function idleReset() {
    clearTimeout(idleTimer);
    if (mode === "convo") idleTimer = setTimeout(() => { beep(false); mode = "wake"; settle(); }, (cfg.idle_seconds || 90) * 1000);
  }
  function settle() {
    if (busy || holdRec) return;
    if (lastNote) { const n = lastNote; lastNote = ""; say(n, "warn"); setTimeout(() => { if (!busy) settle(); }, 6000); return; }
    if (mode === "wake") say(`Listening for "${cfg.wake_phrase}"`, "");
    else if (mode === "convo") say(`I'm listening. Say "${(cfg.stop_phrases || ["that's all"])[0]}" to stop`, "live");
    else say(cfg && cfg.hear ? "Hold the mic to talk" : "", "");
    const b = $("#handsfree");
    b.setAttribute("aria-pressed", String(mode !== "off"));
    b.textContent = mode === "off" ? "Hands-free" : "Hands-free on";
  }
  async function handleUtterance(blob) {
    busy = true;
    try {
      const text = await transcribe(blob);
      if (!text) return;
      if (mode === "wake") {
        const m = matchWake(text, cfg.wake_phrase);
        if (!m) { say(`Heard: "${text.slice(0, 60)}"`, ""); await new Promise((r) => setTimeout(r, 2500)); return; }
        mode = "convo"; beep(true); idleReset();
        if (m.rest.split(" ").length >= 2) await send(m.rest);
      } else if (mode === "convo") {
        if (matchStop(text, cfg.stop_phrases)) {
          mode = "wake"; clearTimeout(idleTimer);
          await speak("Grand, I'll be here.");
          beep(false);
        } else {
          await send(text);
        }
      }
    } catch (err) { say(err.message, "warn"); await new Promise((r) => setTimeout(r, 1500)); }
    finally { busy = false; idleReset(); settle(); }
  }
  function vadTick() {
    if (!analyser) return;
    const data = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(data);
    let sum = 0; for (const v of data) sum += v * v;
    const rms = Math.sqrt(sum / data.length);
    const now = performance.now();
    const vu = document.querySelector("#vu > span");
    if (vu) vu.style.width = `${Math.min(100, Math.round((rms / 0.12) * 100))}%`;
    if (ctx.state !== "running") { wake(); return; }
    if (busy || holdRec) {
      loud = 0;
      if (recorder) { const r = recorder; recorder = null; try { r.rec.stop(); } catch {} } // drop it
      return;
    }
    if (!recorder) {
      floor = floor * 0.95 + Math.min(rms, 0.05) * 0.05;
      loud = rms > Math.max(0.012, floor * 3) ? loud + 1 : 0;
      if (loud >= 3) { recorder = record(stream); quietSince = 0; say(mode === "convo" ? "Hearing you" : "…", "rec"); }
      return;
    }
    const quiet = rms < Math.max(0.008, floor * 2);
    if (quiet) { quietSince = quietSince || now; } else quietSince = 0;
    const len = now - recorder.started;
    if ((quietSince && now - quietSince > 1100) || len > 25000) {
      const r = recorder; recorder = null; loud = 0;
      r.rec.stop();
      r.done.then((blob) => { if (len < 500) { settle(); return; } handleUtterance(blob); });
    }
  }
  async function handsFreeOn() {
    unlock();
    try {
      const s = await openMic();
      await wake();
      const src = ctx.createMediaStreamSource(s);
      analyser = ctx.createAnalyser(); analyser.fftSize = 1024;
      src.connect(analyser);
      mode = "wake";
      if (window.WW) window.WW.useClair();
      document.querySelector("#vu").hidden = false;
      vadTimer = setInterval(vadTick, 50);
      settle();
    } catch {
      mode = "off"; say("Microphone blocked. Allow it in your browser settings.", "warn");
    }
  }
  function handsFreeOff() {
    mode = "off"; clearInterval(vadTimer); clearTimeout(idleTimer);
    const vu = document.querySelector("#vu"); if (vu) vu.hidden = true;
    if (recorder) { try { recorder.rec.stop(); } catch {} recorder = null; }
    closeMic(); settle();
  }

  // ---- Setup ----
  async function init() {
    try {
      const r = await window.WW.api("/api/voice");
      cfg = r;
    } catch { return; }
    const ok = cfg.hear && window.MediaRecorder && navigator.mediaDevices;
    if (ok) $("#voicebar").dataset.ready = "1";
    $("#voicebar").hidden = !ok || (window.WW && document.querySelector('.channel[aria-selected="true"]').dataset.channel !== "clair");
    $("#mic").hidden = !ok;
    if (!ok) return;
    const mic = $("#mic");
    mic.addEventListener("pointerdown", holdStart);
    ["pointerup", "pointercancel", "pointerleave"].forEach((ev) => mic.addEventListener(ev, holdEnd));
    mic.addEventListener("contextmenu", (e) => e.preventDefault());
    $("#handsfree").addEventListener("click", () => (mode === "off" ? handsFreeOn() : handsFreeOff()));
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && mode !== "off") { handsFreeOff(); say("Hands-free stops when the console isn't on screen.", "warn"); }
    });
    if (!cfg.speak) say("Hold the mic to talk. Her voice isn't set up yet, so replies are text only.", "");
    else settle();
  }
  window.WWVoice = { init, stop: handsFreeOff };
})();
