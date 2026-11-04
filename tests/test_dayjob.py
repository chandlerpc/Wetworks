import json

from wetworks.cells.base import CellContext
from wetworks.cells.dayjob import DayJobCell
from wetworks.config import Settings
from wetworks.dayjob import DayJob, read_upload, scrub
from wetworks.events import EventLog
from wetworks.findings import FindingsStore
from wetworks.llm import Reply

TICKET = """Site: Lake Mary Imaging. Patient: Jane Doe, DOB: 01/02/1960, MRN: 12345678
Study for SMITH^JOHN^A (acc# A99812) won't open in NovaPACS viewer. Server name: PACS01 at 10.1.2.30.
Call back 407-555-0199 or jane.tech@clinic.org. UID 1.2.840.113619.2.55.3.604688 after KB5034441. Mr. Brown reported it.
Patient Jane Roe also can't load priors."""


def test_scrub_removes_patient_details_but_keeps_tech_details():
    clean, found = scrub(TICKET)
    for gone in ("Jane Doe", "01/02/1960", "12345678", "SMITH^JOHN", "A99812", "407-555-0199", "jane.tech@", "1.2.840.113619",
                 "Mr. Brown", "Jane Roe"):
        assert gone not in clean, gone
    for kept in ("NovaPACS viewer", "PACS01", "10.1.2.30", "KB5034441", "won't open", "can't load priors"):
        assert kept in clean, kept
    assert found["dob"] == 1 and found["phone"] == 1 and found["patient name"] == 2


def test_library_tickets_and_solved_becomes_fix(tmp_path):
    dj = DayJob(tmp_path / "dj.db")
    d = dj.add_doc("pacs-notes.md", "Priors not loading\n\nCheck the prefetch rules in the worklist settings and restart the "
                   "query service.\n\nViewer crash on open\n\nClear the local cache folder. Patient: Bob Smith")
    assert d["chunks"] >= 1 and d["scrubbed"]
    assert "Bob Smith" not in json.dumps(dj.search("prefetch rules"))
    t, found = dj.new_ticket(TICKET, "NovaPACS", ref="CS-1182")
    assert "Jane Doe" not in t["text"] and found
    hits = dj.search(t["text"])
    assert hits and any("prefetch" in h["body"] or "priors" in h["body"].lower() for h in hits)
    solved = dj.solve(t["id"], "Re-ran prefetch for the date range; priors loaded.", title="Priors missing after migration")
    assert solved["status"] == "solved" and solved["fix_id"]
    assert dj.search("prior prefetch migration")[0]["kind"] in ("fix", "doc")
    assert dj.counts()["fixes"] == 1 and dj.counts()["solved"] == 1
    assert dj.delete_doc(d["id"]) and dj.counts()["docs"] == 0


def test_read_upload_html():
    assert "Restart the service" in read_upload("kb.html", b"<html><script>x()</script><p>Restart the service</p></html>")


class LocalLLM:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def ask_local(self, agent, prompt, **kw):
        self.prompts.append(prompt)
        return Reply(text=self.answer, model="local:fake", cost_usd=0.0)

    def ask(self, *a, **k):
        raise AssertionError("Day Job must never use a cloud model")


def test_technician_drafts_help_locally_and_brief_gets_counts_only(tmp_path):
    dj = DayJob(tmp_path / "dj.db")
    dj.add_fix("Viewer won't open studies", "Blank viewer", "Clear the viewer cache and re-login.", "NovaPACS")
    llm = LocalLLM(json.dumps({"summary": "Viewer can't open studies", "likely_cause": "Corrupt cache", "steps": ["Clear cache"],
                               "ask_customer": [], "reply": "Hi, please try clearing the cache.", "used": [1]}))
    ctx = CellContext(settings=Settings(dry_run=False, data_dir=tmp_path), llm=llm,
                      findings=FindingsStore(tmp_path / "f.db"), events=EventLog(tmp_path / "e.db"))
    cell = DayJobCell(ctx, dj)
    t, _ = dj.new_ticket(TICKET, "NovaPACS")
    help = cell.help(t["id"])
    assert help["sources"][0]["title"].endswith("Viewer won't open studies")
    assert "Jane Doe" not in llm.prompts[0] and "Clear the viewer cache" in llm.prompts[0]
    assert json.loads(dj.ticket(t["id"])["help"])["likely_cause"] == "Corrupt cache"
    found = cell.run()
    assert found and "1 support ticket still open" in found[0].title and "NovaPACS" not in found[0].summary


def test_cloud_agents_cannot_search_day_job(tmp_path):
    from wetworks.main import build

    architect, cells, comms = build(Settings(dry_run=True, data_dir=tmp_path, config_dir="config"))
    comms.dayjob.add_fix("Viewer cache", "Blank", "Clear cache", "NovaPACS")
    assert "aren't shared" in comms.clair_agent.__self__.toolbox.tools["dayjob_search"].fn(query="cache") or \
        "isn't shared" in comms.clair_agent.__self__.toolbox.tools["dayjob_search"].fn(query="cache")
    assert comms.commander.toolbox.tools["dayjob_search"].fn(query="viewer cache")[0]["kind"] == "fix"
    assert "Sample" in comms.drills.tools["architect"].tools["dayjob_search"].fn(query="x")[0]["title"]
