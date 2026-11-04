# The Wetworks Handbook

The complete story of how Wetworks was built, how every part works, the rules it runs by, and how to
operate and change it. Written for The Architect (and any agent or person who works on Wetworks) so
nothing from the build sessions is lost. Companion files: `docs/architecture.md` (milestone-by-milestone
technical record), `README.md` (operator's guide), `docs/art-spec.md` (image-AI art spec), and
`config/knowledge/wetworks-handbook.md` (the same knowledge as short facts loaded into memory).

Last updated: November 4, 2026 (after the Content room and The Architect-on-Claude milestones).

---

## 1. What Wetworks is for

Wetworks is Patrick Caldwell's personal multi-agent ops center: a set of AI agents that run 24/7 on one
laptop and work on his behalf. It exists to make his life more efficient across, in priority order:

1. **Make money** (CodeVoyage freelance work: leads, outreach, research)
2. **Make the day job easier** (Novarad technical support)
3. **Manage finances**
4. **Free up time**
5. **Build games**
6. **Create content**

The long-term goal is bigger than a set of helpers: every agent keeps learning, and the endgame is
Patrick's own AI model hosting platform to develop, train, deploy and use custom LLMs around the clock.
The Forge is the start of that.

The theme is a Splinter Cell-style ops center: a commander, field teams, sleeper agents, sealed rooms
that open as new cells are built, and a gritty pixel-art map of it all.

### Who Patrick is (as far as the work needs)

- Software and AI developer in Sanford / Lake Mary, Florida. Full Sail University (B.S. Computer Science
  with an AI concentration; B.S. Game Design). USMC veteran. Data analyst background.
- Owns CodeVoyage LLC, a freelance AI and software consultancy for small and local businesses and startups:
  AI chatbots, workflow automation, analytics dashboards, AI-adoption consulting. Built SummitFlow, a
  climbing-gym SaaS (now closed).
- Works in technical support for Novarad medical imaging software (NovaPACS, Nova RIS).
- Develops on a Mac; Claude is his main daily development tool in a multi-model workflow.
- How he likes to work: precision over breadth; fix root causes, not symptoms; terminal commands one per
  code block; free models wherever they do the job; visual results he can react to; when he says he likes
  something as it is, keep it and fix only what he asked.

---

## 2. The rules Wetworks runs by

These were set by Patrick during the build and are not optional.

### Autonomy
- **Agents act freely inside Wetworks**: research, open missions, take notes, draft, leave follow-ups,
  reorganise their own work.
- **Anything that leaves Wetworks or can't be undone needs Patrick's approval** in the console's
  *Needs you* panel (the red phone on the Floor rings): sending anything to other people, posting,
  merging code, spending money, switching every agent's model.
- **Wetworks never contacts anyone as Patrick.** Outreach emails, follow-ups, social posts and ticket
  replies are drafts. Patrick copies and sends or posts them himself.

### Code
- The Code cell works only through pull requests on branches named `wetworks/mission-<id>`.
- It never pushes to `main` and can't edit `.git/`, `.github/` (workflows run with repo secrets),
  `.env*` (except `.env.example`), keys or certificates.
- Merging needs Patrick's approval. After a merge the MSI does **not** update itself: Patrick runs
  `git pull` and `docker compose up -d --build`. Deliberate: Wetworks must never restart itself onto
  broken code.
- Console or code changes always need `docker compose up -d --build` (rebuild), not `restart`.

### Money
- Build week (Nov 3, 2026): $20 AI spending limit; free models by default; small paid tests allowed.
- Every paid call goes through the ledger. At 80% of the monthly budget the brief warns; at 100%
  non-essential calls are refused and essential ones drop to the cheapest tier.
- Claude Fable and GPT-6 Astra only with Patrick's approval (expensive).
- Design principle: get the most out of every token. Cheapest model that can do the job; escalate only when needed.

### Privacy
| Area | Who can read details | What the brief / Discord gets | Cloud agents |
|---|---|---|---|
| Finance | Console; agents on the local model | Totals at most (`brief = "totals"`), or counts, or off | Nothing unless `share_with_cloud = true` |
| Day Job | Console; agents on the local model (titles and snippets) | Counts only | Never |
| The Observer | Console; agents on the local model | Yesterday's hours and number of focus blocks | Never |
| Content | Anyone (titles only for agents) | Count of drafts ready | Can list and add ideas; drafting stays local |
| Drills (on Gemini) | Fake sample data for every private room | n/a | n/a |

- Patient details are scrubbed before anything Day Job stores or shows a model (labelled fields, DICOM
  `LAST^FIRST` names, "Patient First Last", SSNs, phones, emails, DICOM UIDs, 7+ digit IDs, Mr./Dr. names).
  Hosts, IPs and KB numbers are kept. The scrubber is pattern-based: a safety net, not a guarantee.
- The Observer watches sensitive screens (money, passwords, messages, Novarad and patient screens) so it
  can learn those workflows, but keeps no window titles for them and only task-level descriptions with
  amounts, any 3+ digit number, names, emails, quoted text and patient identifiers stripped. Screenshots
  are never written to disk. Patrick has employer approval to run it on his work PC (not set up yet).
- The licensed pixel-art pack is never committed or uploaded to an image AI.

---

## 3. The hardware and how it's reached

- **MSI Raider GE78HX** laptop: RTX 4070 Laptop GPU (8GB VRAM), 32GB RAM, Windows 11, set up to run
  24/7 (power settings changed; Windows Hello PIN-only sign-in caused a schtasks password issue that
  Patrick resolved). Black Desert Online is the only other software Patrick keeps on it, and only for AFK
  play minimized, so the GPU is effectively Wetworks'.
- **WSL2 Ubuntu 24.04** with systemd, `networkingMode=mirrored` in `C:\Users\pchan\.wslconfig`. Windows
  user `pchan`, Linux user `savage`. Repo at `~/Wetworks`.
- **Docker Engine inside WSL** (not Docker Desktop), NVIDIA container toolkit for GPU passthrough.
- **Tailscale** on the MSI and Patrick's phone and Mac. `tailscale serve --bg 8700` gives the console an
  https `*.ts.net` address (an http/https mismatch caused "sent invalid response" on the phone until this
  was used). Every API call needs the bearer access code `WETWORKS_CONSOLE_TOKEN`.
- **Mac**: development machine. Changes are pushed to GitHub (`chandlerpc/Wetworks`, private) and pulled
  on the MSI.

---

## 4. The system map

```
Patrick ── console (desktop Floor / phone tabs) ── voice ── Discord (briefs only)
   │
   ├── C.L.a.i.R (Comms Booth) ── Gemini via her Cloudflare Worker, local backup
   │        └── tell_architect ─┐
   └── The Architect (Command Deck) ── Claude Opus (test) or local
            ├── Splinter Operator ── Splinters (research and code missions, one at a time)
            ├── Sleeper Operator ── Intel, Revenue, The Archivist, The Bookkeeper, The Technician,
            │                      The Writer, The Observer (nightly)
            └── The Forge ── traces → datasets → fine-tuning → local models; drills
```

### Docker Compose services
| Service | What it does |
|---|---|
| `architect` | The Python app: API and console on port 8700, scheduler, every agent and cell |
| `ollama` | Local models on the GPU; `gemma4:12b` kept loaded (`OLLAMA_KEEP_ALIVE=-1`) |
| `searxng` | Self-hosted web search (free) |
| `whisper` | faster-whisper `medium.en` on CPU: hears requests |
| `whisper-fast` | faster-whisper `base.en` on CPU: listens only for "Hey Clair" |
| `sandbox` | Runs code-mission tests: internal-only network, read-only, no secrets, capped resources |
| `forge` | Unsloth fine-tuning on the GPU; job runner on port 8900; hands `/forge` to uid 1000 on start |

### Data files (`/data` volume)
`ledger.db` (spend), `events.db` (activity feed), `findings.db`, `comms.db` (chat channels),
`missions.db`, `leads.db`, `agents.db` (approvals and follow-ups), `memory.db`, `chats.db` (imported past
chats), `traces.db` (every agent decision, plus drills), `finance.db`, `dayjob.db`, `observer.db`,
`content.db`, `forge/` (datasets, jobs, `forge.db`, `active_model.txt`), `voice/` (last few recordings for
debugging), `workspaces/` and `sandbox-runs/` (code missions), `HALTED` (kill switch), `overrides.json`
(console-changed settings).

### Repository layout
- `wetworks/main.py`: builds everything (`build()`), the scheduler, the CLI commands.
- `wetworks/api.py`: every HTTP route. `wetworks/floor.py`: the Floor snapshot. `wetworks/halt.py`: kill switch.
- `wetworks/agent/`: `runtime.py` (step loop), `toolkit.py` (tools), `commander.py` (The Architect),
  `clair.py`, `brains.py`, `research.py` (URL-tracked research tools), `state.py` (approvals, follow-ups),
  `traces.py`, `drills.py`.
- `wetworks/cells/`: `intel.py`, `revenue.py`, `splinter.py`, `coder.py`, `learner.py`, `finance.py`,
  `dayjob.py`, `content.py`, `observer.py`, `base.py`.
- Stores: `finance.py`, `dayjob.py`, `content.py`, `observer.py`, `leads.py`, `missions.py`, `memory.py`,
  `knowledge.py`, `findings.py`, `events.py`, `ledger.py`, `forge.py`.
- Models: `llm.py` (Claude, Grok, budget), `local.py` (Ollama incl. image description, SearXNG),
  `bridge.py` (C.L.a.i.R's Worker), `voice.py` (Whisper, Azure TTS).
- `wetworks/code/`: Code cell brain and git workspace. `wetworks/tools/`: `install_art.py`, `un7z.py`.
- `wetworks/console/`: `index.html`, `console.js`, `console.css`, `floor.js` (pixel Floor), `voice.js`, `art/`.
- `config/`: `profile.md`, `knowledge/`, `intel.toml`, `revenue.toml`, `finance.toml`, `observer.toml`,
  `content.toml`, `searxng/`, `imports/` (git-ignored).
- `forge/` (runner, trainer, Dockerfile), `sandbox/`, `observer/` (helpers), `docs/`, `tests/`.

---

## 5. Brains and models

| Who | Brain | Setting |
|---|---|---|
| The Architect | Claude Opus 5.5 (since Nov 4 test) or local | `WETWORKS_ARCHITECT_BRAIN` (falls back to `AGENT_BRAIN`) |
| Cells, Splinter research | Local `gemma4:12b` | `WETWORKS_AGENT_BRAIN` |
| Code cell | Local (weak at code) or Claude Sonnet | `WETWORKS_CODE_BRAIN` (`claude-code` on Max planned Friday) |
| C.L.a.i.R | Gemini 2.5 Flash via her Worker's step mode; local backup | `WETWORKS_CLAIR_URL` / `_TOKEN` |
| Briefs, Intel provider | Local (Ollama + SearXNG) | `WETWORKS_PROVIDER` (`claude`, `grok` for paid tests) |
| Finance, Day Job, Observer, Content drafting | Local only, always | hard-coded |

Claude tiers: route `claude-haiku-5-5`, work `claude-sonnet-5-5`, think `claude-opus-5-5`. Ledger prices
per million tokens: Opus $4 / $20, Sonnet $2 / $10, Haiku $0.10 / $0.50. A deep Architect chat answer on
Opus costs roughly $0.30 to $0.90; his scheduled check-ins every 2 hours cost roughly $3 to $5 a day.

On Claude, The Architect gets 14 steps per cycle, 10 per chat reply, 3,500 tokens per step, and keeps
working while The Forge trains. On the local model: 8 per cycle, 5 per chat, 900 tokens per step.

Local model notes: Ollama's `think` is switched off (a thinking model once spent its whole token budget
reasoning and returned "(no reply)"); an empty answer is retried once with more room.

---

## 6. Every agent and room

### The Architect (Command Deck)
Plans, delegates, reviews and reports; never does bulk work himself. **Check-ins** every 2 hours,
08:00 to 22:00, plus two minutes after any mission finishes: review findings, leads, missions, follow-ups
and memory against the priorities; open at most 2 missions per cycle (3 open at once, 6 per day); leave
follow-ups; report to the activity feed; Discord only for what needs action today. **Chat**: Patrick's
Architect channel runs him as an agent with the same tools. His persona: decisive but economical, every
mission uses the MSI's GPU, never duplicate a mission, external or irreversible things need approval.

### C.L.a.i.R (Comms Booth)
Patrick's assistant and comms handler, a mint-green flame wisp on the Floor. Originally his public AI
recruiter on his portfolio site (clAIr, renamed C.L.a.i.R in Wetworks). Lives in `clair-worker.js` in the
`chandlerpc/portfolio` repo on Cloudflare (`clair.pchandlerc.workers.dev`), with a private `POST /wetworks`
route (needs `WETWORKS_BRIDGE_TOKEN`) that supports chat and `mode: "step"` (one agent decision per call).
Short, warm replies (often voice); starts missions for real work; hands strategy to The Architect with
`tell_architect`; never sends anything outside Wetworks. Setup lessons: the URL must include `/wetworks`
(a 403 came from a missing path and stale Worker code); her context states spend explicitly (she once
said $0 when it was $0.43).

**Voice.** Hold-to-talk mic and hands-free mode: wake phrase "hey clair" (must open the utterance; checked
by the fast model), stop phrases ("that's all clair", "goodbye clair", "that's all", "goodbye"), 90 s idle
timeout. Whisper hears with a vocabulary hint (house words, current lead names, `WETWORKS_VOICE_VOCAB`);
transcripts drop silence hallucinations ("Thank you.", "[BLANK_AUDIO]") and fix house words. She answers in
Azure `en-IE-EmilyNeural`, voiced in two parts so the first sentence plays sooner; free tier 500k chars a
month, stopped at 480k. Lessons: the Azure key was rejected (401) because placeholder `<>` brackets were
pasted in (settings now strip brackets and quotes); the wake phrase once matched inside other words
(now word boundaries); a hold-to-talk race was fixed with a pressing flag, warm mic, tick and 250 ms tail.
Browsers only allow the mic while the console is on screen.

### Splinters (Splinter Ready Room, Code Bay)
One mission at a time through the Splinter Operator. Research missions: plan, search SearXNG, read top
pages (public addresses only, redirects checked), write up with numbered citations and a "Next step:"
line; sources only from real results. Code missions (Code Bay): see section 2 and `docs/architecture.md`
Milestone 7. Missions report back into the channel that opened them; lead-research missions file their
write-up onto the lead instead.

### Intel (Intel Watch) and Revenue (Revenue Room)
Sleepers. Intel runs 20 min before each brief over `config/intel.toml` topics (AI models & tools, money
opportunities, job market, Black Desert Online; add topics freely). Revenue runs 45 min before the morning
brief, rotates `config/revenue.toml` search terms for Sanford, Lake Mary and greater Orlando, scores fit
1 to 5 against CodeVoyage's services, drafts outreach for 4+. Both run as agents with URL-tracked research
tools and fall back to fixed routines if the model doesn't search. **Lead follow-ups**: strong new leads
get a research Splinter first (two a morning; first drafts wait for it); marking a lead Contacted schedules
follow-ups after 4 then 7 days with drafted follow-ups and brief reminders; replies or closing stop them.

### The Archivist (Archives)
The Learner, 23:30 nightly: extracts durable facts Patrick stated in the day's chats and missions (skipping
temporary and sensitive things) into memory; archives mission details older than a year with a heads-up the
week before.

