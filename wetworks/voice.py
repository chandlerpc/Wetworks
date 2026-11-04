"""C.L.a.i.R's voice.

Hearing: Whisper on the MSI's GPU (free, private).
Speaking: Azure neural TTS, voice en-IE-EmilyNeural (young Irish female). Azure's free tier
covers 500,000 characters a month; a guard stops speech (text only) before that runs out.
"""

import html
import logging
import re

import httpx

log = logging.getLogger("wetworks.voice")

# Words Whisper should expect; names especially are hard without a hint.
VOCAB_HINT = ("Clair, C.L.a.i.R, The Architect, Wetworks, Splinter, Sleeper, Intel, mission, "
              "Black Desert, Orlando, Sanford, Lake Mary.")
KEEP_RECORDINGS = 5

# What Whisper "hears" in silence, room noise or a TV: never a real request on their own.
GHOSTS = {"thank you", "thanks", "thank you very much", "thanks for watching", "thank you for watching",
          "thanks for watching and see you next time", "please subscribe", "subscribe", "you", "bye", "bye bye",
          "okay", "ok", "so", "um", "uh", "hmm", "the end", "music", "applause", "silence", "blank audio",
          "subtitles by the amara org community", "i'm sorry", "oh"}

# Words Whisper reliably splits or misspells in this house.
FIXUPS = [(r"\bwet ?works\b", "Wetworks"), (r"\bthe architects?\b", "The Architect"),
          (r"\bc\.? ?l\.? ?a\.? ?i\.? ?r\b", "Clair"), (r"\bsplinter cells?\b", "Splinter cell"),
          (r"\bthe forage\b", "The Forge"), (r"\bnova ?pacs\b", "NovaPACS"), (r"\bnova ?rad\b", "Novarad")]


def clean_transcript(text: str) -> str:
    """Drop Whisper's silence hallucinations and fix known misspellings."""
    t = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", text or "")  # [BLANK_AUDIO], (music)
    t = re.sub(r"\s+", " ", t).strip()
    bare = re.sub(r"[^a-z' ]+", " ", t.lower()).strip()
    bare = re.sub(r"\s+", " ", bare)
    if not bare or bare in GHOSTS:
        return ""
    for pat, rep in FIXUPS:
        t = re.sub(pat, rep, t, flags=re.I)
    return t


def speech_chunks(text: str, first: int = 140) -> list[str]:
    """Split a cleaned reply so the first sentence can play while the rest is being voiced."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    if len(sentences) < 2 or len(text) <= first:
        return [text] if text else []
    head, rest = "", []
    for i, sentence in enumerate(sentences):
        if not head or len(head) + len(sentence) < first:
            head = f"{head} {sentence}".strip()
        else:
            rest = sentences[i:]
            break
    return [head] + ([" ".join(rest)] if rest else [])

TTS_MODEL_KEY = "azure:tts"  # ledger row name; characters are stored in input_tokens
MAX_SPOKEN_CHARS = 700


def clean_for_speech(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Turn a chat reply into something that sounds natural out loud."""
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)                 # code blocks
    t = re.sub(r"https?://\S+", "the link", t)                   # URLs
    t = re.sub(r"\[(\d+)\]", "", t)                               # citation markers
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)                  # headings
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)                # bullets
    t = re.sub(r"[*_`~]+", "", t)                                 # emphasis
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", t)  # emoji
    t = t.replace("C.L.a.i.R", "Clair").replace("#", "number ")
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = (cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + ".") + " The rest is on screen."
    return t


def ssml(text: str, voice: str) -> str:
    lang = "-".join(voice.split("-")[:2])
    return (f"<speak version='1.0' xml:lang='{lang}'><voice name='{html.escape(voice, quote=True)}'>"
            f"{html.escape(text, quote=False)}</voice></speak>")


class VoiceError(RuntimeError):
    pass


