"""The tools Wetworks agents can use. Everything inside Wetworks runs on its own;
anything that leaves the system or can't be undone is gated behind Patrick's approval."""

import re

from .runtime import Tool, Toolbox


def build_toolbox(*, llm, memory, missions, open_mission, findings, leads, revenue, notify,
                  followups, workspace=None, agent_name="architect", daily_mission_cap=6, commander=None,
                  chats=None) -> Toolbox:
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
        rows = leads.recent(status=status or None, limit=20)
        return [{"id": l["id"], "business": l["business"], "fit": l["fit"], "offer": l["offer"], "status": l["status"],
                 "signal": l["signal"][:160], "has_draft": bool(l["draft"]), "notes": l["notes"][:120]} for l in rows] \
            or "No leads."

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
               {"status": "(optional) new | contacted | replied | won | lost"}, list_leads))
    t.add(Tool("note_lead", "Add research notes to a lead, or dismiss it as not a fit.",
               {"id": "lead number", "notes": "what you found", "dismiss": "(optional) true to dismiss"}, note_lead))
    t.add(Tool("draft_outreach", "Write (or rewrite) the outreach email draft for a lead. It is NOT sent.",
               {"id": "lead number"}, draft_outreach))

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

    # ---- gated: leaves Wetworks or can't be undone ------------------------------
    if workspace is not None:
        def merge(pr):
            n = int(re.sub(r"\D", "", str(pr)) or 0)
            return workspace.merge_pr(n)

        t.add(Tool("merge_pull_request", "Merge a Wetworks pull request into main.",
                   {"pr": "pull request number"}, merge, gate="approve",
                   summarize=lambda a: f"Merge pull request #{a.get('pr')} into main"))
    return t
