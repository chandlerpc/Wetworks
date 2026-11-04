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

{research}Rules: under 120 words; plain and specific to what we noticed; friendly, local, no hype; do NOT
invent past clients, results, prices or facts about them; one soft ask (a 15-minute call this week);
sign off as {owner}, {company}.
Reply with ONLY this JSON object: {{"subject": "...", "body": "..."}}"""

FOLLOWUP_PROMPT = """Write follow-up #{n} from {owner} ({company}) to {business}. They haven't replied to this
email, sent {days} days ago:

{previous}

Rules: under 70 words; polite and brief; add one new useful point instead of repeating the first email;
no guilt, no "just checking in", no invented facts; {closing}; sign off as {owner}, {company}.
Reply with ONLY this JSON object: {{"subject": "...", "body": "..."}}"""

RESEARCH_REQUEST = """Research {business} before Patrick reaches out to them. Website: {url}. Area: {location}.
What we noticed: {signal}
What Patrick would offer: {offer}

Find, citing sources: what they do and roughly how big they are; who runs it or makes decisions (only
names that are public on their site or news); the booking, intake or office software they appear to use;
anything recent (openings, hiring, reviews mentioning wait times or missed calls). Finish with the best
angle for a first message and anything that makes them a poor fit. Do not contact them."""

OFFER_NAMES = {"chatbot": "Chatbot", "automation": "Automation", "dashboard": "Dashboard"}


class Revenue(Cell):
    name = "revenue"
    kind = "sleeper"

    def __init__(self, ctx, leads: LeadStore, config_path: Path | None = None):
        super().__init__(ctx)
        self.leads = leads
        self.open_mission = None  # set by main: queues a Splinter research mission
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
        nudges = self.check_followups()
        aggregators = set(self.config.get("aggregators", []))
        is_agg = lambda url: any(domain_of(url) == a or domain_of(url).endswith("." + a) for a in aggregators)

        if self.ctx.ask is not None and self.ctx.settings.cells_agentic:
            new, searched = self._hunt_agent(is_agg)
            if not searched:
                self.log.warning("Revenue agent didn't use its tools; running the fixed routine")
                new = self._hunt_fixed(is_agg)
        else:
            new = self._hunt_fixed(is_agg)
        return nudges + self._finish(new)

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
        min_fit = int(self.config.get("draft_min_fit", 4))
        # 3. Research the best fits before drafting (a Splinter mission per lead; drafts wait for it)
        self.queue_research(min_fit)
        # 4. Draft outreach for the best fits (still never sent)
        for lead in self.leads.needing_drafts(min_fit, int(self.config.get("max_drafts", 4))):
            self.draft(lead)

        # 5. Surface strong leads in the brief
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

    def _email(self, raw: str) -> str:
        from ..code.brain import parse_json_obj

        data = parse_json_obj(raw)
        subject, body = str(data.get("subject", "")).strip(), str(data.get("body", "")).strip()
        if not body:
            return ""
        return f"Subject: {subject}\n\n{body}" if subject else body

    def draft(self, lead: dict) -> str:
        research = lead.get("research", "")
        research = (f"Research done before outreach (use only what helps; don't recite it):\n{research[:2500]}\n\n"
                    if research and not research.startswith("(") else "")
        text = self._email(self._ask(f"revenue:draft:{lead['id']}", DRAFT_PROMPT.format(
            owner=self.config.get("owner", "Patrick"), company=self.config.get("company", ""),
            business=lead["business"], signal=lead["signal"] or "(not stated)",
            offer=lead["offer"] or "help with AI and automation", why=lead["why"] or "",
            area=self.config.get("area", ""), research=research), max_tokens=500))
        if text:
            self.leads.update(lead["id"], draft=text)
        return text

    # ---- research before outreach ----------------------------------------------------------------
    def research(self, lead: dict) -> dict | None:
        """Queue a Splinter mission that researches one lead. Returns the mission, or None if unavailable."""
        if self.open_mission is None:
            return None
        m = self.open_mission(f"Research {lead['business']} before outreach"[:80], RESEARCH_REQUEST.format(
            business=lead["business"], url=lead["url"] or "(unknown)", location=lead["location"] or self.config.get("area", ""),
            signal=lead["signal"] or "(not stated)", offer=lead["offer"] or "AI and automation help"),
            f"lead:{lead['id']}")
        self.leads.set_research(lead["id"], mission=m["id"])
        return m

    def queue_research(self, min_fit: int) -> int:
        if self.open_mission is None or not self.config.get("research_before_outreach", True):
            return 0
        n = 0
        for lead in self.leads.needing_research(min_fit, int(self.config.get("research_per_run", 2))):
            if self.research(lead):
                n += 1
        return n

    def mission_finished(self, mission: dict) -> bool:
        """Operator callback. Attaches a lead-research write-up to its lead and drafts the first email."""
        src = str(mission.get("source", ""))
        if not src.startswith("lead:"):
            return False
        lead = self.leads.get(int(src.split(":", 1)[1]))
        if not lead:
            return True
        if mission["status"] == "done" and mission.get("result"):
            sources = "\n".join(f"[{s['n']}] {s['url']}" for s in mission.get("sources") or [])
            text = mission["result"].strip() + (f"\n\nSources:\n{sources}" if sources else "")
        else:
            text = f"(research mission #{mission['id']} {mission['status']})"
        lead = self.leads.set_research(lead["id"], text=text)
        if lead["status"] == "new" and not lead["draft"]:
            self.draft(lead)
        if self.ctx.events is not None:
            self.ctx.events.add("revenue", "done" if not text.startswith("(") else "warn",
                                f"Lead #{lead['id']} {lead['business']}: research "
                                f"{'ready and outreach drafted' if not text.startswith('(') else 'did not finish'}")
        return True

    # ---- follow-ups ------------------------------------------------------------------------------
    def followup_schedule(self) -> list[int]:
        """Days to wait: first entry after the first email, then after each follow-up."""
        return [max(1, int(d)) for d in self.config.get("followup_days", [4, 7])]

    def next_followup_days(self, done: int) -> int | None:
        sched = self.followup_schedule()
        return sched[done] if done < len(sched) else None

    def check_followups(self) -> list[Finding]:
        """Draft follow-ups for contacted leads that are due, and put them in the brief. Never sends."""
        out = []
        sched = self.followup_schedule()
        for lead in self.leads.due_followups():
            n = lead["followups"] + 1
            if not lead["followup_draft"] and lead["draft"]:
                text = self._email(self._ask(f"revenue:followup:{lead['id']}", FOLLOWUP_PROMPT.format(
                    n=n, owner=self.config.get("owner", "Patrick"), company=self.config.get("company", ""),
                    business=lead["business"], previous=lead["draft"][:1500],
                    days=sum(sched[:n]) if sched else "a few",
                    closing="make this the last nudge and leave the door open" if n >= len(sched)
                    else "one soft ask"), max_tokens=350))
                if text:
                    self.leads.set_followup_draft(lead["id"], text)
            f = Finding(cell=self.name, topic="Revenue follow-ups",
                        title=f"Follow up #{n} with {lead['business']}",
                        summary="No reply yet. A follow-up draft is ready in Leads." if lead["draft"]
                        else "No reply yet. Time for a follow-up.",
                        url=f"{lead['url'] or 'lead:' + str(lead['id'])}#followup-{n}-{lead['followup_due'][:10]}",
                        importance=3)
            if self.ctx.findings.add(f):
                out.append(f)
        if out and self.ctx.events is not None:
            self.ctx.events.add("revenue", "info", f"{len(out)} lead{'s' if len(out) > 1 else ''} due a follow-up today")
        return out
