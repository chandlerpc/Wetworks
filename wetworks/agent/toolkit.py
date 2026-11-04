"""The tools Wetworks agents can use. Everything inside Wetworks runs on its own;
anything that leaves the system or can't be undone is gated behind Patrick's approval."""

import re

from .runtime import Tool, Toolbox


def build_toolbox(*, llm, memory, missions, open_mission, findings, leads, revenue, notify,
                  followups, workspace=None, agent_name="architect", daily_mission_cap=6, commander=None,
                  finance=None, finance_level="none", dayjob=None, dayjob_local=False, observer=None, observer_local=False, content=None,
                  chats=None, forge=None) -> Toolbox:
    t = Toolbox()

    # ---- looking things up ---------------------------------------------------
    t.add(Tool("search_web", "Search the web (free, private search). Returns titles, URLs and snippets.",
               {"query": "what to search for", "time_range": "(optional) day | week | month | year"},
               lambda query, time_range="month": llm.web_search_local(query, time_range=time_range, limit=8)))
    t.add(Tool("read_page", "Read the text of a public web page.", {"url": "a URL from search results"},
               lambda url: llm.read_page_local(url, max_chars=3500) or "Couldn't read that page."))
    t.add(Tool("recall", "Search Wetworks' memory about Patrick, his business, people and past decisions.",
               {"query": "what to look for"},
               lambda query: [m["text"] for m in memory.search(query, 10, include_pinned=True)] or "Nothing remembered about that."))

    if chats is not None:
        t.add(Tool("search_past_chats", "Search Patrick's past Claude conversations (his earlier work, projects and "
                   "decisions). Returns titles, dates and excerpts.", {"query": "what to look for"},
                   lambda query: chats.search(query) or "Nothing in the past chats matches that."))
        t.add(Tool("read_past_chat", "Read one past Claude conversation found with search_past_chats.",
                   {"id": "the chat's id from search results"},
                   lambda id: chats.read(str(id)) or "No chat with that id."))

    # ---- Wetworks itself -----------------------------------------------------
    def list_missions(status=""):
        rows = missions.recent(30)
        if status:
            rows = [m for m in rows if m["status"] == status]
        return [{"id": m["id"], "title": m["title"], "status": m["status"], "kind": m.get("kind", "research"),
                 "source": m["source"], "updated": m["updated"][:16], "result": m.get("result_preview", "")[:200]}
                for m in rows[:15]] or "No missions."

    def get_mission(id):
        m = missions.get(int(id))
        if not m:
            return f"No mission #{id}."
        return {"id": m["id"], "title": m["title"], "status": m["status"], "request": m["request"],
                "result": m["result"][:2000], "sources": m["sources"][:6], "steps": [s["text"] for s in m["steps"]][-6:]}

    def start_mission(title, request, kind="research"):
        open_now = [m for m in missions.recent(50) if m["status"] in ("queued", "running")]
        if len(open_now) >= 3:
            return "Three missions are already queued or running. Wait for those before opening more."
        today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
        mine_today = [m for m in missions.recent(100) if m["source"] == agent_name and m["created"][:10] == today]
        if len(mine_today) >= daily_mission_cap:
            return f"Daily limit reached: you've opened {daily_mission_cap} missions today."
        m = open_mission(str(title), str(request), agent_name, "code" if kind == "code" else "research")
        return f"Opened {'code ' if m['kind'] == 'code' else ''}mission #{m['id']}: {m['title']}"

    t.add(Tool("list_missions", "List recent missions and their status.",
               {"status": "(optional) queued | running | done | failed"}, list_missions))
    t.add(Tool("get_mission", "Read one mission's request, result and sources.", {"id": "mission number"}, get_mission))
    t.add(Tool("start_mission", "Hand work to a Splinter team: research (web research + write-up) or code (a pull "
               "request on Wetworks, which Patrick reviews). Use for anything that takes real work.",
               {"title": "short title", "request": "the full task, self-contained", "kind": "(optional) research | code"},
               start_mission))
    t.add(Tool("recent_findings", "Latest Intel and Revenue findings.", {},
               lambda: [{"topic": tp, "title": ti, "summary": s[:200], "importance": i, "url": u}
                        for tp, ti, s, i, u in findings.db.execute(
                            "SELECT topic, title, summary, importance, url FROM findings ORDER BY ts DESC LIMIT 15")]
               or "No findings yet."))

    def list_leads(status=""):
        rows = leads.due_followups(20) if status == "due" else leads.recent(status=status or None, limit=20)
        return [{"id": l["id"], "business": l["business"], "fit": l["fit"], "offer": l["offer"], "status": l["status"],
                 "signal": l["signal"][:160], "has_draft": bool(l["draft"]), "notes": l["notes"][:120],
                 "followup_due": l["followup_due"][:10], "followups_sent": l["followups"],
                 "research": "running" if l["research_mission"] and not l["research"] else ("ready" if l["research"] else "none")}
                for l in rows] or "No leads."

    def research_lead(id):
        l = leads.get(int(id))
        if not l:
            return f"No lead #{id}."
        if l["research_mission"] and not l["research"]:
            return f"Already being researched in mission #{l['research_mission']}."
        if len([m for m in missions.recent(50) if m["status"] in ("queued", "running")]) >= 3:
            return "Three missions are already queued or running. Try again after they finish."
        leads.set_research(l["id"], text="")
        m = revenue.research(leads.get(l["id"]))
        return f"Mission #{m['id']} queued to research {l['business']}. The write-up lands on the lead." if m \
            else "Missions aren't available right now."

    def note_lead(id, notes="", dismiss=False):
        l = leads.get(int(id))
        if not l:
            return f"No lead #{id}."
        merged = (l["notes"] + "\n" if l["notes"] else "") + f"[{agent_name}] {notes}".strip()
        leads.update(l["id"], notes=merged if notes else None, status="dismissed" if dismiss in (True, "true", "yes") else None)
        return f"Updated lead #{id}."

    def draft_outreach(id):
        l = leads.get(int(id))
        if not l:
            return f"No lead #{id}."
        text = revenue.draft(l)
        return text or "Couldn't write a draft just now."

    t.add(Tool("list_leads", "Leads the Revenue cell found (businesses that might hire Patrick).",
               {"status": "(optional) due (follow-up due) | new | contacted | replied | won | lost"}, list_leads))
    t.add(Tool("research_lead", "Send a Splinter to research a lead in depth before Patrick reaches out. Contacts no one.",
               {"id": "lead number"}, research_lead))
    t.add(Tool("note_lead", "Add research notes to a lead, or dismiss it as not a fit.",
               {"id": "lead number", "notes": "what you found", "dismiss": "(optional) true to dismiss"}, note_lead))
    t.add(Tool("draft_outreach", "Write (or rewrite) the outreach email draft for a lead. It is NOT sent.",
               {"id": "lead number"}, draft_outreach))

    # ---- money (Patrick's; detail stays with local brains) ------------------------
    if finance is not None and finance_level != "none":
        t.add(Tool("finance_summary", "Patrick's money this month: spending by category vs budget, bills and "
                   "subscriptions due soon, recurring monthly total." + (" Totals only." if finance_level != "local" else ""),
                   {}, lambda: finance.summary_for(finance_level)))
    elif finance is not None:
        t.add(Tool("finance_summary", "Patrick's money. Details stay on the MSI and aren't shared with you.",
                   {}, lambda: "Finance details stay on the MSI and aren't shared with cloud models. "
                               "Point Patrick to the Finance room in the console."))

    # ---- day job (work material: local brains only, never cloud) ------------------
    if dayjob is not None and dayjob_local:
        t.add(Tool("dayjob_search", "Search Patrick's Novarad support notes and past fixes (titles and snippets).",
                   {"query": "what to look for"},
                   lambda query: [{"kind": h["kind"], "title": h["title"], "snippet": h["snippet"]} for h in dayjob.search(str(query))]
                   or "Nothing in the library matches."))
    elif dayjob is not None:
        t.add(Tool("dayjob_search", "Patrick's Novarad support notes. They stay on the MSI and aren't shared with you.",
                   {"query": "what to look for"},
                   lambda query: "Day Job material stays on the MSI and isn't shared with cloud models. "
                                 "Point Patrick to the Day Job room in the console."))

    # ---- how Patrick's day goes (The Observer; local brains only) ------------------
    if observer is not None and observer_local:
        def observer_today():
            d = observer.day()
            return {"active_minutes": d["active_minutes"], "first": d["first"], "apps": d["apps"][:8],
                    "focus_blocks": d["focus_blocks"], "doing_lately": [s["what"] for s in d["screens"][-5:]],
                    "habits": observer.patterns()}
        t.add(Tool("observer_today", "How Patrick's day is going: active time, apps, focus blocks, what he's been doing, "
                   "and his usual habits. Use it to time things well and avoid interrupting focus.", {}, observer_today))
    elif observer is not None:
        t.add(Tool("observer_today", "How Patrick's day is going. It stays on the MSI and isn't shared with you.", {},
                   lambda: "Patrick's activity stays on the MSI and isn't shared with cloud models."))

    # ---- content ideas (fine for any brain: no private data; drafting stays local) --------
    if content is not None:
        t.add(Tool("content_ideas", "Patrick's post ideas, drafts and posting calendar (titles and status).", {},
                   lambda: [{"id": p["id"], "title": p["title"], "status": p["status"], "planned": p["planned"]}
                            for p in content.pieces(limit=20)] or "No ideas yet."))
        t.add(Tool("add_content_idea", "Add a post idea for Patrick (The Writer drafts it; nothing is posted).",
                   {"title": "the idea", "angle": "the specific point or story"},
                   lambda title, angle="": (lambda p: f"Added idea #{p['id']}." if p else "Already have that idea.")(
                       content.add_idea(str(title), str(angle), source=agent_name))))

    # ---- the handbook: how Wetworks works and why (no private data) --------------
    from ..knowledge import read_handbook

    t.add(Tool("read_handbook", "Read Wetworks' own docs: how every part works, the rules, decisions, history and "
               "how to operate or change it. No section = table of contents.",
               {"section": "(optional) a section name or topic, e.g. 'privacy', 'The Forge', 'schedule'"}, read_handbook))

    # ---- memory and time -------------------------------------------------------
    t.add(Tool("remember", "Save a durable fact Patrick stated (a preference, decision, project, person).",
               {"text": "one plain sentence", "kind": "(optional) preference | fact | project | person | decision"},
               lambda text, kind="fact": "Saved." if memory.add(text, kind=kind, source=agent_name) else "Already known."))
    t.add(Tool("follow_up_later", "Leave yourself a note to come back to something later.",
               {"note": "what to check", "hours": "how many hours from now"},
               lambda note, hours=24: (lambda f: f"Follow-up #{f['id']} set for {f['due'][:16]} UTC.")(followups.add(agent_name, note, float(hours)))))

    # ---- telling Patrick (internal: his own channels) ---------------------------
    def tell_patrick(message):
        notify.send(f"🛰️ **{agent_name.title()}**: {message}")
        return "Sent to Patrick."

    if agent_name != "clair":  # C.L.a.i.R is already talking to him
        t.add(Tool("tell_patrick", "Send Patrick a short message (Discord). Only for things he needs to know now.",
                   {"message": "what to tell him"}, tell_patrick))

    if commander is not None:
        def tell_architect(note):
            followups.add("architect", f"From {agent_name}: {note}", 0.25)
            commander.soon(f"note from {agent_name}: {note[:80]}", delay_seconds=60)
            return "Passed to The Architect; he'll pick it up within a few minutes."

        t.add(Tool("tell_architect", "Hand something to The Architect: strategy, priorities, or work that needs "
                   "several missions coordinated.", {"note": "what he should know or do"}, tell_architect))

    # ---- The Forge -----------------------------------------------------------------
    if forge is not None:
        def forge_status():
            return {"forge": forge.health(), "datasets": forge.datasets(), "jobs": forge.jobs(5),
                    "local_models": forge.models(), "benchmarks": forge.evals(5),
                    "agent_decisions_recorded": forge.traces.counts(), "agents_use": forge.llm.ollama.model}

        def queue_training(name, dataset="agent-steps"):
            try:
                j = forge.queue(str(name), str(dataset))
            except Exception as e:
                return f"Not queued: {e}"
            return f"Queued training job {j['id']} for tonight's window ({forge.s.forge_window} local)."

        def benchmark(model, dataset="agent-steps"):
            try:
                return forge.evaluate(str(model), str(dataset))
            except Exception as e:
                return f"Benchmark didn't run: {e}"

        t.add(Tool("forge_status", "The Forge: training jobs, datasets, local models, benchmarks, recorded decisions.",
                   {}, forge_status))
        t.add(Tool("build_training_data", "Rebuild the agent-steps dataset from decisions recorded from Gemini/Claude.",
                   {}, lambda: forge.build_agent_dataset()))
        t.add(Tool("queue_training", "Queue a fine-tuning job on the GPU for tonight's training window.",
                   {"name": "short name for the new model", "dataset": "(optional) dataset name, default agent-steps"},
                   queue_training))
        t.add(Tool("benchmark_model", "Score a local model on held-out agent decisions (how reliably it drives agents).",
                   {"model": "Ollama model name", "dataset": "(optional) default agent-steps"}, benchmark))
        t.add(Tool("switch_local_model", "Make every agent think with a different local model.",
                   {"model": "Ollama model name"}, lambda model: forge.use_model(str(model)), gate="approve",
                   summarize=lambda a: f"Switch all agents' local model to {a.get('model')}"))

    # ---- gated: leaves Wetworks or can't be undone ------------------------------
    if workspace is not None:
        def merge(pr):
            n = int(re.sub(r"\D", "", str(pr)) or 0)
            return workspace.merge_pr(n)

        t.add(Tool("merge_pull_request", "Merge a Wetworks pull request into main.",
                   {"pr": "pull request number"}, merge, gate="approve",
                   summarize=lambda a: f"Merge pull request #{a.get('pr')} into main"))
    return t
