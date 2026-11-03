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
Intel findings, and two-way comms with clAIr and The Architect. It is only reachable over
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
  cells/        base Cell + Intel
  events.py     activity log + who's working
  comms.py      chat with clAIr and The Architect
  api.py        Command API + console server
  console/      the ops console (HTML/CSS/JS, no build step)
config/
  intel.toml    Intel topics
  main.py       scheduler entry point
```
