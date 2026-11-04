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


def _past(lead_store, lid):
    """Make a lead's follow-up due now."""
    lead_store.db.execute("UPDATE leads SET followup_due = '2000-01-01T00:00:00+00:00' WHERE id = ?", (lid,))
    lead_store.db.commit()


def test_research_runs_before_drafting(tmp_path):
    rev, leads, llm, ctx = setup(tmp_path, [SCORE, DRAFT, DRAFT])
    opened = []
    rev.open_mission = lambda title, request, source: opened.append((title, request, source)) or {"id": 40 + len(opened)}
    found = rev.run()
    assert [o[2] for o in opened] == ["lead:1", "lead:2"]  # the two strong leads, research_per_run = 2
    assert "Do not contact them" in opened[0][1]
    rows = {r["id"]: r for r in leads.recent()}
    assert rows[1]["research_mission"] == 41 and rows[1]["draft"] == ""  # draft waits for research
    assert len(found) == 2

    # the Splinter finishes: research lands on the lead and the first email is drafted from it
    done = {"id": 41, "source": "lead:1", "status": "done", "result": "Two dentists; uses Dentrix.",
            "sources": [{"n": 1, "url": "https://lakemarydental.example/about"}]}
    assert rev.mission_finished(done) is True
    lead = leads.get(1)
    assert lead["research"].startswith("Two dentists") and "lakemarydental.example/about" in lead["research"]
    assert lead["draft"].startswith("Subject: Front desk help")
    assert "uses Dentrix" in llm.prompts[-1][1]
    assert rev.mission_finished({"id": 1, "source": "architect", "status": "done"}) is False

    # a failed research mission still unblocks drafting, without pretending there was research
    rev.mission_finished({"id": 42, "source": "lead:2", "status": "failed", "result": ""})
    lead2 = leads.get(2)
    assert lead2["research"].startswith("(research mission #42 failed") and lead2["draft"]
    assert "Research done before outreach" not in llm.prompts[-1][1]


def test_followups_remind_and_draft_never_send(tmp_path):
    FU = json.dumps({"subject": "Re: front desk", "body": "One more idea...\nPatrick"})
    rev, leads, llm, ctx = setup(tmp_path, [FU, FU])
    lead = leads.add("Lake Mary Family Dental", "https://lakemarydental.example", "hiring", 5)
    leads.update(lead["id"], draft="Subject: Hi\n\nFirst email")
    lead = leads.update(lead["id"], status="contacted", followup_days=rev.next_followup_days(0))
    assert lead["contacted_at"] and lead["followup_due"] > lead["contacted_at"]  # due in 4 days
    assert rev.check_followups() == [] and leads.due_count() == 0

    _past(leads, lead["id"])
    found = rev.check_followups()
    assert [f.title for f in found] == ["Follow up #1 with Lake Mary Family Dental"]
    assert leads.get(lead["id"])["followup_draft"].startswith("Subject: Re: front desk")
    assert "First email" in llm.prompts[0][1] and leads.due_count() == 1
    assert rev.check_followups() == []  # not repeated in the next brief

    lead = leads.followed_up(lead["id"], rev.next_followup_days(1))
    assert lead["followups"] == 1 and lead["followup_draft"] == "" and lead["followup_due"]
    _past(leads, lead["id"])
    assert rev.check_followups()[0].title == "Follow up #2 with Lake Mary Family Dental"
    assert "last nudge" in llm.prompts[1][1]
    lead = leads.followed_up(lead["id"], rev.next_followup_days(2))
    assert lead["followup_due"] == ""  # two follow-ups, then it stops

    # a reply stops the nudges
    other = leads.add("Sunrise Spa", "https://sunrise.example", "x", 4)
    leads.update(other["id"], status="contacted", followup_days=4)
    assert leads.update(other["id"], status="replied")["followup_due"] == ""


def test_lead_actions_api(tmp_path):
    from wetworks.api import create_app
    from wetworks.main import build

    s = Settings(dry_run=True, data_dir=tmp_path, console_token="secret", config_dir="config")
    architect, cells, comms = build(s)
    comms.operator.submit = lambda: None  # keep the research mission queued
    app = create_app(settings=s, architect=architect, cells=cells, comms=comms, events=architect.events,
                     leads=comms.leads, missions=comms.missions)
    leads = comms.leads
    lead = leads.add("Sunrise Spa", "https://sunrise.example", "hiring", 4)
    r = call(app, "POST", f"/api/leads/{lead['id']}/followed-up", token="secret", body={})
    assert r["status"] == 409  # not contacted yet
    r = call(app, "POST", f"/api/leads/{lead['id']}", token="secret", body={"status": "contacted"})
    assert r["json"]["followup_due"]
    r = call(app, "POST", f"/api/leads/{lead['id']}/snooze", token="secret", body={"days": 10})
    assert r["status"] == 200 and r["json"]["followup_due"] > lead["created"]
    _past(leads, lead["id"])
    r = call(app, "GET", "/api/leads?status=due", token="secret")["json"]
    assert [l["id"] for l in r["leads"]] == [lead["id"]] and r["due"] == 1
    r = call(app, "POST", f"/api/leads/{lead['id']}/followed-up", token="secret", body={})
    assert r["json"]["followups"] == 1
    r = call(app, "POST", f"/api/leads/{lead['id']}/research", token="secret", body={})
    assert r["status"] == 200 and r["json"]["research_mission"]
    m = comms.missions.get(r["json"]["research_mission"])
    assert m["source"] == f"lead:{lead['id']}" and m["status"] == "queued"
    assert call(app, "POST", f"/api/leads/{lead['id']}/research", token="secret", body={})["status"] == 409
    assert call(app, "POST", f"/api/leads/{lead['id']}/explode", token="secret", body={})["status"] == 404


def test_old_lead_database_upgrades(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "leads.db")
    db.execute("""CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, updated TEXT NOT NULL,
        business TEXT NOT NULL, url TEXT NOT NULL, domain TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
        signal TEXT NOT NULL, offer TEXT NOT NULL DEFAULT '', why TEXT NOT NULL DEFAULT '', fit INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'new', draft TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '')""")
    db.execute("INSERT INTO leads (created, updated, business, url, domain, signal, fit) VALUES ('t','t','Old Co','','','x',3)")
    db.commit(); db.close()
    leads = LeadStore(tmp_path / "leads.db")
    old = leads.get(1)
    assert old["business"] == "Old Co" and old["followups"] == 0 and old["research"] == ""
