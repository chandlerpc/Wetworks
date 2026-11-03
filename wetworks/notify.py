"""Outbound notifications. Discord first; phone/C.L.a.i.R destinations plug in here later."""

import httpx

DISCORD_LIMIT = 2000


def chunk(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line breaks so no Discord message exceeds the limit."""
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts


class Notifier:
    def __init__(self, webhook_url: str, dry_run: bool = False):
        self.url = webhook_url
        self.dry_run = dry_run or not webhook_url

    def send(self, text: str, username: str = "The Architect") -> None:
        for part in chunk(text):
            if self.dry_run:
                print(f"--- [{username}] ---\n{part}")
                continue
            r = httpx.post(self.url, json={"username": username, "content": part}, timeout=15)
            r.raise_for_status()
