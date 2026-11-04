import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from wetworks.config import Settings
from wetworks.ledger import Ledger
from wetworks.voice import TTS_MODEL_KEY, Voice, VoiceError, clean_for_speech, ssml

from .test_console import call, make


def test_clean_for_speech():
    text = ("## Leads\n- **Clinic A** wants intake [1]\n- See https://x.example/a 🎯\n"
            "Mission #4 opened. C.L.a.i.R here.")
    out = clean_for_speech(text)
    assert out == "Leads Clinic A wants intake See the link Mission number 4 opened. Clair here."
    long = "Sentence one is here. " * 60
    cut = clean_for_speech(long, limit=100)
    assert cut.endswith("The rest is on screen.") and len(cut) < 140


def test_ssml_escapes_text():
    s = ssml("Fish & chips <now>", "en-IE-EmilyNeural")
    assert "xml:lang='en-IE'" in s and "name='en-IE-EmilyNeural'" in s
    assert "Fish &amp; chips &lt;now&gt;" in s


def voice(tmp_path, **kw):
    s = Settings(data_dir=tmp_path, azure_speech_key="k", azure_speech_region="eastus", **kw)
    return Voice(s, Ledger(tmp_path / "l.db"))


def test_speak_calls_azure_and_counts_characters(tmp_path, monkeypatch):
    v = voice(tmp_path)
    seen = {}

    def post(url, headers, content, timeout):
        seen.update(url=url, headers=headers, body=content.decode())
        return SimpleNamespace(status_code=200, content=b"MP3")

    monkeypatch.setattr("wetworks.voice.httpx.post", post)
    assert v.speak("Hiya, all quiet tonight.") == b"MP3"
    assert seen["url"] == "https://eastus.tts.speech.microsoft.com/cognitiveservices/v1"
    assert seen["headers"]["Ocp-Apim-Subscription-Key"] == "k"
    assert "en-IE-EmilyNeural" in seen["body"]
    assert v.chars_this_month() == len("Hiya, all quiet tonight.")
    assert v.ledger.month_to_date() == 0  # free tier: logged at $0


def test_speak_stops_before_free_tier_runs_out(tmp_path, monkeypatch):
    v = voice(tmp_path, tts_monthly_char_cap=30)
    v.ledger.record_cost("voice:clair", TTS_MODEL_KEY, 25, 0, 0.0)
    monkeypatch.setattr("wetworks.voice.httpx.post", lambda *a, **k: pytest.fail("Azure must not be called"))
    with pytest.raises(VoiceError, match="allowance"):
        v.speak("This is longer than five characters.")


def test_speak_errors(tmp_path, monkeypatch):
    with pytest.raises(VoiceError, match="isn't set up"):
        Voice(Settings(data_dir=tmp_path), Ledger(tmp_path / "l.db")).speak("hi")
    v = voice(tmp_path)
    monkeypatch.setattr("wetworks.voice.httpx.post", lambda *a, **k: SimpleNamespace(status_code=401, content=b""))
    with pytest.raises(VoiceError, match="rejected"):
        v.speak("hi")


def test_transcribe_sends_audio_to_whisper(tmp_path, monkeypatch):
    v = voice(tmp_path)
    seen = {}

    def post(url, params, files, timeout):
        seen.update(url=url, params=params, files=files)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"text": " Hey Clair, what's new? "})

    monkeypatch.setattr("wetworks.voice.httpx.post", post)
    assert v.transcribe(b"audio", "audio/mp4") == "Hey Clair, what's new?"
    assert seen["url"] == "http://whisper:9000/asr" and seen["params"]["output"] == "json"
    assert seen["files"]["audio_file"][0] == "speech.m4a"  # iPhone records mp4


def test_voice_api(tmp_path):
    app, architect, comms = make(tmp_path)
    from wetworks.api import create_app

    v = Voice(architect.s, architect.ledger)
    app = create_app(settings=architect.s, architect=architect, cells={}, comms=comms, events=architect.events, voice=v)
    cfg = call(app, "GET", "/api/voice", token="secret")["json"]
    assert cfg["hear"] is True and cfg["speak"] is False and cfg["wake_phrase"] == "hey clair"
    assert "that's all clair" in cfg["stop_phrases"] and cfg["voice"] == "en-IE-EmilyNeural"
    assert call(app, "POST", "/api/voice/say", token="secret", body={"text": "hi"})["status"] == 503
    r = call(app, "POST", "/api/comms/clair", token="secret", body={"text": "hello", "wait": True})
    assert r["status"] == 200 and r["json"]["reply"]["sender"] == "clair"
    assert call(app, "GET", "/api/voice")["status"] == 401


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_wake_and_stop_phrases():
    test = Path(__file__).parent / "voice_phrases.test.js"
    r = subprocess.run(["node", str(test)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