### The Observer (Archives)
Learns how Patrick's day works on his own machines. Helpers: `observer/agent.py` (Mac via osascript and
screencapture, needs Accessibility and Screen Recording permission; the MSI from WSL through powershell.exe),
`observer/agent.ps1` (Windows with nothing installed). Every 5 to 10 s: foreground app, window title, idle;
about once a minute while active: a screenshot the local vision model describes in one sentence, then
discards. Pause and Record from the console take effect within 15 s. Day view (active time, apps, focus
blocks of 25+ min, switches, what was on screen), 14-day habits (usual start and end, morning routine,
daily apps), nightly digest at 23:15, pruning after `keep_days` (14). After about 5 days of data it
suggests changes as approval cards (`adopt_observation`): brief times (saved to `overrides.json`, applied
on restart), a morning-prep mission, up to two automation ideas. **Teach a task**: Record on a chosen
computer, do the task once, Stop; the local model writes a playbook; "Automate this" opens a mission.
Patrick ran the helper on the PC first and it worked.

### The Forge (The Forge room, Forge Operator)
Section 7.

### The Bookkeeper (Finance)
Bank or card CSV import from the console (finds the header row; date, description and amount or
debit/credit columns; flips card exports where purchases are positive; masks 6+ digit runs; dedupes on
re-import; undo per import). Categories: `config/finance.toml` rules (most specific first), Patrick's
corrections (applied to every charge from that merchant), then the local model for the rest. Recurring
charges (steady cadence and amount; everyday categories need near-identical amounts), due in the next
30 days, budgets per category. Runs 20 min before the morning brief. Budgets in the config were placeholders
for Patrick to set.

