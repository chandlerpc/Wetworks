"""Revenue — Sleeper cell that finds local businesses that could use Patrick's services.

Each morning it runs a few search terms from config/revenue.toml (rotating so every run looks
somewhere new), lets the local model pick real businesses showing a sign of need, scores them
for fit, and drafts a short outreach email for the best ones. It never contacts anyone: the
leads and drafts wait in the console for Patrick.
"""

import tomllib
from datetime import date
from pathlib import Path

from ..findings import Finding
from ..leads import LeadStore, domain_of
from ..local import candidates_block
from .base import Cell
from .intel import parse_findings

SYSTEM = (
    "You are the Revenue cell in Wetworks, finding real clients for a one-person AI and software "
    "consultancy. Be strict: only real, local, independent businesses with a concrete sign they "
    "need help. Use only facts in the search results. Never invent businesses, people or details."
)

SCORE_PROMPT = """Services on offer:
{services}

Ideal customers:
{ideal}

Area: {area}

Search results:
{candidates}

Pick the results that are about a real, independent business in the area with a sign it could use
one of the services (hiring for admin or front-desk work, booking strain, a new opening, manual
processes, growth). Skip job boards themselves, national chains, and software or AI companies.
Reply with ONLY this JSON object:
{{"leads": [{{"index": <result number>, "title": "the business's name, exactly as written",
  "location": "town if stated", "signal": "what in the result suggests they need help",
  "offer": "Chatbot" or "Automation" or "Dashboard", "why": "one sentence on why that service fits",
  "importance": <fit 1-5, 5 = clear need and clear fit>}}]}}
Use an empty list if nothing qualifies."""

DRAFT_PROMPT = """Write a short first-contact email from {owner} ({company}) to {business}.

What we noticed: {signal}
Service to offer: {offer} ({why})
Area: {area}

Rules: under 120 words; plain and specific to what we noticed; friendly, local, no hype; do NOT
invent past clients, results, prices or facts about them; one soft ask (a 15-minute call this week);
sign off as {owner}, {company}.
Reply with ONLY this JSON object: {{"subject": "...", "body": "..."}}"""

OFFER_NAMES = {"chatbot": "Chatbot", "automation": "Automation", "dashboard": "Dashboard"}


