"""Finance: Patrick's money, kept on the MSI.

Bank and card CSV exports are imported into a local SQLite file. Nothing here talks to a bank, and
transaction details never go to a cloud model: categorising uses rules and the local model only.
What leaves this module for the brief, Discord or cloud agents is summary-level (see `summary_for`).
"""

import csv
import hashlib
import io
import re
import sqlite3
import statistics
import threading
import tomllib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CATEGORIES = ["Housing", "Utilities", "Groceries", "Dining", "Gas", "Transport", "Shopping", "Subscriptions",
              "AI & tools", "Entertainment", "Health", "Insurance", "Fees", "Income", "Transfers", "Other"]
NOT_SPENDING = {"Income", "Transfers"}

DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%d %b %Y", "%b %d, %Y", "%Y/%m/%d")
DATE_COLS = ("date", "transaction date", "trans date", "posted date", "posting date", "post date")
DESC_COLS = ("description", "merchant", "payee", "name", "details", "memo", "transaction description")
AMOUNT_COLS = ("amount", "transaction amount")
DEBIT_COLS = ("debit", "withdrawal", "withdrawals", "money out", "debit amount")
CREDIT_COLS = ("credit", "deposit", "deposits", "money in", "credit amount")
CAT_COLS = ("category",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask(text: str) -> str:
    """Hide card/account numbers: any run of 6+ digits keeps only its last 4."""
    return re.sub(r"\d{6,}", lambda m: "••" + m.group(0)[-4:], text or "")


def merchant_key(desc: str) -> str:
    """A stable name for grouping: drop card-processor noise, numbers and locations."""
    t = (desc or "").lower()
    t = re.sub(r"^(pos|debit card|card|checkcard|purchase|recurring|ach|online|web|tst\*|sq \*|sq\*|pp\*|paypal \*)\s*", "", t)
    t = re.sub(r"(debit card purchase|purchase authorized on \d+/\d+|recurring payment)", " ", t)
    t = re.sub(r"[#*•]+\s*\w*", " ", t)
    t = re.sub(r"\d+", " ", t)
    t = re.sub(r"[^a-z& ]+", " ", t)
    words = [w for w in t.split() if len(w) > 1 and w not in {"fl", "ca", "ny", "tx", "usa", "us", "www", "com", "inc", "llc"}]
    return " ".join(words[:3]) or (desc or "").strip().lower()[:24]


def parse_date(s: str) -> date | None:
    s = (s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def parse_amount(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    v = float(s)
    return -v if neg else v


class FinanceError(ValueError):
    pass


class Finance:
    def __init__(self, path: Path, config_path: Path | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.config_path = config_path
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tx (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, description TEXT NOT NULL,
                merchant TEXT NOT NULL, amount REAL NOT NULL, account TEXT NOT NULL, category TEXT NOT NULL,
                cat_source TEXT NOT NULL, batch INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE);
            CREATE INDEX IF NOT EXISTS tx_day ON tx(day);
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, account TEXT NOT NULL, filename TEXT NOT NULL,
                added INTEGER NOT NULL, skipped INTEGER NOT NULL, flipped INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS learned (merchant TEXT PRIMARY KEY, category TEXT NOT NULL, source TEXT NOT NULL);
        """)
        self.db.commit()

    # ---- settings -----------------------------------------------------------------------
    def config(self) -> dict:
        if not self.config_path or not self.config_path.exists():
            return {}
        with open(self.config_path, "rb") as f:
            return tomllib.load(f).get("finance", {})

    def rules(self) -> list[tuple[str, str]]:
        out = []
        for cat, words in (self.config().get("rules") or {}).items():
            for w in words:
                out.append((str(w).lower(), cat))
        return sorted(out, key=lambda r: -len(r[0]))  # most specific first

    def budgets(self) -> dict[str, float]:
        return {k: float(v) for k, v in (self.config().get("budgets") or {}).items()}

    # ---- import ---------------------------------------------------------------------------
    def import_csv(self, text: str, account: str = "", filename: str = "", flip: str = "auto") -> dict:
        """Import a bank/card CSV. Money out is stored negative. flip: auto | yes | no."""
        try:
            dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        # find the header row (some banks put a title block first)
        hi = next((i for i, r in enumerate(rows[:15]) if any(c.strip().lower() in DATE_COLS for c in r)), None)
        if hi is None:
            raise FinanceError("Couldn't find a date column. Export the transactions as CSV from your bank's website.")
        head = [c.strip().lower() for c in rows[hi]]
        col = lambda names: next((head.index(n) for n in names if n in head), None)
        di, de, am, db, cr, ca = col(DATE_COLS), col(DESC_COLS), col(AMOUNT_COLS), col(DEBIT_COLS), col(CREDIT_COLS), col(CAT_COLS)
        if de is None or (am is None and db is None and cr is None):
            raise FinanceError("Couldn't find description and amount columns in that file.")
        parsed = []
        for r in rows[hi + 1:]:
            if len(r) <= max(i for i in (di, de, am, db, cr) if i is not None):
                continue
            d = parse_date(r[di])
            if not d:
                continue
            if am is not None:
                v = parse_amount(r[am])
            else:
                out_v = parse_amount(r[db]) if db is not None else None
                in_v = parse_amount(r[cr]) if cr is not None else None
                v = (abs(in_v) if in_v else 0.0) - (abs(out_v) if out_v else 0.0) if (out_v or in_v) else None
            if v is None:
                continue
            parsed.append((d, mask(r[de].strip())[:200], v, (r[ca].strip() if ca is not None and ca < len(r) else "")))
        if not parsed:
            raise FinanceError("No transactions found in that file.")
        flipped = False
        if am is not None and (flip == "yes" or (flip == "auto" and self._looks_like_card(parsed))):
            parsed = [(d, desc, -v, c) for d, desc, v, c in parsed]
            flipped = True
        account = (account or "").strip()[:40] or "Account"
        with self.lock:
            cur = self.db.execute("INSERT INTO batches (ts, account, filename, added, skipped, flipped) VALUES (?, ?, ?, 0, 0, ?)",
                                  (_now(), account, filename[:120], int(flipped)))
            batch = cur.lastrowid
            added = skipped = 0
            seen = defaultdict(int)
            for d, desc, v, bank_cat in parsed:
                key = f"{account}|{d}|{desc}|{v:.2f}"
                seen[key] += 1  # two identical coffees on one day are both real
                h = hashlib.sha256(f"{key}|{seen[key]}".encode()).hexdigest()
                m = merchant_key(desc)
                cat, src = self._categorize(m, desc, v, bank_cat)
                try:
                    self.db.execute("INSERT INTO tx (day, description, merchant, amount, account, category, cat_source, batch, hash) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (d.isoformat(), desc, m, round(v, 2), account, cat, src, batch, h))
                    added += 1
                except sqlite3.IntegrityError:
                    skipped += 1
            self.db.execute("UPDATE batches SET added = ?, skipped = ? WHERE id = ?", (added, skipped, batch))
            self.db.commit()
        return {"batch": batch, "added": added, "skipped": skipped, "flipped": flipped, "account": account}

    @staticmethod
    def _looks_like_card(parsed) -> bool:
        """Card exports often list purchases as positive numbers and payments as negative."""
        pos = sum(1 for _, _, v, _ in parsed if v > 0)
        return pos / len(parsed) >= 0.6 and any(re.search(r"payment|thank you|autopay", d, re.I) for _, d, v, _ in parsed if v < 0)

    def undo_batch(self, batch: int) -> int:
        with self.lock:
            n = self.db.execute("DELETE FROM tx WHERE batch = ?", (batch,)).rowcount
            self.db.execute("DELETE FROM batches WHERE id = ?", (batch,))
            self.db.commit()
        return n

    def batches(self, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id, ts, account, filename, added, skipped, flipped FROM batches ORDER BY id DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [dict(zip(("id", "ts", "account", "filename", "added", "skipped", "flipped"), r)) for r in rows]

    # ---- categories -------------------------------------------------------------------------
    def _categorize(self, merchant: str, desc: str, amount: float, bank_cat: str = "") -> tuple[str, str]:
        r = self.db.execute("SELECT category FROM learned WHERE merchant = ?", (merchant,)).fetchone()
        if r:
            return r[0], "learned"
        low = desc.lower()
        for word, cat in self.rules():
            if word in low:
                return cat, "rule"
        if amount > 0:
            return "Income", "rule"
        mapped = next((c for c in CATEGORIES if bank_cat and c.lower().split(" ")[0] in bank_cat.lower()), None)
        if mapped:
            return mapped, "bank"
        return "Other", "none"

    def set_category(self, tid: int, category: str, remember: bool = True) -> dict | None:
        if category not in CATEGORIES:
            raise FinanceError(f"Unknown category {category!r}")
        with self.lock:
            r = self.db.execute("SELECT merchant FROM tx WHERE id = ?", (tid,)).fetchone()
            if not r:
                return None
            if remember:  # apply to every past and future charge from this merchant
                self.db.execute("INSERT OR REPLACE INTO learned (merchant, category, source) VALUES (?, ?, 'patrick')", (r[0], category))
                self.db.execute("UPDATE tx SET category = ?, cat_source = 'patrick' WHERE merchant = ?", (category, r[0]))
            else:
                self.db.execute("UPDATE tx SET category = ?, cat_source = 'patrick' WHERE id = ?", (category, tid))
            self.db.commit()
        return self.get(tid)

    def uncategorized_merchants(self, limit: int = 40) -> list[str]:
        with self.lock:
            rows = self.db.execute("SELECT merchant, COUNT(*) n FROM tx WHERE cat_source = 'none' GROUP BY merchant "
                                   "ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    def learn(self, merchant: str, category: str, source: str = "local-model") -> None:
        if category not in CATEGORIES:
            return
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO learned (merchant, category, source) VALUES (?, ?, ?)", (merchant, category, source))
            self.db.execute("UPDATE tx SET category = ?, cat_source = ? WHERE merchant = ? AND cat_source = 'none'",
                            (category, source, merchant))
            self.db.commit()

    # ---- reading ----------------------------------------------------------------------------
    KEYS = ("id", "day", "description", "merchant", "amount", "account", "category", "cat_source", "batch")

    def get(self, tid: int) -> dict | None:
        r = self.db.execute(f"SELECT {', '.join(self.KEYS)} FROM tx WHERE id = ?", (tid,)).fetchone()
        return dict(zip(self.KEYS, r)) if r else None

    def transactions(self, category: str = "", q: str = "", month: str = "", limit: int = 200) -> list[dict]:
        sql, args = f"SELECT {', '.join(self.KEYS)} FROM tx WHERE 1=1", []
        if category:
            sql += " AND category = ?"; args.append(category)
        if q:
            sql += " AND description LIKE ?"; args.append(f"%{q}%")
        if month:
            sql += " AND substr(day, 1, 7) = ?"; args.append(month)
        sql += " ORDER BY day DESC, id DESC LIMIT ?"; args.append(limit)
        with self.lock:
            return [dict(zip(self.KEYS, r)) for r in self.db.execute(sql, args).fetchall()]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM tx").fetchone()[0]

    def month(self, ym: str | None = None) -> dict:
        ym = ym or date.today().strftime("%Y-%m")
        with self.lock:
            rows = self.db.execute("SELECT category, SUM(amount) FROM tx WHERE substr(day, 1, 7) = ? GROUP BY category", (ym,)).fetchall()
        by = {c: round(v, 2) for c, v in rows}
        spend = {c: round(-v, 2) for c, v in by.items() if c not in NOT_SPENDING and v < 0}
        budgets = self.budgets()
        cats = sorted(set(spend) | set(budgets), key=lambda c: -spend.get(c, 0))
        return {"month": ym, "income": round(by.get("Income", 0), 2), "spent": round(sum(spend.values()), 2),
                "by_category": [{"category": c, "spent": spend.get(c, 0.0), "budget": budgets.get(c)} for c in cats]}

    # ---- recurring charges ---------------------------------------------------------------------
    def recurring(self, today: date | None = None) -> list[dict]:
        """Charges that repeat on a steady cadence: subscriptions and bills."""
        today = today or date.today()
        since = (today - timedelta(days=400)).isoformat()
        with self.lock:
            rows = self.db.execute("SELECT merchant, day, amount, category, description FROM tx WHERE amount < 0 AND day >= ? "
                                   "AND category NOT IN ('Transfers') ORDER BY day", (since,)).fetchall()
        groups = defaultdict(list)
        for m, d, a, c, desc in rows:
            groups[m].append((date.fromisoformat(d), -a, c, desc))
        out = []
        for m, g in groups.items():
            if len(g) < 2:
                continue
            days = sorted({d for d, *_ in g})
            if len(days) < 2:
                continue
            gaps = [(b - a).days for a, b in zip(days, days[1:])]
            gap = statistics.median(gaps)
            cadence = next((name for name, lo, hi in (("weekly", 6, 8), ("every 2 weeks", 13, 15), ("monthly", 27, 33),
                                                       ("quarterly", 85, 95), ("yearly", 355, 375)) if lo <= gap <= hi), None)
            if not cadence or any(abs(x - gap) > max(4, gap * 0.25) for x in gaps):
                continue
            amounts = [a for _, a, *_ in g]
            typical = statistics.median(amounts)
            spread = (max(amounts) - min(amounts)) / typical if typical > 0 else 1
            everyday = g[-1][2] in ("Groceries", "Dining", "Gas", "Shopping", "Transport")
            if typical <= 0 or spread > (0.05 if everyday else 0.35):
                continue  # varying amounts at a shop you visit often aren't a bill
            if len(g) < 3 and cadence in ("weekly", "every 2 weeks"):
                continue
            last = days[-1]
            nxt = last + timedelta(days=round(gap))
            per_month = typical * {"weekly": 52 / 12, "every 2 weeks": 26 / 12, "monthly": 1, "quarterly": 1 / 3, "yearly": 1 / 12}[cadence]
            stale = (today - last).days > gap * 1.8  # cancelled or moved
            out.append({"merchant": m, "label": g[-1][3][:60], "category": g[-1][2], "cadence": cadence, "amount": round(typical, 2),
                        "per_month": round(per_month, 2), "last": last.isoformat(), "next": nxt.isoformat(),
                        "first_seen": days[0].isoformat(), "times": len(g), "active": not stale})
        return sorted(out, key=lambda r: (not r["active"], r["next"]))

    def upcoming(self, days: int = 30, today: date | None = None) -> list[dict]:
        today = today or date.today()
        end = today + timedelta(days=days)
        return [r for r in self.recurring(today) if r["active"] and today <= date.fromisoformat(r["next"]) <= end]

    # ---- what may leave the MSI ------------------------------------------------------------------
    def summary_for(self, audience: str, today: date | None = None) -> dict:
        """audience: 'local' (full detail), 'totals' (no merchants), 'counts' (no amounts)."""
        m = self.month()
        rec = [r for r in self.recurring(today) if r["active"]]
        up = self.upcoming(7, today)
        over = [c for c in m["by_category"] if c["budget"] and c["spent"] >= 0.9 * c["budget"]]
        if audience == "counts":
            return {"transactions": self.count(), "recurring_charges": len(rec), "due_this_week": len(up),
                    "categories_near_budget": [c["category"] for c in over]}
        out = {"month": m["month"], "spent_this_month": m["spent"], "income_this_month": m["income"],
               "by_category": m["by_category"][:8], "recurring_monthly_total": round(sum(r["per_month"] for r in rec), 2),
               "recurring_charges": len(rec), "due_this_week": {"count": len(up), "total": round(sum(r["amount"] for r in up), 2)},
               "categories_near_budget": [c["category"] for c in over]}
        if audience == "local":
            out["upcoming"] = up
            out["recurring"] = rec[:25]
        return out
