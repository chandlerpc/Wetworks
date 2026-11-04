import json
from datetime import date
from pathlib import Path

import pytest

from wetworks.cells.base import CellContext
from wetworks.cells.revenue import Revenue
from wetworks.config import Settings
from wetworks.events import EventLog
from wetworks.findings import FindingsStore
from wetworks.leads import LeadStore, domain_of
from wetworks.llm import Reply

from .test_console import call, make

CONFIG = Path(__file__).parent.parent / "config" / "revenue.toml"


class FakeLLM:
    def __init__(self, answers, results):
        self.answers, self.results, self.prompts, self.queries = list(answers), results, [], []

    def web_search_local(self, q, **kw):
        self.queries.append(q)
        return list(self.results)

    def ask_local(self, agent, prompt, **kw):
        self.prompts.append((agent, prompt))
        return Reply(text=self.answers.pop(0), model="local:fake", cost_usd=0.0)


RESULTS = [
    {"title": "Lake Mary Family Dental - hiring front desk", "url": "https://lakemarydental.example/careers", "content": "We're growing and need help answering calls."},
    {"title": "Receptionist job at Sunrise Spa - Indeed", "url": "https://www.indeed.com/viewjob?jk=1", "content": "Sunrise Spa in Sanford is hiring a receptionist to manage bookings."},
    {"title": "Best 10 gyms in Orlando", "url": "https://www.yelp.com/search?gyms", "content": "list"},
]
SCORE = json.dumps({"leads": [
    {"index": 0, "title": "Lake Mary Family Dental", "location": "Lake Mary", "signal": "Hiring front desk; calls piling up",
     "offer": "chatbot", "why": "A booking chatbot handles routine calls", "importance": 5},
    {"index": 1, "title": "Sunrise Spa", "location": "Sanford", "signal": "Hiring a receptionist for bookings",
     "offer": "Automation", "why": "Automate booking confirmations", "importance": 4},
    {"index": 9, "title": "Hallucinated Co", "signal": "x", "importance": 5},
    {"index": 2, "title": "Orlando Gym Co", "signal": "weak", "offer": "Dashboard", "importance": 2},
]})
DRAFT = json.dumps({"subject": "Front desk help for Lake Mary Family Dental", "body": "Hi there, ...\nPatrick Caldwell, CodeVoyage"})


def setup(tmp_path, answers, results=RESULTS):
    llm = FakeLLM(answers, results)
    s = Settings(dry_run=True, data_dir=tmp_path)
    ctx = CellContext(settings=s, llm=llm, findings=FindingsStore(tmp_path / "f.db"), events=EventLog(tmp_path / "e.db"))
    leads = LeadStore(tmp_path / "leads.db")
    return Revenue(ctx, leads, config_path=CONFIG), leads, llm, ctx


def test_hunt_scores_drafts_and_surfaces(tmp_path):
    rev, leads, llm, ctx = setup(tmp_path, [SCORE, DRAFT, DRAFT])
    found = rev.run()
    rows = leads.recent()
    assert [r["business"] for r in rows] == ["Lake Mary Family Dental", "Sunrise Spa", "Orlando Gym Co"]
    dental = rows[0]
    assert dental["offer"] == "Chatbot" and dental["fit"] == 5 and dental["domain"] == "lakemarydental.example"
    assert rows[1]["domain"] == ""  # job board page: not used to recognise the business
    assert dental["draft"].startswith("Subject: Front desk help") and rows[2]["draft"] == ""  # fit 2: no draft
    assert [f.title for f in found] == ["Lake Mary Family Dental: Chatbot", "Sunrise Spa: Automation"]
    assert found[0].importance == 4 and found[1].importance == 3
    assert len(llm.queries) == 4  # queries_per_run
    draft_prompt = llm.prompts[1][1]
    assert "do NOT\ninvent past clients" in draft_prompt and "Patrick Caldwell (CodeVoyage)" in draft_prompt


def test_rerun_skips_known_businesses(tmp_path):
    rev, leads, llm, _ = setup(tmp_path, [SCORE, DRAFT, DRAFT, SCORE])
    rev.run()
    assert rev.run() == [] and len(leads.recent()) == 3


def test_queries_rotate_daily(tmp_path):
    rev, *_ = setup(tmp_path, [])
    a, b = rev.todays_queries(date(2026, 11, 4)), rev.todays_queries(date(2026, 11, 5))
    assert len(a) == 4 and a != b and set(a) <= set(rev.config["queries"])


def test_no_results_no_model_call(tmp_path):
    rev, leads, llm, _ = setup(tmp_path, [], results=[])
    assert rev.run() == [] and llm.prompts == []


def test_lead_store_rules(tmp_path):
    s = LeadStore(tmp_path / "l.db")
    a = s.add("Acme Plumbing", "https://www.acmeplumbing.example/", "hiring", 9)
    assert a["fit"] == 5 and a["domain"] == "acmeplumbing.example"
    assert s.add("Acme Plumbing LLC", "https://acmeplumbing.example/about", "x", 3) is None  # same site
    assert s.add("acme plumbing", "", "x", 3) is None  # same name
    assert s.update(a["id"], status="contacted", notes="emailed Tue")["status"] == "contacted"
    with pytest.raises(ValueError):
        s.update(a["id"], status="maybe")
    s.update(a["id"], status="dismissed")
    assert s.recent() == [] and s.counts() == {"dismissed": 1}
    assert domain_of("https://WWW.Example.com/x") == "example.com"


def test_leads_api(tmp_path):
    app, architect, comms = make(tmp_path)
    from wetworks.api import create_app

    leads = LeadStore(tmp_path / "leads.db")
    lead = leads.add("Sunrise Spa", "https://sunrise.example", "hiring", 4, offer="Automation")
    app = create_app(settings=architect.s, architect=architect, cells={}, comms=comms, events=architect.events, leads=leads)
    r = call(app, "GET", "/api/leads", token="secret")["json"]
    assert r["leads"][0]["business"] == "Sunrise Spa" and r["counts"] == {"new": 1}
    assert call(app, "POST", f"/api/leads/{lead['id']}", token="secret", body={"status": "won"})["json"]["status"] == "won"
    assert call(app, "POST", f"/api/leads/{lead['id']}", token="secret", body={"status": "nope"})["status"] == 400
    assert call(app, "POST", "/api/leads/999", token="secret", body={"status": "won"})["status"] == 404
    assert call(app, "GET", "/api/leads")["status"] == 401
    architect.leads = leads
    assert "Lead pipeline: 0 new, 0 contacted, 0 replied, 1 won." in architect.context_block()
