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
        # Thinking models can spend the whole token budget reasoning privately and return an
        # empty answer. Wetworks wants direct answers, so thinking is switched off.
        body["think"] = False

        tin = tout = 0
        for attempt in range(2):
            r = httpx.post(f"{self.base}/api/chat", json=body, timeout=self.timeout)
            if r.status_code == 400 and "think" in r.text.lower() and "think" in body:
                body.pop("think")  # model doesn't support the switch; ask normally
                r = httpx.post(f"{self.base}/api/chat", json=body, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            tin += int(data.get("prompt_eval_count", 0))
            tout += int(data.get("eval_count", 0))
            text = (data.get("message", {}).get("content") or "").strip()
            if text:
                return text, tin, tout
            # Empty answer: give it more room once before giving up.
            log.warning("Ollama returned an empty answer (attempt %d); retrying with more room", attempt + 1)
            body["options"]["num_predict"] = max_tokens * 2
        return "", tin, tout


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


class _TextExtractor:
    """Pulls readable text out of HTML with the standard library (no extra dependencies)."""

    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside"}

    def __init__(self):
        from html.parser import HTMLParser

        outer = self

        class P(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.depth = 0
                self.parts: list[str] = []

            def handle_starttag(self, tag, attrs):
                if tag in outer.SKIP:
                    self.depth += 1

            def handle_endtag(self, tag):
                if tag in outer.SKIP and self.depth:
                    self.depth -= 1

            def handle_data(self, data):
                if not self.depth and data.strip():
                    self.parts.append(data.strip())

        self.parser = P()

    def text(self, html: str) -> str:
        self.parser.feed(html)
        return " ".join(self.parser.parts)


def is_public_url(url: str) -> bool:
    """True only for http(s) URLs whose host resolves to public internet addresses.

    Stops research from reaching the Docker network (Ollama, SearXNG), the home LAN,
    Tailscale devices or cloud metadata endpoints.
    """
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (ValueError, OSError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip in ipaddress.ip_network("100.64.0.0/10"):
            return False
    return True


def fetch_text(url: str, max_chars: int = 3000, timeout: float = 15) -> str:
    """Download a public page and return its readable text, or '' if it can't be read."""
    if not is_public_url(url):
        return ""
    try:
        # Redirects are not followed blindly: a public page could redirect to a private address.
        r = httpx.get(url, timeout=timeout, follow_redirects=False,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; WetworksBot/0.1)"})
        hops = 0
        while r.status_code in (301, 302, 303, 307, 308) and hops < 3:
            nxt = str(r.next_request.url) if r.next_request else ""
            if not is_public_url(nxt):
                return ""
            r = httpx.get(nxt, timeout=timeout, follow_redirects=False,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; WetworksBot/0.1)"})
            hops += 1
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
        return _TextExtractor().text(r.text[:400_000])[:max_chars]
    except Exception as e:  # network trouble is normal; the Splinter carries on with snippets
        log.info("Couldn't read %s: %s", url, e)
        return ""