### The Technician (Day Job)
Novarad support helper. A library of Patrick's notes, KB files (text, Markdown, HTML, CSV, email, PDF via
pypdf) and fixes with full-text search; tickets (open, waiting, solved). The local model drafts summary,
likely cause, steps, questions to ask and a reply, preferring his own fixes and citing them; solving a
ticket saves the resolution as a fix. Never connects to Novarad systems. Brief gets counts only.

### The Writer (Content)
Suggests post ideas grounded in the week's finished missions, important Intel findings and Observer digests;
drafts the oldest idea for LinkedIn, X, blog and portfolio in Patrick's voice (`config/content.toml`:
pillars, cadence, voice, `never_mention`). Local model only. Calendar of planned posts and open slots.
Ideas or drafts touching work, patients, money or family are dropped or flagged. Wetworks never posts.

### Game Dev (sealed)
Opens after Claude is connected (Friday): design docs and prototype code need a stronger model.

---

## 7. The Forge and the learning loop

- **Traces**: every agent decision is recorded with the brain that made it (`local`, `gemini`, `claude`).
  Teacher lessons are Gemini and Claude decisions that finish or call a tool that was actually offered.
- **Dataset** `agent-steps`: chat-format JSONL from teacher lessons, 10% held out; needs at least 150.
- **Training**: Unsloth QLoRA on `unsloth/Qwen3-4B-Instruct-2507`, jobs in the 01:00 to 05:00 window unless
  started now; before a job Wetworks unloads Ollama and pauses local thinking (C.L.a.i.R keeps going on
  Gemini; The Architect on Claude keeps going); output GGUF q4_k_m registered in Ollama as
  `wetworks-<name>:<id>`; then the local model reloads.
