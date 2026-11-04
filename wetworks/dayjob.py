"""Day Job: a support helper for Patrick's Novarad work, kept on the MSI.

Patrick gives it notes, KB articles and his own fixes; pasted tickets are scrubbed of patient details
before anything is stored or shown to a model, and only the local model reads them. It never connects
to Novarad systems.
"""

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

PRODUCTS = ["NovaPACS", "NovaRIS", "NovaRad other", "Network / server", "Workstation", "Other"]
STATUSES = ("open", "waiting", "solved")

# ---- patient-detail scrubbing (HIPAA): applied before storage, search or any model call ----------------
FIELD = (r"(?:patient(?:'s)?\s*name|pt\.?\s*name|patient|pt|dob|d\.o\.b\.?|date\s+of\s+birth|birth\s*date|mrn|"
         r"medical\s+record(?:\s+(?:no|number|#))?|accession(?:\s+(?:no|number|#))?|acc\s*#?|ssn|patient\s+id|pid|"
         r"home\s+address|insurance\s+id|member\s+id)")
SCRUBS = [
    ("patient name", re.compile(r"\b[A-Z][A-Za-z'\-]+\^[A-Z][A-Za-z'\-]+(?:\^[A-Za-z.'\-]*)*")),   # DICOM LAST^FIRST
    ("field", re.compile(rf"(?i)\b({FIELD})\s*[:#=]+\s*([^\n,;|()]{{1,40}}?)(?=[\n,;|()]|\.\s|\s{{2}}|$)")),
    ("named", re.compile(r"\b([Pp]atient|[Pp]t\.?)\s+([A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+){1,2})")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")),
    ("UID", re.compile(r"\b\d+(?:\.\d+){5,}\b")),                                                # DICOM UIDs
    ("name", re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?")),
    ("ID number", re.compile(r"(?<![\w.])\d{7,}(?![\w.])")),                                    # MRNs, accessions
]


def scrub(text: str) -> tuple[str, dict]:
    """Remove patient details. Returns (clean text, {what: count})."""
    found: dict[str, int] = {}
    out = text or ""
    for kind, rx in SCRUBS:
        def repl(m, kind=kind):
            if kind == "named":
                found["patient name"] = found.get("patient name", 0) + 1
                return f"{m.group(1)} [PATIENT NAME REMOVED]"
            if kind == "field":
                label = re.sub(r"\s+", " ", m.group(1).strip()).upper()
                found[label.lower()] = found.get(label.lower(), 0) + 1
                return f"{m.group(1)}: [{label} REMOVED]"
            found[kind] = found.get(kind, 0) + 1
            return f"[{kind.upper()} REMOVED]"
        out = rx.sub(repl, out)
    return out, found


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fts_query(text: str, limit: int = 12) -> str:
    """Turn free text into a forgiving FTS5 OR query of its meaningful words."""
    stop = {"that", "this", "with", "from", "have", "they", "when", "what", "there", "their", "been", "were", "will",
            "would", "could", "should", "about", "after", "before", "removed", "please", "thanks", "hello", "able", "into"}
    words = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", text or ""):
        lw = w.lower()
        if lw not in stop and lw not in words:
            words.append(lw)
    return " OR ".join(f'"{w}"' for w in words[:limit])


class DayJob:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, name TEXT NOT NULL,
                chunks INTEGER NOT NULL, scrubbed INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS fixes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, product TEXT NOT NULL,
                title TEXT NOT NULL, symptoms TEXT NOT NULL, steps TEXT NOT NULL, source TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, updated TEXT NOT NULL,
                product TEXT NOT NULL, ref TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, help TEXT NOT NULL,
                resolution TEXT NOT NULL, fix_id INTEGER NOT NULL DEFAULT 0);
            CREATE VIRTUAL TABLE IF NOT EXISTS library USING fts5(kind UNINDEXED, ref UNINDEXED, title, body);
        """)
        self.db.commit()

    # ---- library ---------------------------------------------------------------------------
    def add_doc(self, name: str, text: str) -> dict:
        clean, found = scrub(text)
        chunks = self._chunks(clean)
        if not chunks:
            raise ValueError("That file had no readable text.")
        with self.lock:
            cur = self.db.execute("INSERT INTO docs (ts, name, chunks, scrubbed) VALUES (?, ?, ?, ?)",
                                  (_now(), name[:120], len(chunks), sum(found.values())))
            did = cur.lastrowid
            self.db.executemany("INSERT INTO library (kind, ref, title, body) VALUES ('doc', ?, ?, ?)",
                                [(did, name[:120], c) for c in chunks])
            self.db.commit()
        return {"id": did, "name": name, "chunks": len(chunks), "scrubbed": found}

    @staticmethod
    def _chunks(text: str, size: int = 900) -> list[str]:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
        out, cur = [], ""
        for p in paras:
            if len(cur) + len(p) > size and cur:
                out.append(cur); cur = ""
            while len(p) > size:
                out.append(p[:size]); p = p[size:]
            cur = f"{cur}\n\n{p}".strip()
        if cur:
            out.append(cur)
        return out

    def delete_doc(self, did: int) -> bool:
        with self.lock:
            n = self.db.execute("DELETE FROM docs WHERE id = ?", (did,)).rowcount
            self.db.execute("DELETE FROM library WHERE kind = 'doc' AND ref = ?", (did,))
            self.db.commit()
        return bool(n)

    def docs(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id, ts, name, chunks, scrubbed FROM docs ORDER BY id DESC").fetchall()
        return [dict(zip(("id", "ts", "name", "chunks", "scrubbed"), r)) for r in rows]

    def add_fix(self, title: str, symptoms: str, steps: str, product: str = "Other", source: str = "patrick") -> dict:
        title, _ = scrub(title.strip()[:160]); symptoms, _ = scrub(symptoms.strip()[:3000]); steps, _ = scrub(steps.strip()[:6000])
        if not title or not steps:
            raise ValueError("A fix needs a title and the steps that worked.")
        product = product if product in PRODUCTS else "Other"
        with self.lock:
            cur = self.db.execute("INSERT INTO fixes (ts, product, title, symptoms, steps, source) VALUES (?, ?, ?, ?, ?, ?)",
                                  (_now(), product, title, symptoms, steps, source))
            fid = cur.lastrowid
            self.db.execute("INSERT INTO library (kind, ref, title, body) VALUES ('fix', ?, ?, ?)",
                            (fid, f"[{product}] {title}", f"Symptoms: {symptoms}\n\nFix: {steps}"))
            self.db.commit()
        return self.fix(fid)

    def fix(self, fid: int) -> dict | None:
        r = self.db.execute("SELECT id, ts, product, title, symptoms, steps, source FROM fixes WHERE id = ?", (fid,)).fetchone()
        return dict(zip(("id", "ts", "product", "title", "symptoms", "steps", "source"), r)) if r else None

    def fixes(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id FROM fixes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self.fix(r[0]) for r in rows]

    def search(self, text: str, limit: int = 6) -> list[dict]:
        clean, _ = scrub(text)
        q = fts_query(clean)
        if not q:
            return []
        with self.lock:
            try:
                rows = self.db.execute("SELECT kind, ref, title, snippet(library, 3, '', '', ' … ', 40), body, bm25(library) "
                                       "FROM library WHERE library MATCH ? ORDER BY bm25(library) LIMIT ?", (q, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [{"kind": k, "ref": int(ref), "title": t, "snippet": sn, "body": b} for k, ref, t, sn, b, _ in rows]

    # ---- tickets ---------------------------------------------------------------------------------
    TKEYS = ("id", "ts", "updated", "product", "ref", "text", "status", "help", "resolution", "fix_id")

    def new_ticket(self, text: str, product: str = "Other", ref: str = "") -> tuple[dict, dict]:
        clean, found = scrub(text.strip()[:12000])
        if not clean.strip():
            raise ValueError("Paste the ticket text first.")
        ref, _ = scrub(ref.strip()[:40])
        with self.lock:
            cur = self.db.execute("INSERT INTO tickets (ts, updated, product, ref, text, status, help, resolution) "
                                  "VALUES (?, ?, ?, ?, ?, 'open', '', '')",
                                  (_now(), _now(), product if product in PRODUCTS else "Other", ref, clean))
            self.db.commit()
        return self.ticket(cur.lastrowid), found

    def ticket(self, tid: int) -> dict | None:
        r = self.db.execute(f"SELECT {', '.join(self.TKEYS)} FROM tickets WHERE id = ?", (tid,)).fetchone()
        return dict(zip(self.TKEYS, r)) if r else None

    def tickets(self, status: str = "", limit: int = 50) -> list[dict]:
        sql = f"SELECT {', '.join(self.TKEYS)} FROM tickets" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC LIMIT ?"
        with self.lock:
            rows = self.db.execute(sql, ((status, limit) if status else (limit,))).fetchall()
        return [dict(zip(self.TKEYS, r)) for r in rows]

    def update_ticket(self, tid: int, status: str | None = None, help: str | None = None, resolution: str | None = None) -> dict | None:
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        sets, vals = [], []
        for col, v in (("status", status), ("help", help), ("resolution", scrub(resolution)[0] if resolution else resolution)):
            if v is not None:
                sets.append(f"{col} = ?"); vals.append(v)
        if sets:
            with self.lock:
                self.db.execute(f"UPDATE tickets SET {', '.join(sets)}, updated = ? WHERE id = ?", (*vals, _now(), tid))
                self.db.commit()
        return self.ticket(tid)

    def solve(self, tid: int, resolution: str, title: str = "", save_fix: bool = True) -> dict | None:
        """Close a ticket; by default the resolution becomes a fix in the library for next time."""
        t = self.update_ticket(tid, status="solved", resolution=resolution)
        if not t:
            return None
        if save_fix and resolution.strip():
            first = t["text"].strip().splitlines()[0][:100] if t["text"].strip() else "Ticket"
            f = self.add_fix(title or first, t["text"][:1500], resolution, t["product"], source=f"ticket:{tid}")
            with self.lock:
                self.db.execute("UPDATE tickets SET fix_id = ? WHERE id = ?", (f["id"], tid)); self.db.commit()
        return self.ticket(tid)

    def counts(self) -> dict:
        with self.lock:
            c = dict(self.db.execute("SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall())
            c["fixes"] = self.db.execute("SELECT COUNT(*) FROM fixes").fetchone()[0]
            c["docs"] = self.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        return c


def read_upload(name: str, data: bytes) -> str:
    """Text from an uploaded file: text, markdown, HTML, CSV, email, or PDF (if pypdf is installed)."""
    low = name.lower()
    if low.endswith(".pdf"):
        try:
            import io

            from pypdf import PdfReader
        except ImportError as e:
            raise ValueError("PDF reading isn't installed on the MSI yet; rebuild with docker compose up -d --build.") from e
        return "\n\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    text = data.decode("utf-8-sig", errors="replace")
    if low.endswith((".html", ".htm")):
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        import html as _html

        text = _html.unescape(text)
    return text
