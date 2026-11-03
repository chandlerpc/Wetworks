"""Free, local providers: Ollama (models on the MSI's GPU) and SearXNG (self-hosted search)."""

import json
import logging

import httpx

log = logging.getLogger("wetworks.local")


class Ollama:
    def __init__(self, base_url: str, model: str, num_ctx: int = 16384, timeout: float = 300):
        self.base = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout = timeout

    def chat(self, prompt: str, system: str = "", json_mode: bool = False, max_tokens: int = 1024) -> tuple[str, int, int]:
        """Return (text, input_tokens, output_tokens)."""
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"num_ctx": self.num_ctx, "num_predict": max_tokens, "temperature": 0.3},
        }
        if json_mode:
            body["format"] = "json"
        r = httpx.post(f"{self.base}/api/chat", json=body, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return data["message"]["content"], int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0))


class SearXNG:
    def __init__(self, base_url: str, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def search(self, query: str, time_range: str = "week", limit: int = 8) -> list[dict]:
        """Return [{title, url, content}] for a query. time_range: day | week | month | year."""
        r = httpx.get(
            f"{self.base}/search",
            params={"q": query, "format": "json", "time_range": time_range, "language": "en"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        out = []
        for item in r.json().get("results", [])[:limit]:
            if item.get("url"):
                out.append({
                    "title": item.get("title", "").strip(),
                    "url": item["url"],
                    "content": (item.get("content") or "").strip()[:400],
                })
        return out


def candidates_block(results: list[dict]) -> str:
    return "\n".join(f"[{i}] {r['title']}\n    {r['url']}\n    {r['content']}" for i, r in enumerate(results))


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
