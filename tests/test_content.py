import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from wetworks.cells.base import CellContext
from wetworks.cells.content import ContentCell
from wetworks.config import Settings
from wetworks.content import Content
from wetworks.events import EventLog
from wetworks.findings import Finding, FindingsStore
from wetworks.llm import Reply

CONFIG = Path(__file__).parent.parent / "config" / "content.toml"


def make(tmp_path):
    return Content(tmp_path / "content.db", CONFIG)


def test_ideas_drafts_flags_and_calendar(tmp_path):
    c = make(tmp_path)
    p = c.add_idea("Running 6 agents on one gaming laptop", "What fits in 8GB of VRAM and what doesn't")
    assert p["status"] == "idea" and c.add_idea("running 6 agents on one gaming laptop!") is None  # no duplicates
    p = c.set_draft(p["id"], "linkedin", "I built an ops center on my MSI...")
    assert p["status"] == "draft" and p["flags"] == ""
    p = c.set_draft(p["id"], "x", "Fixed a NovaPACS issue for a patient today")  # never_mention words
    assert "novapacs" in p["flags"] and "patient" in p["flags"]
    with pytest.raises(ValueError):
        c.update(p["id"], status="viral")
    when = (date.today() + timedelta(days=2)).isoformat()
    p = c.update(p["id"], planned=when, channel="linkedin")
    assert p["status"] == "scheduled"
    cal = c.calendar(14)
    assert any(x.get("piece") == p["id"] and x["date"] == when for x in cal)
    assert any("piece" not in x and x["channel"] == "x" for x in cal)  # open slots from the cadence
    c.update(p["id"], status="posted", posted_url="https://www.linkedin.com/posts/x")
    assert c.counts()["posted"] == 1


class LocalLLM:
    def __init__(self):
        self.prompts = []

    def ask_local(self, agent, prompt, **kw):
        self.prompts.append((agent, prompt))
        if agent == "content:ideas":
            return Reply(text=json.dumps({"ideas": [
                {"title": "What a week of free local models cost me: $0.43", "angle": "Gemma on the MSI vs paid APIs", "pillar": "costs", "source": "ledger"},
                {"title": "How I handle Novarad tickets faster", "angle": "work stuff", "pillar": "x", "source": "x"},  # filtered
            ]}), model="local:fake", cost_usd=0.0)
        return Reply(text=f"Draft for {agent}", model="local:fake", cost_usd=0.0)

    def ask(self, *a, **k):
        raise AssertionError("Drafting must stay on the local model")


def test_writer_suggests_and_drafts_locally(tmp_path):
    c = make(tmp_path)
    findings = FindingsStore(tmp_path / "f.db")
    findings.add(Finding(cell="intel", topic="AI models & tools", title="New small model tops agent benchmarks",
                         summary="A 4B model now beats 12B on tool use", url="https://example.com/a", importance=4))
    llm = LocalLLM()
    cell = ContentCell(CellContext(settings=Settings(dry_run=False, data_dir=tmp_path), llm=llm, findings=findings,
                                   events=EventLog(tmp_path / "e.db")), c)
    found = cell.run()
    titles = [p["title"] for p in c.pieces()]
    assert titles == ["What a week of free local models cost me: $0.43"]          # work idea dropped
    assert "New small model tops agent benchmarks" in llm.prompts[0][1]           # grounded in what he read
    p = c.pieces()[0]
    assert set(p["drafts"]) == {"linkedin", "x", "blog", "portfolio"} and p["status"] == "draft"
    assert found and "1 post draft ready" in found[0].title


def test_content_api_and_agent_tools(tmp_path):
    from wetworks.api import create_app
    from wetworks.main import build

    from .test_console import call

    s = Settings(dry_run=True, data_dir=tmp_path, console_token="secret", config_dir="config")
    architect, cells, comms = build(s)
    app = create_app(settings=s, architect=architect, cells=cells, comms=comms, events=architect.events, content=comms.content)
    r = call(app, "POST", "/api/content/ideas", token="secret", body={"title": "Pixel art ops center", "angle": "From iso to top-down"})
    pid = r["json"]["id"]
    assert call(app, "POST", "/api/content/ideas", token="secret", body={"title": "Pixel art ops center"})["status"] == 409
    r = call(app, "POST", f"/api/content/{pid}", token="secret", body={"drafts": {"x": "Rebuilt my ops center in pixel art."}})
    assert r["json"]["drafts"]["x"].startswith("Rebuilt") and r["json"]["status"] == "draft"
    ov = call(app, "GET", "/api/content", token="secret")["json"]
    assert ov["pieces"][0]["id"] == pid and "linkedin" in ov["channels"] and ov["calendar"]
    out = comms.clair_agent.__self__.toolbox.tools["add_content_idea"].fn(title="Voice assistant with an Irish accent")
    assert out.startswith("Added idea")
    assert any(p["title"].startswith("Voice") for p in comms.content.pieces())
