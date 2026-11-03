"""Bridge to clAIr's Cloudflare Worker (private /wetworks route).

Wetworks sends the conversation plus live status; the Worker answers as clAIr on Gemini
and may open a mission. If the Worker can't be reached, comms falls back to local clAIr.
"""

import httpx


class BridgeError(RuntimeError):
    pass


class ClairBridge:
    def __init__(self, url: str, token: str, owner: str = "Patrick", timeout: float = 45):
        self.url = url.rstrip("/")
        self.token = token
        self.owner = owner
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def ask(self, history: list[dict], context: str) -> tuple[str, dict | None]:
        """history: [{"sender": "patrick" | "clair", "text": ...}], oldest first."""
        messages = [
            {"role": "user" if m["sender"] == "patrick" else "assistant", "content": m["text"]}
            for m in history
        ]
        try:
            r = httpx.post(
                self.url,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                json={"owner": self.owner, "context": context, "messages": messages},
                timeout=self.timeout,
            )
        except httpx.HTTPError as e:
            raise BridgeError(f"couldn't reach clAIr's Worker ({type(e).__name__})") from e
        if r.status_code == 401:
            raise BridgeError("clAIr's Worker rejected the bridge key; check WETWORKS_CLAIR_TOKEN")
        if r.status_code != 200:
            raise BridgeError(f"clAIr's Worker answered {r.status_code}")
        data = r.json()
        reply = str(data.get("reply") or "").strip()
        mission = data.get("mission")
        if not (isinstance(mission, dict) and str(mission.get("request", "")).strip()):
            mission = None
        if not reply:
            raise BridgeError("clAIr's Worker sent an empty reply")
        return reply, mission