- **Benchmarks**: any local model scored on held-out lessons (valid step, same action, same tool).
- **Switching** the agents' local model is approval-gated for The Architect.
- **Drills**: every 15 min from 07:00 to 23:00 an agent works a practice request on Gemini with the real
  tool list (curated requests in Patrick's voice, templates with real lead names and mission numbers, full
  command cycles). Read-only tools are real; anything that changes state is simulated; private rooms give
  fake samples. Limits: 120 Gemini calls a day, pause until tomorrow on a 429, stop at 600 lessons.
  The first live run banked 8 lessons from 3 drills.
- **Setup lessons**: the forge container originally created `/forge` as root and the architect (uid 1000)
  crashed with `PermissionError` on `/data/forge/datasets`; the forge container now chowns the folder on
  start and writes with an open umask. GPU memory shows about 7.5 of 8 GB used: that's gemma kept loaded.

---

## 8. The console and the Floor

- **Desktop (≥1000 px)**: opens on the Floor, a top-down 3/4 pixel-art map (Retro Cybercity Corporate
  tileset, installed with `python3 -m wetworks.tools.install_art <archive.7z>`; on the MSI the archive is in
  the Windows Downloads folder at `/mnt/c/Users/pchan/Downloads`). Rooms: Intel Watch, Revenue Room,
  Splinter Ready Room (top); Archives, Command Deck, Comms Booth, Code Bay (middle); The Forge, Day Job,
  Finance, Game Dev (sealed), Content (bottom). Agents walk between home, station and bunk; activity pings;
  lamps brighten when busy and pulse red on a blocked agent; the red phone rings for approvals; Forge glow
  and sparks follow GPU load; darkness and lighting pass; Look panel (colour grades Original, Night ops,
  Night vision, Amber noir, Neon, Ghost; darkness and glow sliders) per device. Click agents for dossiers
  (Splinters show their mission timeline), rooms for actions. HUD: spend, missions, Forge lessons bar,
  approvals, host and GPU health, radio log, guarded kill switch. Characters are stand-ins drawn in the
  pack's style until the image-AI sprites from `docs/art-spec.md` arrive.
