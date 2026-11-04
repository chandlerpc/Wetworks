# Wetworks — Architecture

Wetworks is a multi-agent system that runs 24/7 on the MSI (Windows 11 → WSL2 Ubuntu 24.04 → Docker Engine, GPU passthrough). Development happens on the Mac; the MSI pulls and runs.

## Hierarchy

```
                    Patrick (phone / Discord / voice via C.L.a.i.R)
                                   │
                         ┌─────────┴─────────┐
                         │      C.L.a.i.R        │  comms layer (Cloudflare Worker)
                         └─────────┬─────────┘
                                   │
                         ┌─────────┴─────────┐
                         │  The Architect    │  overseer — Claude Opus
                         └──┬──────┬──────┬──┘
              Splinter Operator    │    Sleeper Operator
              (on-demand)          │    (scheduled / triggered)
                 │                 │          │
          Splinter 001…      The Forge    Sleeper 001…
                              (local GPU)
```

- **The Architect** plans, delegates, reviews and reports. It never does bulk work itself.
- **Splinters** are on-demand strike teams for a single mission.
- **Sleepers** are background agents that wake on schedules or triggers.
- **C.L.a.i.R** stays on Cloudflare and handles comms so The Architect stays on task.

## Launch cells

| Cell | Type | Default model | Mission |
|---|---|---|---|
| Revenue | Sleeper | Sonnet | Find and track money-making opportunities |
| Code | Splinter | Opus / Sonnet | Build and review code |
| Intel | Sleeper | Grok | Research and current-events monitoring |
| The Forge | Splinter | Local models (GPU) | Local model lab, fine-tuning experiments |

New cells run a 2-week trial before they are made permanent.

## Model routing (token efficiency)

Use the cheapest model that can do the job; escalate only when needed.

| Tier | Model | Use for |
|---|---|---|
| Route | `claude-haiku-5-5` | Classification, summarising logs, formatting |
| Work | `claude-sonnet-5-5` | Briefs, drafting, most cell work |
| Think | `claude-opus-5-5` | The Architect's planning and reviews |
| Approval only | Claude Fable, GPT-6 Astra | Never called without Patrick's approval |

Other vendors (Gemini, Grok, OpenAI) plug in behind the same `LLM` interface later. Cross-vendor review: work from one vendor's model is reviewed by another's.

## Budget

- Monthly cap: `$250` (configurable via `WETWORKS_MONTHLY_BUDGET_USD`).
- Every call is logged to a SQLite ledger with tokens and cost.
- At 80% of budget, the brief warns. At 100%, all non-essential calls stop and only briefs keep running on the cheapest tier.

## Milestone 1 (this commit)

- The Architect service in Docker, `restart: unless-stopped`.
- Morning and evening briefs to Discord, Eastern time.
- Spend ledger and monthly cap.
- Host health in each brief (disk, memory, uptime).
- Dry-run mode for testing without API keys.

## Milestone 2: cell runtime + Intel

- `wetworks/cells/base.py`: base `Cell` (Sleeper or Splinter, 2-week trial tracking, crash-safe runs).
- `wetworks/operators.py`: Sleeper Operator schedules cells; Intel wakes 20 min before each brief.
- `wetworks/findings.py`: shared findings store; URLs are de-duplicated for 14 days.
- `wetworks/cells/intel.py`: one web-search pass per topic in `config/intel.toml`.
  Grok (web + X search) when `XAI_API_KEY` is set, Claude web search otherwise, with automatic fallback.
- Briefs now include an Intel section grouped by topic (🔴 act today, 🟠 act this week).
- Paid search estimate (Claude/Grok): about $0.30–0.40 per Intel run, roughly $20/month.

## Milestone 2.1: free by default