class Revenue(Cell):
    name = "revenue"
    kind = "sleeper"

    def __init__(self, ctx, leads: LeadStore, config_path: Path | None = None):
        super().__init__(ctx)
        self.leads = leads
        self.config_path = config_path or ctx.settings.config_dir / "revenue.toml"
        self.config = self._load()

    def _load(self) -> dict:
        with open(self.config_path, "rb") as f:
            cfg = tomllib.load(f).get("revenue", {})
        start = cfg.get("trial_start")
        self.trial_start = date.fromisoformat(str(start)) if start else None
        return cfg

    def _ask(self, agent: str, prompt: str, max_tokens: int) -> str:
        llm = self.ctx.llm
        if self.ctx.settings.provider == "local":
            return llm.ask_local(agent, prompt, system=SYSTEM, json_mode=True, max_tokens=max_tokens).text
        return llm.ask(agent, prompt, tier="work", system=SYSTEM, max_tokens=max_tokens).text

    def todays_queries(self, today: date | None = None) -> list[str]:
        qs = list(self.config.get("queries", []))
        n = max(1, int(self.config.get("queries_per_run", 4)))
        if not qs:
            return []
        start = ((today or date.today()).toordinal() * n) % len(qs)
        return [qs[(start + i) % len(qs)] for i in range(min(n, len(qs)))]

    def run(self) -> list[Finding]:
        self.config = self._load()
        if not self.config.get("enabled", True):
            return []
        aggregators = set(self.config.get("aggregators", []))
        is_agg = lambda url: any(domain_of(url) == a or domain_of(url).endswith("." + a) for a in aggregators)

        if self.ctx.ask is not None and self.ctx.settings.cells_agentic:
            new, searched = self._hunt_agent(is_agg)
            if not searched:
                self.log.warning("Revenue agent didn't use its tools; running the fixed routine")
                new = self._hunt_fixed(is_agg)
        else:
            new = self._hunt_fixed(is_agg)
        return self._finish(new)

    def _hunt_agent(self, is_agg) -> tuple[list[dict], bool]:
        """Revenue as an agent: it chooses where to look, checks businesses, and records real leads."""
        from ..agent.research import Research
        from ..agent.runtime import Agent, Tool

        cap = int(self.config.get("max_new_leads", 8))
        research = Research(self.ctx.llm, self.ctx.memory, time_range="month")
        new: list[dict] = []

        def record_lead(business, url, signal, fit, offer="", why="", location=""):
            if len(new) >= cap:
                return f"You've recorded {cap} leads, the limit for this run. Finish now."
            if not research.known(url):
                return "That URL didn't come up in your searches. Only record businesses you actually found."
            try:
                fit_n = int(fit)
            except (TypeError, ValueError):
                fit_n = 3
            lead = self.leads.add(business=str(business), url=str(url), signal=str(signal), fit=fit_n,
                                  offer=OFFER_NAMES.get(str(offer).strip().lower(), ""), why=str(why),
                                  location=str(location), own_site=not is_agg(url))
            if not lead:
                return "Already in the pipeline (or no name given); skip it."
            new.append(lead)
            return f"Recorded lead #{lead['id']} ({len(new)}/{cap})."

        services = "\n".join(f"- {s}" for s in self.config.get("services", []))
        goal = (f"Find new clients for {self.config.get('company', 'the consultancy')} in "
                f"{self.config.get('area', 'the local area')}.\n\nServices:\n{services}\n\nIdeal customers:\n"
                f"{str(self.config.get('ideal_customers', '')).strip()}\n\nSearch for real, local, independent businesses "
                "showing a concrete sign they need help (hiring admin or front-desk staff, booking strain, a new opening, "
                f"manual processes, growth). Starting ideas for searches: {'; '.join(self.todays_queries())}. Vary them. "
                "Read a page when you need to confirm a business is real and local. Record each good one with "
                f"record_lead (fit 1-5; offer Chatbot, Automation or Dashboard), at most {cap}. Skip job boards themselves, "
                "chains, and software or AI companies. Finish with done and one line on what you found.")
        agent = Agent("revenue", "You are the Revenue operative in Wetworks, finding real clients for a one-person AI "
                      "and software consultancy. Be strict and never invent businesses or details.",
                      research.toolbox([Tool("record_lead", "Record a business as a lead for Patrick.",
                                             {"business": "the business's name exactly as written", "url": "its page from "
                                              "your results", "signal": "what suggests they need help", "fit": "1-5",
                                              "offer": "(optional) Chatbot | Automation | Dashboard",
                                              "why": "(optional) why that service fits", "location": "(optional) town"},
                                             record_lead)]),
                      self.ctx.ask, events=None, max_steps=12)
        run = agent.run(goal)
        self.log.info("Revenue agent: %d leads, %d searches, %d pages, %d steps", len(new), research.searches,
                      research.pages, len(run.steps))
        return new, research.searches > 0

    def _hunt_fixed(self, is_agg) -> list[dict]:
        # 1. Search
        results, seen = [], set()
        for q in self.todays_queries():
            for r in self.ctx.llm.web_search_local(q, time_range="month", limit=8):
                if r["url"] in seen or (not is_agg(r["url"]) and self.leads.known(r["url"])):
                    continue
                seen.add(r["url"])
                results.append(r)
        results = results[:24]
        if not results:
            self.log.info("Revenue: no fresh search results")
            return []
        self._results_seen = len(results)

        # 2. Score
        services = "\n".join(f"- {s}" for s in self.config.get("services", []))
        raw = self._ask("revenue:score", SCORE_PROMPT.format(
            services=services, ideal=str(self.config.get("ideal_customers", "")).strip(),
            area=self.config.get("area", ""), candidates=candidates_block(results)), max_tokens=1500)
        new = []
        for item in parse_findings(raw):
            try:
                src = results[int(item.get("index"))]
                fit = int(item.get("importance", 3))
            except (TypeError, ValueError, IndexError):
                continue  # pointed at a result that doesn't exist; drop rather than guess
            offer = OFFER_NAMES.get(str(item.get("offer", "")).strip().lower(), "")
            lead = self.leads.add(
                business=str(item["title"]), url=src["url"], signal=str(item.get("signal", "")),
                fit=fit, offer=offer, why=str(item.get("why", "")), location=str(item.get("location", "")),
                own_site=not is_agg(src["url"]),
            )
            if lead:
                new.append(lead)
            if len(new) >= int(self.config.get("max_new_leads", 8)):
                break
        return new

    def _finish(self, new: list[dict]) -> list[Finding]:
        # 3. Draft outreach for the best fits (still never sent)
        min_fit = int(self.config.get("draft_min_fit", 4))
        for lead in self.leads.needing_drafts(min_fit, int(self.config.get("max_drafts", 4))):
            self.draft(lead)

        # 4. Surface strong leads in the brief
        found = []
        for lead in new:
            if lead["fit"] >= min_fit:
                f = Finding(cell=self.name, topic="Revenue leads",
                            title=f"{lead['business']}{': ' + lead['offer'] if lead['offer'] else ''}",
                            summary=lead["signal"] or lead["why"], url=lead["url"],
                            importance=4 if lead["fit"] >= 5 else 3)
                if self.ctx.findings.add(f):
                    found.append(f)
        self.log.info("Revenue: %d new leads (%d strong)", len(new), len(found))
        return found

    def draft(self, lead: dict) -> str:
        raw = self._ask(f"revenue:draft:{lead['id']}", DRAFT_PROMPT.format(
            owner=self.config.get("owner", "Patrick"), company=self.config.get("company", ""),
            business=lead["business"], signal=lead["signal"] or "(not stated)",
            offer=lead["offer"] or "help with AI and automation", why=lead["why"] or "",
            area=self.config.get("area", "")), max_tokens=500)
        from ..code.brain import parse_json_obj

        data = parse_json_obj(raw)
        subject, body = str(data.get("subject", "")).strip(), str(data.get("body", "")).strip()
        if not body:
            return ""
        text = f"Subject: {subject}\n\n{body}" if subject else body
        self.leads.update(lead["id"], draft=text)
        return text