- **Tactical view / phone**: tabs (scrollable on phone): Ops (room map, approvals, spend, activity newest
  first, memory), Missions, Comms (C.L.a.i.R and The Architect, voice), Intel, Leads, Finance, Day Job,
  Content, Observer.
- UI lessons: the Ops activity panel once hid behind the tab bar; Patrick wanted only the scroll fixed, not
  the layout changed. Static files are served no-cache and versioned, but a rebuild is still needed.

---

## 9. Schedule (Eastern time)

| When | What |
|---|---|
| Every 2 h, 08:10 to 22:10 | The Architect's check-in (and 2 min after any mission finishes) |
| 06:15 | Revenue hunt (45 min before the morning brief) |
| 06:30 | The Writer (30 min before) |
| 06:40 | Intel (20 min before each brief); The Bookkeeper (20 min before) |
| 06:45 | The Technician (15 min before) |
| 06:50 | The Observer's morning numbers (10 min before) |
| 07:00 / 21:00 | Morning and evening briefs to Discord |
| 20:40 | Intel before the evening brief |
| 23:15 | The Observer's digest, pruning and suggestions |
| 23:30 | The Archivist (Learner) |
| Every 15 min, 07:00 to 23:00 | Forge drills (until limits) |
| Every 5 min | Forge tick (queued jobs in the 01:00 to 05:00 window) |