- `WETWORKS_PROVIDER=local` (default): briefs and Intel run on **Ollama** (local model on the
  MSI's GPU, default `gemma4:12b`) and **SearXNG** (self-hosted search). Cost: $0.
- Intel's local flow: SearXNG runs each topic's `queries`, already-reported URLs are dropped,
  and the local model picks and summarises the best results by index, so URLs always come
  from real search results, never from the model.
- `claude` and `grok` providers stay available for short paid tests.
- Build-phase budget cap: $5/month in `.env.example`. Raise it once everything is proven.
- Ollama keeps the model loaded in GPU memory permanently (the GPU is dedicated to Wetworks), so runs skip the cold start.

## Milestone 3: Command API and ops console

- One private API (`wetworks/api.py`, port 8700) that every interface uses: the phone console
  now, the full ops-center UI and C.L.a.i.R's bridge later.
- Access: Tailscale only (WSL mirrored networking + a firewall rule limited to port 8700),
  plus a bearer access code. The API refuses everything when no code is configured.
- Console: night-vision HUD styling. Tri-lens status light per agent (The Architect, C.L.a.i.R,
  Intel) glows while that agent is working. Tabs on phone; three panes side by side on desktop.
- Comms: C.L.a.i.R and The Architect each have a persona and answer on the local model with live
  status and recent findings as context. C.L.a.i.R runs locally here until the bridge to her
  Cloudflare Worker is built, at which point this channel is routed to her.

## Milestone 4: Splinter Operator and missions

- `wetworks/missions.py`: mission queue (queued, running, done, failed, cancelled) with step log,
  crash recovery (running missions go back to the queue after a restart) and cancellation.
- `wetworks/operators.py` `SplinterOperator`: one worker thread, one mission at a time, so the GPU
  isn't split. Posts to Discord and back into the chat channel that asked when a mission finishes.
- `wetworks/cells/splinter.py`: plan → research (SearXNG + reading the top pages) → write-up with
  numbered citations and a "Next step:" line. Sources are taken from real search results only.
- Page reading is limited to public internet addresses (no Docker network, LAN, Tailscale or
  metadata endpoints) and checks every redirect.
- Chat: C.L.a.i.R and The Architect reply in JSON and can open a mission when Patrick asks for work.
- Console: Missions tab (phone) / under Ops (desktop), new-mission box, live status, detail sheet
  with the write-up, sources and how it was done.

## Milestone 5: C.L.a.i.R bridge

- C.L.a.i.R's Cloudflare Worker (chandlerpc/portfolio, `clair-worker.js`) gained a private
  `POST /wetworks` route next to her public recruiter mode. It needs the
  `WETWORKS_BRIDGE_TOKEN` secret, uses an assistant persona, and returns `{reply, mission}`.
- `wetworks/bridge.py`: the console's C.L.a.i.R channel calls that route with the conversation
  and live status. Gemini 2.5 Flash free tier, so $0. If Cloudflare can't be reached, local
  C.L.a.i.R answers and the console says so.
- The Architect's channel always stays local.
- Her private mode is a self-contained block, so it can be rebuilt (new model, voice, tools)
  without touching the public recruiter.

## Milestone 6: C.L.a.i.R's voice

- Hearing: `whisper` container (faster-whisper `medium.en`) on the CPU, so the MSI's 8GB RTX 4070
  stays dedicated to the local model. Free and private.
- Speaking: Azure neural TTS, `en-IE-EmilyNeural` (young Irish female). Free tier 500k
  characters/month; `tts_monthly_char_cap` (480k) stops speech before that and falls back to text.
  The Azure key stays on the MSI; the phone only receives MP3 audio.
- Console (`console/voice.js`): hold-to-talk mic, and hands-free mode. Wake phrase
  ("hey clair"), stop phrases ("that's all, clair", "goodbye, clair"…), idle timeout (90 s).
  Voice activity detection runs in the browser; each utterance is transcribed on the MSI.
- Limit: browsers only allow the microphone while the console is on screen. Always-on
  "Hey Clair" with the screen locked needs a native app.

## Milestone 7: Code cell (framework)

- Code missions (kind `code`) run in `wetworks/cells/coder.py`: fresh branch
  `wetworks/mission-<id>` from main → brain picks files and proposes full-file edits → edits are
  staged → a copy of the files (no `.git`) runs its tests in the sandbox → one repair round →
  commit only the staged files → push the branch → open a pull request (draft if tests fail).
  Nothing merges automatically.
- Safety:
  - never pushes to main; blocks `.git/`, `.github/` (workflows run on push with repo secrets),
    `.env*` (except `.env.example`), keys and certificates;
  - git hooks and fsmonitor are always disabled; the GitHub token is sent as a per-command
    header and never written into the clone;
  - the `sandbox` container has no secrets, sits on an internal-only Docker network (no internet),
    runs read-only with no extra privileges, capped memory and processes, and only sees a copy.
- Brains (`wetworks/code/brain.py`): `local` (free, weak at coding) and `claude-api` (paid,
  inside the monthly cap). Friday: `claude-code` on the Max plan.
- Chat: clAIr and The Architect can open code missions; the console has a Research / Code switch.

## Milestone 8: Revenue cell

- `wetworks/cells/revenue.py`: Sleeper, once a day 45 minutes before the morning brief.
  Rotates through search terms in `config/revenue.toml`, has the local model pick real, local,
  independent businesses with a sign of need, scores fit (1-5) and matches a service
  (Chatbot / Automation / Dashboard). Drafts a short outreach email for fit ≥ 4.
- Never contacts anyone. Leads and drafts wait in the console's Leads tab; Patrick sets the
  status (new → contacted → replied → won/lost) and adds notes.
- Strong leads appear in the brief under "Revenue leads"; the pipeline summary is in the context
  C.L.a.i.R and The Architect see.
- Job-board pages are kept as evidence but never used to identify a business, so different
  businesses posting on the same job board aren't merged.

## Milestone 9: Memory

- `wetworks/memory.py`: SQLite with full-text search (no model, no GPU).
- `config/profile.md`: who Patrick is, his business, skills and preferences. Loaded on start as
  pinned memories that every agent sees.
- Chat: C.L.a.i.R and The Architect recall memories related to each message, and can add a
  "remember" item when Patrick states something durable or asks them to remember.
- Missions: Splinter write-ups are tailored with the profile and related memories.
- Learner (Sleeper, 23:30 nightly): extracts durable facts Patrick stated in the day's chats and
  missions (skipping temporary and sensitive things), and archives mission details older than a
  year, with a heads-up in the brief the week before.
- Console: Memory section in Ops to search, teach and forget.

## Milestone 10: Agentic core

Wetworks moves from fixed pipelines to agents that choose their own steps.
- `wetworks/agent/runtime.py`: goal + toolbox + step loop. Each step the model returns JSON to call a
  tool or finish; tool results come back as observations. Step limits, bad-step recovery, tool
  failures become observations. Same protocol on the local model and Claude (`WETWORKS_AGENT_BRAIN`).
- `wetworks/agent/toolkit.py`: web search, read page, recall/remember, missions (list, read, start
  research or code), findings, leads (list, note, dismiss, draft outreach), follow-ups, tell Patrick.
- Autonomy policy (Patrick's choice): agents act freely inside Wetworks; anything that leaves it or
  can't be undone is gated (`gate="approve"`) and waits in `agents.db` until approved in the
  console's **Needs you** panel. First gated tool: merging a pull request.
- `wetworks/agent/commander.py`: The Architect commands. Check-ins every 2 hours, 08:00–22:00, and
  two minutes after any mission finishes. He reviews findings, leads, missions, follow-ups and memory
  against the priorities, opens at most 2 missions per cycle (3 open at once, 6 per day), leaves
  follow-ups, and reports to Activity; Discord only for what needs action today.
- The Architect's chat channel now runs as an agent with the same tools.
- Reality check: the free 12B model handles short loops; long autonomous chains need Claude (Friday).

## Milestone 11: Everything agentic

Every part of Wetworks now decides its own steps with tools:

| Agent | Brain | Tools | Notes |
|---|---|---|---|
| The Architect (cycles + chat) | `WETWORKS_AGENT_BRAIN` | full toolbox incl. gated merge | commander |
| C.L.a.i.R (chat + voice) | Gemini via her Worker's step mode; local fallback | missions, leads, findings, memory, web, `tell_architect` | missions she opens report back to her |
| Intel (per topic) | agent brain | search, read, recall, `record_finding` | writes its own queries |
| Revenue | agent brain | search, read, recall, `record_lead` | drafts outreach for strong fits |
| Splinter research | agent brain | search, read, recall | decides whether and what to research; writer then writes |
| Code cell | code brain | plan → edit → test → repair | already iterative |

- `wetworks/agent/research.py`: research tools scoped to one run. Every URL an agent sees is tracked,
  and findings and leads can only point at those URLs, so links are always real.
- If the model can't drive its tools at all (no searches), Intel and Revenue fall back to their fixed
  routines for that run, so a bad day for the local model never means an empty brief.
- `WETWORKS_CELLS_AGENTIC=false` switches the cells back to fixed routines.
- C.L.a.i.R's Worker (`clair-worker.js`) has a `mode: "step"` on the private route: one agent decision
  per call. Gemini's free tier limits how many steps she can take per day; when it's hit she thinks on
  the local model and the console says so.

## Milestone 12: Knowledge and The Forge

**Knowledge.** `config/knowledge/*.md` is loaded into memory on start (seeded from Claude's memory of
Patrick's past chats). `import-chats` loads a Claude data export (`conversations.json` or the export
zip, kept in the git-ignored `config/imports/`) into a searchable archive; The Architect and C.L.a.i.R
have `search_past_chats` and `read_past_chat`.

**The Forge** (`forge/` container + `wetworks/forge.py`):
- Unsloth QLoRA fine-tuning on the RTX 4070 (8GB). Default base `unsloth/Qwen3-4B-Instruct-2507`.
- Job runner on port 8900 inside the GPU container; one job at a time; progress in `status.json`.
- GPU handoff: before training, Wetworks unloads every Ollama model and pauses local thinking
  (`LocalModelPaused`; C.L.a.i.R keeps going on Gemini, Architect cycles skip). Afterwards it reloads.
- Jobs run in the 01:00–05:00 window unless started "now".
- Output: GGUF (q4_k_m) registered in Ollama as `wetworks-<name>:<id>` via the blobs + create API.
- Data: every agent decision is recorded (`traces.db`) with the brain that made it. The `agent-steps`
  dataset uses only valid decisions from teacher brains (Gemini now, Claude from Friday), so the local
  model learns to drive Wetworks' agents. 10% is held out.
- Benchmarks: any local model is scored on held-out decisions (valid step, same action, same tool).
- Switching the agents' local model is approval-gated for The Architect; Patrick can switch any time.

## Next milestones

1. The ops-center UI.
2. Claude powering the agents and the Code cell (Friday).
2. One-time import of Mac notes and documents into memory.
2. Revenue follow-ups: reminders for contacted leads, and a Splinter that researches a lead
   in depth before you reach out (Sleeper), fed by Intel's money findings.
3. C.L.a.i.R bridge: The Architect ↔ Cloudflare Worker.
4. The Forge: local model container with GPU.
5. Ops-center UI (gritty Splinter Cell style).
6. Memory: profile seed, Mac notes import, 1-year auto-archive.
