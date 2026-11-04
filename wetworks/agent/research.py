"""Research tools scoped to one agent run.

Every URL an agent sees (in search results or pages it reads) is tracked, and anything it
records must point at one of those URLs. That way findings and leads always link to real pages,
never to addresses a model made up.
"""

from .runtime import Tool, Toolbox


class Research:
    def __init__(self, llm, memory=None, time_range: str = "week", max_results: int = 8):
        self.llm = llm
        self.memory = memory
        self.time_range = time_range
        self.max_results = max_results
        self.seen: dict[str, dict] = {}   # url -> {"title", "content", "page"}
        self.searches = 0
        self.pages = 0

    def search(self, query: str, time_range: str = "") -> list[dict] | str:
        self.searches += 1
        results = self.llm.web_search_local(str(query), time_range=time_range or self.time_range, limit=self.max_results)
        for r in results:
            self.seen.setdefault(r["url"], {"title": r["title"], "content": r["content"]})
        return [{"title": r["title"], "url": r["url"], "snippet": r["content"][:300]} for r in results] or "No results."

    def read(self, url: str) -> str:
        url = str(url)
        if url not in self.seen:
            return "Only read pages that came up in your searches."
        self.pages += 1
        text = self.llm.read_page_local(url, max_chars=3500)
        if text:
            self.seen[url]["page"] = text
        return text or "Couldn't read that page; rely on its search snippet."

    def known(self, url: str) -> bool:
        return str(url) in self.seen

    def tools(self) -> list[Tool]:
        out = [
            Tool("search_web", "Search the web. Returns titles, URLs and snippets. Write your own specific queries.",
                 {"query": "what to search for", "time_range": "(optional) day | week | month | year"}, self.search),
            Tool("read_page", "Read a page from your search results when the snippet isn't enough.",
                 {"url": "a URL from your search results"}, self.read),
        ]
        if self.memory is not None:
            out.append(Tool("recall", "Check Wetworks' memory about Patrick and his business.",
                            {"query": "what to look for"},
                            lambda query: [m["text"] for m in self.memory.search(query, 8, include_pinned=True)]
                            or "Nothing remembered about that."))
        return out

    def toolbox(self, extra: list[Tool]) -> Toolbox:
        return Toolbox(self.tools() + extra)
