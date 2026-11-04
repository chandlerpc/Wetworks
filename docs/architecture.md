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

- Hearing: `whisper` container (faster-whisper `small.en`) on the GPU. Free and private.
- Speaking: Azure neural TTS, `en-IE-EmilyNeural` (young Irish female). Free tier 500k
  characters/month; `tts_monthly_char_cap` (480k) stops speech before that and falls back to text.
  The Azure key stays on the MSI; the phone only receives MP3 audio.
- Console (`console/voice.js`): hold-to-talk mic, and hands-free mode. Wake phrase
  ("hey clair"), stop phrases ("that's all, clair", "goodbye, clair"…), idle timeout (90 s).
  Voice activity detection runs in the browser; each utterance is transcribed on the MSI.
- Limit: browsers only allow the microphone while the console is on screen. Always-on
  "Hey Clair" with the screen locked needs a native app.

## Next milestones

1. Code cell, so Wetworks can work on its own repo (needed for "build itself").
2. Revenue cell (Sleeper), fed by Intel's money findings.
3. C.L.a.i.R bridge: The Architect ↔ Cloudflare Worker.
4. The Forge: local model container with GPU.
5. Ops-center UI (gritty Splinter Cell style).
6. Memory: profile seed, Mac notes import, 1-year auto-archive.
