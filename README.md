# Wetworks

Patrick's multi-agent ops center. The Architect oversees; Splinter and Sleeper cells do the work.
Runs 24/7 on the MSI (WSL2 + Docker). See [docs/architecture.md](docs/architecture.md).

## Status

- Milestone 1: The Architect sends morning and evening briefs to Discord, tracks spend against a monthly cap, and reports host health.
- Milestone 2: cell runtime and the Intel Sleeper, which searches the web for each topic in `config/intel.toml` before every brief.
- Milestone 2.1: free by default. Briefs and Intel run on a local model (Ollama) and self-hosted search (SearXNG). Paid providers are opt-in via `WETWORKS_PROVIDER`.

## First-time setup of the free stack

```
docker compose up -d --build
```

```
docker compose exec ollama ollama pull gemma4:12b
```

## Ops console (phone and desktop)

The Architect serves a private ops console on port 8700: live activity, spend and schedule,
Intel findings, and two-way comms with C.L.a.i.R and The Architect. It is only reachable over
Tailscale and every API call needs the access code from `WETWORKS_CONSOLE_TOKEN`.

Open `http://msi:8700` on any device signed in to your Tailscale, then use your browser's
"Add to Home Screen" to get an app icon.

All interfaces use the same Command API:

| Endpoint | What it does |
|---|---|
| `GET /api/status` | spend, host health, who's working, schedule |
| `GET /api/events?after=<id>` | activity feed |
| `GET /api/findings` | latest Intel findings |
| `GET/POST /api/comms/{clair,architect}` | read / send messages |
| `POST /api/actions/{intel,brief-morning,brief-evening}` | run something now |
| `GET/POST /api/missions` | list / start missions |
| `GET /api/missions/{id}` | one mission: steps, write-up, sources |
| `POST /api/missions/{id}/cancel` | cancel a queued or running mission |
| `GET /api/floor` | everything the Floor view draws |
| `POST /api/halt` | kill switch: `{"halted": true, "confirm": "HALT"}` / `{"halted": false}` |

The Floor uses the Retro Cybercity Corporate pixel-art pack, which is licensed and not in git.
Install it once on the MSI from the archive you bought, then rebuild:

```
python3 -m wetworks.tools.install_art ~/Downloads/RCCcorpoTILESETrevamped.7z
```

On a laptop or desktop the console opens on **the Floor**: an isometric view of the ops center with
every agent in its room. Click an agent for its dossier, a room for its actions, or the red
"needs you" button for approvals. The Tactical switch goes back to the list view. On a phone,
Ops starts with a small room map. The kill switch (bottom right on the Floor) stops all autonomous
work until you resume; it stays set across restarts.

Run a mission from the terminal:

```
docker compose exec architect python -m wetworks.main mission "Find three Orlando clinics that might pay for AI intake automation"
```

## Talking to C.L.a.i.R

In Comms, hold the mic button to talk, or turn on **Hands-free** and say "Hey Clair".
Say "That's all, Clair" (or stay quiet for 90 seconds) to end. She answers in an Irish voice
(Azure `en-IE-EmilyNeural`). Set `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION` in `.env`;
without them she still hears you and replies in text.

Hearing runs on two Whisper models on the MSI: a quick one (`base.en`) that only listens for
"Hey Clair", and an accurate one (`medium.en`) for what you actually say to her. "Hey Clair" only
counts at the start of what you say, so a TV or a story that mentions her won't wake her. Whisper
is told to expect Wetworks words plus your current lead names; add more in `.env`:

```
WETWORKS_VOICE_VOCAB=Sydney, Gregg, Perry, Black Desert Online, Novarad
```

The activity feed shows how long each transcription took ("Heard in 1.4s").

## Code missions

Choose **Code** in Missions (or ask C.L.a.i.R / The Architect to change Wetworks). A Splinter team
works on a fresh branch, runs the tests in an offline sandbox, and opens a pull request for you to
review. It never pushes to main and can't edit workflows, `.env` files or keys. Needs `GITHUB_TOKEN`
(fine-grained, this repo only: Contents and Pull requests read/write).

```
docker compose exec architect python -m wetworks.main code "Add a footer to the console showing the version"
```

## Leads (Revenue cell)

Every morning the Revenue cell looks for local businesses that could use your services
(`config/revenue.toml`: services, area, ideal customers, search terms) and drafts outreach for the
best fits. It never sends anything: review, copy and track leads in the console's **Leads** tab.

- **Research first.** Each strong lead gets a Splinter that researches the business (what they do, who
  decides, what software they use, recent news) before the first email is drafted. Tap "Send a Splinter"
  on any lead to research it yourself.