---

## 10. Operating Wetworks

Commands are run in Ubuntu on the MSI from `~/Wetworks`, one per block:

```
git pull
```
```
docker compose up -d --build
```
```
docker compose ps
```
```
docker compose logs --tail 60 architect
```
```
docker compose exec architect python -m wetworks.main forge-status
```
```
docker compose exec architect python -m wetworks.main drill 3
```
```
docker compose exec architect python -m wetworks.main cycle
```
```
docker compose exec architect python -m wetworks.main run revenue
```
```
docker compose exec architect python -m wetworks.main mission "what to research"
```
```
docker compose exec architect python -m wetworks.main import-chats config/imports/export.zip
```
```
python3 -m wetworks.tools.install_art /mnt/c/Users/pchan/Downloads/RCCcorpoTILESETrevamped.7z
```
```
python3 observer/agent.py --server http://localhost:8700 --device msi
```

Other CLI commands: `brief morning|evening`, `code "<request>"`, `forge-dataset`,
`forge-train <name> [dataset] [--now]`, `forge-eval <model>`.

**Troubleshooting lessons from the build**
- `cd Wetworks` failing or "no such file .env": wrong folder or not yet cloned; the repo is `~/Wetworks`.
- A container restart loop: read `docker compose logs --tail 60 <service>`; the Forge permission crash was found this way.
- GitHub reports "This repository moved" on push; harmless (the remote is `chandlerpc/Wetworks.git`).
- Files the user downloads in Windows are under `/mnt/c/Users/pchan/Downloads` from Ubuntu.
- The browser on the MSI couldn't open GitHub sign-in during setup; Patrick logged in manually.