class Voice:
    def __init__(self, settings, ledger, vocab_fn=None):
        self.s = settings
        self.ledger = ledger
        self.vocab_fn = vocab_fn  # () -> extra names Whisper should expect (lead businesses, etc.)
        self.last_ms = 0

    def hint(self) -> str:
        extra = []
        try:
            extra = [n for n in (self.vocab_fn() if self.vocab_fn else []) if n]
        except Exception:
            pass
        words = [w.strip() for w in self.s.voice_vocab.split(",") if w.strip()] + extra
        out = VOCAB_HINT
        for w in dict.fromkeys(words):  # keep order, drop repeats; Whisper reads ~224 tokens of hint
            if len(out) + len(w) + 2 > 600:
                break
            out = out.rstrip(".") + f", {w}."
        return out

    # ---- hearing -----------------------------------------------------------
    @property
    def can_hear(self) -> bool:
        return bool(self.s.whisper_url)

    def transcribe(self, audio: bytes, content_type: str = "audio/webm", quick: bool = False) -> str:
        """quick=True uses the small fast model (wake-phrase checks); falls back to the main one."""
        if self.s.dry_run:
            return ""
        ext = {"audio/mp4": "m4a", "audio/aac": "aac", "audio/ogg": "ogg", "audio/wav": "wav",
               "audio/mpeg": "mp3"}.get(content_type.split(";")[0].strip(), "webm")
        if not quick:
            self._keep(audio, ext)
        urls = ([self.s.whisper_fast_url] if quick and self.s.whisper_fast_url else []) + [self.s.whisper_url]
        import time

        t0 = time.monotonic()
        last = None
        for url in urls:
            try:
                r = httpx.post(
                    f"{url.rstrip('/')}/asr",
                    params={"task": "transcribe", "language": "en", "output": "json", "encode": "true",
                            "vad_filter": "true", "initial_prompt": self.hint()},
                    files={"audio_file": (f"speech.{ext}", audio, content_type)},
                    timeout=20 if quick else 60,
                )
                r.raise_for_status()
                break
            except httpx.HTTPError as e:
                last = e
        else:
            raise VoiceError(f"Whisper isn't answering ({type(last).__name__})") from last
        self.last_ms = int((time.monotonic() - t0) * 1000)
        raw = str(r.json().get("text", "")).strip()
        text = clean_transcript(raw)
        log.info("Whisper%s %d ms: %r%s", " (quick)" if quick else "", self.last_ms, text,
                 "" if text == raw else f" (raw {raw!r})")
        return text

    def _keep(self, audio: bytes, ext: str) -> None:
        """Keep the last few recordings in /data/voice so they can be played back when debugging."""
        try:
            d = self.s.data_dir / "voice"
            d.mkdir(parents=True, exist_ok=True)
            for old in sorted(d.glob("heard-*"))[: -(KEEP_RECORDINGS - 1) or None]:
                old.unlink(missing_ok=True)
            import time

            (d / f"heard-{time.time_ns():020d}.{ext}").write_bytes(audio)
        except OSError as e:
            log.info("Couldn't keep recording: %s", e)

    # ---- speaking ----------------------------------------------------------
    @property
    def can_speak(self) -> bool:
        return bool(self.s.azure_speech_key and self.s.azure_speech_region)

    def chars_this_month(self) -> int:
        return self.ledger.tokens_this_month(TTS_MODEL_KEY)

    def speak(self, text: str) -> bytes:
        """Return MP3 audio of `text` in Emily's voice. Raises VoiceError when it can't."""
        if not self.can_speak:
            raise VoiceError("Azure speech isn't set up (AZURE_SPEECH_KEY / AZURE_SPEECH_REGION)")
        spoken = clean_for_speech(text)
        if not spoken:
            raise VoiceError("Nothing to say")
        used = self.chars_this_month()
        if used + len(spoken) > self.s.tts_monthly_char_cap:
            raise VoiceError(f"Monthly voice allowance reached ({used:,} characters); replying in text only")
        if self.s.dry_run:
            return b""
        try:
            r = httpx.post(
                f"https://{self.s.azure_speech_region}.tts.speech.microsoft.com/cognitiveservices/v1",
                headers={
                    "Ocp-Apim-Subscription-Key": self.s.azure_speech_key,
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                    "User-Agent": "wetworks",
                },
                content=ssml(spoken, self.s.voice_name).encode("utf-8"),
                timeout=30,
            )
        except httpx.HTTPError as e:
            raise VoiceError(f"Azure speech unreachable ({type(e).__name__})") from e
        if r.status_code == 401:
            raise VoiceError("Azure rejected the speech key")
        if r.status_code != 200:
            raise VoiceError(f"Azure speech answered {r.status_code}")
        # Characters are what Azure bills (free tier: 500k/month), so log them, at $0.
        self.ledger.record_cost("voice:clair", TTS_MODEL_KEY, len(spoken), 0, 0.0)
        return r.content