- **Follow-ups.** Mark a lead Contacted after you email them. If they haven't replied in 4 days, the
  morning brief reminds you and a follow-up draft is waiting in Leads (then again 7 days later, then it
  stops). Tap "I followed up" after sending, or "Snooze 3 days". Leads shows a count when any are due.

```
docker compose exec architect python -m wetworks.main run revenue
```

## Finance

Your money stays on the MSI: only the local model reads transactions, and the morning brief gets
totals at most (`config/finance.toml` sets that, plus budgets and category rules).

On your bank's or card's website, download transactions as **CSV**, then in the console open
**Finance**, type the account name, and tap **Import CSV**. Re-importing an overlapping file is
safe. Fix a category once and every charge from that merchant follows. The Bookkeeper sorts new
merchants each morning and flags bills due this week, budgets near their limit, and new recurring charges.

## Day Job

A Novarad support helper that stays on the MSI and never touches Novarad systems. In **Day Job**, paste
a ticket (patient details are stripped automatically, but leave names out when you can) and The
Technician drafts the likely cause, steps, questions and a reply, using your own notes first. Add KB
articles or notes with **Add file**; when you mark a ticket solved, what fixed it is saved for next time.

## The Observer

Learns how your day works on your own Mac and the MSI and suggests how Wetworks can fit around it.
Never run it on a work computer. Put the console access code in `~/.wetworks-observer` on each machine,
then start the helper when you want it watching (Ctrl+C stops it; Pause in the console works too).

On the Mac, from your Wetworks folder (macOS will ask to allow Terminal under Accessibility and
Screen Recording):

```
python3 observer/agent.py --server https://YOUR-MSI.ts.net --device mac
```

On the MSI, in Ubuntu:

```
python3 observer/agent.py --server http://localhost:8700 --device msi
```

What it records and what it never records is set in `config/observer.toml`. Ideas it has (brief times,
a morning prep, automations) arrive as approval cards; nothing changes without your OK.

## The Forge (training)

The Forge learns from lessons: decisions Gemini (and Claude, later) made while driving the agents.
**Drills** bank them faster: every 15 minutes from 7 AM to 11 PM, C.L.a.i.R or The Architect works a
practice request on Gemini with the real tools. Looking things up runs for real; anything that would
change something (missions, memory, messages, approvals, training) is simulated, so drills never
touch real work. They stop at 120 Gemini calls a day (the free tier is shared with real chats), when
Gemini says its limit is hit, and once 600 lessons are banked.

Run a few drills now and see the count:

```
docker compose exec architect python -m wetworks.main drill 3
```

```
docker compose exec architect python -m wetworks.main forge-status
```

```
docker compose exec architect python -m wetworks.main forge-dataset
```

```
docker compose exec architect python -m wetworks.main forge-train agent --now
```

```
docker compose exec architect python -m wetworks.main forge-eval gemma4:12b
```

## Intel topics

Edit `config/intel.toml` to add, change or pause topics. It's re-read on every run, so no rebuild is needed.
Run Intel on demand:

```
docker compose exec architect python -m wetworks.main run intel
```

## Run on the MSI (Ubuntu in WSL)

```
cd ~/Wetworks && git pull
```

```
cp .env.example .env && nano .env
```

Fill in `ANTHROPIC_API_KEY` and `WETWORKS_DISCORD_WEBHOOK_URL`.

```
docker compose up -d --build
```

```
docker compose logs -f architect
```

Send a brief right now to test:

```
docker compose exec architect python -m wetworks.main brief morning
```

## Develop on the Mac

```
python3 -m venv .venv && source .venv/bin/activate
```

```
pip install -e '.[dev]'
```

```
pytest -q
```

```
WETWORKS_DRY_RUN=true WETWORKS_DATA_DIR=./data python -m wetworks.main brief morning
```

## Layout

```
wetworks/
  config.py     settings from env / .env
  ledger.py     SQLite spend ledger and model prices
  llm.py        budget-aware model client (route / work / think tiers)
  notify.py     Discord webhook (more destinations later)
  health.py     host health snapshot
  architect.py  The Architect: briefs
  findings.py   shared findings store
  operators.py  Sleeper Operator (schedules cells)
  cells/        base Cell, Intel (Sleeper), Splinter (missions)
  events.py     activity log + who's working
  comms.py      chat with C.L.a.i.R and The Architect (can open missions)
  missions.py   mission queue
  api.py        Command API + console server
  console/      the ops console (HTML/CSS/JS, no build step)
  code/         Code cell: git workspace + pull requests, coding brains
sandbox/        offline test sandbox for code missions
config/
  intel.toml    Intel topics
  main.py       scheduler entry point
```