---

## 11. Changing Wetworks

- Small changes: a code mission (Code Bay) produces a pull request for Patrick to approve. With the local
  code brain it is weak; set `WETWORKS_CODE_BRAIN=claude-api` (Sonnet) or wait for `claude-code` on Max.
- Larger work so far was done by Claude in build sessions on Patrick's behalf, committed to `main` as
  "Patrick Caldwell" with co-author attribution, tested with pytest (169 tests at this point), and
  previewed with screenshots before shipping.
- Conventions: every new room gets a store module, a cell, API routes, a console pane, a Floor room and
  agent, tests (especially privacy tests: what cloud brains and the brief can and can't see), and entries in
  `docs/architecture.md`, `README.md` and this handbook. New settings go in `config.py` with comments and in
  `.env.example`. Private data defaults to local-only.
- TOML gotcha: keys after a `[section.sub]` header belong to that sub-table; put sub-tables last (this once
  hid Content's `never_mention` list).
- Python gotcha: don't name a method `list` in a class that also uses `list[...]` type hints.

---

## 12. Timeline of the build (Nov 2026)

1. MSI wiped (Windows reset, backups confirmed); WSL2 Ubuntu and Docker Engine chosen over Docker Desktop;
   `.wslconfig` created via the device bridge (user folder `pchan`).
2. Milestone 1: The Architect in Docker sending Discord briefs with a spend ledger and budget cap.
3. Intel as the first cell; then "free by default" (Ollama + SearXNG) after Patrick capped spending.
4. Command API and phone/desktop console over Tailscale; Missions and the Splinter Operator.
5. C.L.a.i.R's bridge to her Cloudflare Worker; her voice (Whisper + Azure Emily).
6. Code cell framework; Revenue cell; Memory; the agentic core; everything agentic.
7. Knowledge loading and The Forge; Floor UI (isometric), lead follow-ups and research, Forge drills,
   voice tuning (fast wake model), Floor polish.
8. Floor rebuilt as top-down 3/4 pixel art on the Retro Cybercity pack with lighting and a Look panel.
9. Rooms unsealed: Finance (The Bookkeeper), Day Job (The Technician), Content (The Writer); The Observer
   added (watch sensitive screens, keep no details; PowerShell helper for the work PC).
10. The Architect switched to Claude Opus 5.5 for a capability test (Nov 4); Patrick: "he works great so far".

---

## 13. Open items and what's next

- **Friday (Nov 6)**: Claude Max; connect Claude to the agents and the Code cell (`claude-code`); unseal Game Dev.
- Image-AI characters and ops props (GPU racks, lockers, radar, beds, blast doors) per `docs/art-spec.md`.
- First real Forge training run once 150 teacher lessons are banked; benchmark against gemma.
- Work-PC Observer setup (approved, waiting); Tailscale on the work PC needs IT's OK too.
- Past-chat import from Patrick's new Claude account.
- Known gaps: The Architect has no pull-request lookup tool; brief-time changes apply on restart; Observer
  descriptions depend on the local model reading images (switch to a vision model if "On your screen" stays empty).
- Ideas discussed: an approval-gated deploy step; finance sharing with Claude if Patrick chooses.

## 14. Glossary

- **Splinter**: on-demand agent for one mission. **Sleeper**: background agent on a schedule or trigger.
- **Cell**: a unit of work (Intel, Revenue, Code...). **Room**: a cell's place on the Floor; sealed rooms are future cells.
- **Brief**: the morning and evening Discord summary. **Finding**: something a cell reports to the brief.
- **Teacher lesson**: a recorded decision by Gemini or Claude used to train the local model.
- **Drill**: a practice run that banks lessons without changing anything.
- **Needs you / red phone**: the approvals queue.
