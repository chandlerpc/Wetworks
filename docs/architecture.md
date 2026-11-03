# Wetworks — Architecture

Wetworks is a multi-agent system that runs 24/7 on the MSI (Windows 11 → WSL2 Ubuntu 24.04 → Docker Engine, GPU passthrough). Development happens on the Mac; the MSI pulls and runs.

## Hierarchy

```
                    Patrick (phone / Discord / voice via clAIr)
                                   │
                         ┌─────────┴─────────┐
                         │      clAIr        │  comms layer (Cloudflare Worker)
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
- **clAIr** stays on Cloudflare and handles comms so The Architect stays on task.

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

## Next milestones

1. Cell runtime: a base `Cell` class, Splinter/Sleeper operators, mission queue.
2. Revenue and Intel cells (Sleepers) on schedules.
3. clAIr bridge: The Architect ↔ Cloudflare Worker.
4. The Forge: local model container with GPU.
5. Ops-center UI (gritty Splinter Cell style).
6. Memory: profile seed, Mac notes import, 1-year auto-archive.
