import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from wetworks.cells.base import CellContext
from wetworks.cells.finance import FinanceCell
from wetworks.config import Settings
from wetworks.events import EventLog
from wetworks.finance import Finance, FinanceError, mask, merchant_key, parse_amount
from wetworks.findings import FindingsStore
from wetworks.llm import Reply

CONFIG = Path(__file__).parent.parent / "config" / "finance.toml"
TODAY = date.today()


def d(days_ago):
    return (TODAY - timedelta(days=days_ago)).strftime("%m/%d/%Y")


def checking_csv():
    """A checking account export: Date, Description, Amount (money out negative)."""
    rows = ["Date,Description,Amount,Balance"]
    for m in (0, 1, 2, 3):  # monthly Netflix and power bill, a few groceries, payroll
        rows.append(f"{d(5 + 30 * m)},NETFLIX.COM 866-579-7172 CA,-15.49,1000")
        rows.append(f"{d(9 + 30 * m)},DUKE ENERGY PAYMENT 1234567890123,-142.10,1000")
        rows.append(f"{d(12 + 30 * m)},PUBLIX #1234 SANFORD FL,-{80 + m * 7}.25,1000")
        rows.append(f"{d(14 + 30 * m)},NOVARAD PAYROLL DIR DEP,2400.00,1000")
    rows.append(f"{d(2)},SQ *MYSTERY TACO TRUCK,-18.00,1000")
    return "\n".join(rows)


def card_csv():
    """A card export: purchases positive, payments negative, separate header block."""
    return "\n".join(["Card ending 4421 statement", "",
                      "Transaction Date,Description,Category,Amount",
                      f"{d(3)},STARBUCKS STORE 12345,Food & Drink,6.45",
                      f"{d(4)},AMAZON.COM*2K4,Shopping,23.99",
                      f"{d(6)},PAYMENT THANK YOU,Payment,-500.00"])


def make(tmp_path):
    return Finance(tmp_path / "finance.db", CONFIG)


def test_helpers():
    assert mask("CARD 4111111111111111 PURCHASE") == "CARD ••1111 PURCHASE"
    assert mask("STORE #1234") == "STORE #1234"  # short numbers are kept
    assert merchant_key("SQ *MYSTERY TACO TRUCK") == "mystery taco truck"
    assert merchant_key("PUBLIX #1234 SANFORD FL") == merchant_key("PUBLIX #0987 SANFORD FL")
    assert parse_amount("($1,234.50)") == -1234.5 and parse_amount("-15.49") == -15.49 and parse_amount("") is None


def test_import_categorises_masks_and_dedupes(tmp_path):
    f = make(tmp_path)
    r = f.import_csv(checking_csv(), account="Chase checking", filename="chase.csv")
    assert r["added"] == 17 and r["skipped"] == 0 and not r["flipped"]
    tx = f.transactions()
    by = {t["description"]: t for t in tx}
    assert "1234567890123" not in json.dumps(tx) and "DUKE ENERGY PAYMENT ••0123" in by
    cats = {t["category"] for t in tx if "NETFLIX" in t["description"]}
    assert cats == {"Subscriptions"}
    assert by["NOVARAD PAYROLL DIR DEP"]["category"] == "Income"
    assert by["SQ *MYSTERY TACO TRUCK"]["category"] == "Other" and by["SQ *MYSTERY TACO TRUCK"]["cat_source"] == "none"
    again = f.import_csv(checking_csv(), account="Chase checking")
    assert again["added"] == 0 and again["skipped"] == 17   # re-importing is safe
    assert f.undo_batch(r["batch"]) == 17 and f.count() == 0


def test_card_export_signs_are_flipped(tmp_path):
    f = make(tmp_path)
    r = f.import_csv(card_csv(), account="Amex")
    assert r["flipped"] and r["added"] == 3
    by = {t["description"]: t for t in f.transactions()}
    assert by["STARBUCKS STORE 12345"]["amount"] == -6.45 and by["STARBUCKS STORE 12345"]["category"] == "Dining"
    assert by["PAYMENT THANK YOU"]["amount"] == 500.0 and by["PAYMENT THANK YOU"]["category"] == "Transfers"
    with pytest.raises(FinanceError):
        f.import_csv("just,some,text\n1,2,3")


def test_debit_credit_columns(tmp_path):
    f = make(tmp_path)
    f.import_csv("Posted Date,Payee,Debit,Credit\n" + f"{d(1)},PUBLIX 55,45.10,\n{d(2)},ZELLE FROM MOM,,100.00\n")
    amounts = sorted(t["amount"] for t in f.transactions())
    assert amounts == [-45.10, 100.0]


def test_recurring_upcoming_and_budget(tmp_path):
    f = make(tmp_path)
    f.import_csv(checking_csv(), account="Chase")
    rec = {r["merchant"]: r for r in f.recurring()}
    assert rec["netflix"]["cadence"] == "monthly" and rec["netflix"]["amount"] == 15.49
    assert "duke energy payment" in rec
    assert not any("publix" in m for m in rec)  # varying grocery amounts aren't a subscription... unless steady
    m = f.month((TODAY - timedelta(days=14)).strftime("%Y-%m"))
    assert m["income"] >= 2400
    assert any(c["category"] == "Subscriptions" and c["spent"] > 0 for c in f.month((TODAY - timedelta(days=35)).strftime("%Y-%m"))["by_category"])
    soon = f.upcoming(40)
    assert {r["merchant"] for r in soon} >= {"netflix"}


