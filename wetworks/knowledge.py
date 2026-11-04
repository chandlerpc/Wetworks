"""Knowledge from outside Wetworks: curated knowledge files and Patrick's past Claude chats.

- config/knowledge/*.md: each "- " line becomes a memory (section heading = kind). Reloaded on start.
- Claude data export (conversations.json, or the export .zip): every conversation goes into a
  searchable chat archive, which agents can search with the search_past_chats tool.
"""

import json
import re
import sqlite3
import threading
import zipfile
from pathlib import Path

from .memory import KINDS


def sync_knowledge(memory, folder: Path) -> int:
    """Load every knowledge file as memories, replacing what each file loaded last time."""
    total = 0
    for path in sorted(folder.glob("*.md")) if folder.exists() else []:
        source = f"knowledge:{path.stem}"[:40]
        with memory.lock:
            memory.db.execute("DELETE FROM memories WHERE source = ?", (source,))
            memory.db.commit()
        kind = "fact"
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line.startswith("## "):
                k = line[3:].strip().lower()
                kind = k if k in KINDS else "fact"
            elif line.startswith(("- ", "* ")) and memory.add(line[2:], kind=kind, source=source):
                total += 1
    return total


class ChatArchive:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, created TEXT NOT NULL, text TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS chats_fts USING fts5(title, text, content='chats', content_rowid='rowid');
            CREATE TRIGGER IF NOT EXISTS chats_ai AFTER INSERT ON chats BEGIN
                INSERT INTO chats_fts(rowid, title, text) VALUES (new.rowid, new.title, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS chats_ad AFTER DELETE ON chats BEGIN
                INSERT INTO chats_fts(chats_fts, rowid, title, text) VALUES ('delete', old.rowid, old.title, old.text);
            END;
            """)
        self.db.commit()

    @staticmethod
    def _load_export(path: Path) -> list:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                name = next((n for n in z.namelist() if n.endswith("conversations.json")), None)
                if not name:
                    raise ValueError("No conversations.json in that zip")
                return json.loads(z.read(name))
        return json.loads(path.read_text())

    @staticmethod
    def _message_text(m: dict) -> str:
        text = m.get("text") or ""
        if not text and isinstance(m.get("content"), list):
            text = "\n".join(b.get("text", "") for b in m["content"] if isinstance(b, dict) and b.get("type") == "text")
        return text.strip()

    def import_claude_export(self, path: Path) -> tuple[int, int]:
        """Returns (imported, skipped). Re-importing the same export replaces existing chats."""
        data = self._load_export(Path(path))
        if not isinstance(data, list):
            raise ValueError("Expected a list of conversations (Claude's conversations.json)")
        imported = skipped = 0
        for conv in data:
            if not isinstance(conv, dict):
                skipped += 1
                continue
            lines = []
            for m in conv.get("chat_messages") or []:
                t = self._message_text(m) if isinstance(m, dict) else ""
                if t:
                    who = "Patrick" if m.get("sender") == "human" else "Claude"
                    lines.append(f"{who}: {t}")
            if not lines:
                skipped += 1
                continue
            cid = str(conv.get("uuid") or conv.get("id") or f"{conv.get('name')}-{conv.get('created_at')}")
            with self.lock:
                self.db.execute("DELETE FROM chats WHERE id = ?", (cid,))
                self.db.execute("INSERT INTO chats (id, title, created, text) VALUES (?, ?, ?, ?)",
                                (cid, str(conv.get("name") or "Untitled chat")[:200], str(conv.get("created_at") or "")[:25],
                                 "\n\n".join(lines)))
                self.db.commit()
            imported += 1
        return imported, skipped

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM chats").fetchone()[0]

    def search(self, query: str, limit: int = 6) -> list[dict]:
        words = re.findall(r"[A-Za-z0-9]{3,}", query or "")[:10]
        if not words:
            return []
        match = " OR ".join(f'"{w}"*' for w in words)
        with self.lock:
            rows = self.db.execute(
                "SELECT c.id, c.title, c.created, snippet(chats_fts, 1, '', '', ' … ', 40) FROM chats_fts "
                "JOIN chats c ON c.rowid = chats_fts.rowid WHERE chats_fts MATCH ? ORDER BY bm25(chats_fts) LIMIT ?",
                (match, limit)).fetchall()
        return [{"id": r[0], "title": r[1], "date": r[2][:10], "excerpt": r[3]} for r in rows]

    def read(self, cid: str, max_chars: int = 6000) -> str:
        with self.lock:
            r = self.db.execute("SELECT title, created, text FROM chats WHERE id = ?", (cid,)).fetchone()
        if not r:
            return ""
        return f"{r[0]} ({r[1][:10]})\n\n{r[2][:max_chars]}"


# ---- Handbook: the docs agents can read on demand --------------------------------------------
DOCS = ("docs/handbook.md", "docs/architecture.md", "README.md", "docs/art-spec.md")


def _doc_root() -> Path:
    return Path(__file__).resolve().parent.parent


def handbook_sections() -> list[tuple[str, str, str]]:
    """(doc, heading, text) for every ## / ### section of the docs."""
    out = []
    for rel in DOCS:
        p = _doc_root() / rel
        if not p.exists():
            continue
        head, buf = "(intro)", []
        for line in p.read_text().splitlines():
            if re.match(r"^#{2,3} ", line):
                if buf:
                    out.append((rel, head, "\n".join(buf).strip()))
                head, buf = line.lstrip("# ").strip(), []
            else:
                buf.append(line)
        if buf:
            out.append((rel, head, "\n".join(buf).strip()))
    return out


def read_handbook(section: str = "") -> str:
    """No section: the table of contents. Otherwise the best-matching section(s), in full."""
    secs = handbook_sections()
    if not secs:
        return "The handbook isn't available in this container."
    if not section.strip():
        lines, last = [], None
        for doc, head, _ in secs:
            if doc != last:
                lines.append(f"\n{doc}:"); last = doc
            lines.append(f"  - {head}")
        return "Sections (ask for one by name):" + "\n".join(lines)
    words = [w for w in re.findall(r"[a-z0-9]+", section.lower()) if len(w) > 2] or [section.lower()]
    def score(s):
        doc, head, text = s
        h, t = head.lower(), text.lower()
        return sum(5 * (w in h) + min(t.count(w), 5) for w in words)
    best = sorted(secs, key=score, reverse=True)
    if score(best[0]) == 0:
        return "No section matches that. Call read_handbook with no section to see the contents."
    out, size = [], 0
    for doc, head, text in best[:3]:
        if score((doc, head, text)) == 0 or size > 9000:
            break
        chunk = f"## {head}  ({doc})\n{text}"[:7000]
        out.append(chunk); size += len(chunk)
    return "\n\n".join(out)