def test_set_category_remembers_merchant(tmp_path):
    f = make(tmp_path)
    f.import_csv(checking_csv())
    taco = next(t for t in f.transactions() if "TACO" in t["description"])
    f.set_category(taco["id"], "Dining")
    f.import_csv("Date,Description,Amount\n" + f"{d(0)},SQ *MYSTERY TACO TRUCK,-12.00\n")
    assert {t["category"] for t in f.transactions(q="TACO")} == {"Dining"}
    with pytest.raises(FinanceError):
        f.set_category(taco["id"], "Yachts")


class LocalOnlyLLM:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def ask_local(self, agent, prompt, **kw):
        self.calls.append(prompt)
        return Reply(text=self.answer, model="local:fake", cost_usd=0.0)

    def ask(self, *a, **k):
        raise AssertionError("Finance must never use a cloud model")


def cell(tmp_path, answer="{}"):
    s = Settings(dry_run=False, data_dir=tmp_path)
    llm = LocalOnlyLLM(answer)
    ctx = CellContext(settings=s, llm=llm, findings=FindingsStore(tmp_path / "f.db"), events=EventLog(tmp_path / "e.db"))
    f = make(tmp_path)
    return FinanceCell(ctx, f), f, llm


def test_cell_sorts_with_local_model_only(tmp_path):
    c, f, llm = cell(tmp_path, json.dumps({"mystery taco truck": "Dining"}))
    f.import_csv(checking_csv())
    assert c.sort_uncategorized() == 1
    assert "mystery taco truck" in llm.calls[0]
    assert {t["category"] for t in f.transactions(q="TACO")} == {"Dining"}


def test_brief_findings_respect_privacy_level(tmp_path):
    c, f, _ = cell(tmp_path)
    f.import_csv(checking_csv())
    rows = ["Date,Description,Amount"] + [f"{d(0)},CHIPOTLE 0{i},-30.00" for i in range(1, 12)]
    f.import_csv("\n".join(rows))  # $330 dining against a $300 budget
    found = c.report("totals")
    text = json.dumps([f.__dict__ for f in found])
    assert any("Dining is at" in x.title for x in found)
    assert "NETFLIX" not in text.upper() and "CHIPOTLE" not in text.upper()   # never merchants
    assert "$" in text
    c2, f2, _ = cell(tmp_path / "b")
    f2.import_csv("\n".join(rows))
    counts = c2.report("counts")
    assert counts and "$" not in json.dumps([x.__dict__ for x in counts])
    c3, f3, _ = cell(tmp_path / "c")
    f3.import_csv("\n".join(rows))
    assert c3.report("off") == []


def test_cloud_agents_get_nothing_by_default(tmp_path):
    from wetworks.main import build

    s = Settings(dry_run=True, data_dir=tmp_path, config_dir="config")
    architect, cells, comms = build(s)
    comms.finance.import_csv(checking_csv())
    clair_tool = comms.clair_agent.__self__.toolbox.tools["finance_summary"]
    assert "aren't shared" in clair_tool.fn()   # C.L.a.i.R thinks on Gemini
    arch_tool = comms.commander.toolbox.tools["finance_summary"]
    out = arch_tool.fn()                         # The Architect thinks locally (free model)
    assert out["spent_this_month"] >= 0 and "upcoming" in out
    drill_tool = comms.drills.tools["architect"].tools["finance_summary"]
    assert drill_tool.fn()["month"] == "sample"  # drills (Gemini) only ever see fake numbers


def test_finance_api(tmp_path):
    from wetworks.api import create_app
    from wetworks.main import build

    from .test_console import call

    s = Settings(dry_run=True, data_dir=tmp_path, console_token="secret", config_dir="config")
    architect, cells, comms = build(s)
    app = create_app(settings=s, architect=architect, cells=cells, comms=comms, events=architect.events, finance=comms.finance)
    assert call(app, "GET", "/api/finance")["status"] == 401
    r = call(app, "POST", "/api/finance/import?account=Chase", token="secret", body=None)
    assert r["status"] == 400
    import asyncio  # raw CSV body
    from .test_console import call as _call

    def post_csv(text):
        scope_body = text.encode()
        out = {"status": None, "body": b""}

        async def receive():
            return {"type": "http.request", "body": scope_body, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/api/finance/import", "raw_path": b"/api/finance/import", "query_string": b"account=Chase",
                 "headers": [(b"content-type", b"text/csv"), (b"authorization", b"Bearer secret")],
                 "client": ("t", 1), "server": ("t", 80), "root_path": ""}
        asyncio.run(app(scope, receive, send))
        return out["status"], json.loads(out["body"])

    status, j = post_csv(checking_csv())
    assert status == 200 and j["added"] == 17
    ov = _call(app, "GET", "/api/finance", token="secret")["json"]
    assert ov["count"] == 17 and "Groceries" in ov["categories"] and ov["recurring"]
    tx = _call(app, "GET", "/api/finance/transactions?category=Subscriptions", token="secret")["json"]["transactions"]
    assert tx and all(t["category"] == "Subscriptions" for t in tx)
    r = _call(app, "POST", f"/api/finance/transactions/{tx[0]['id']}", token="secret", body={"category": "Entertainment"})
    assert r["json"]["category"] == "Entertainment"
    assert _call(app, "DELETE", f"/api/finance/imports/{j['batch']}", token="secret")["json"]["removed"] == 17
