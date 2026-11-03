# Wetworks

Patrick's multi-agent ops center. The Architect oversees; Splinter and Sleeper cells do the work.
Runs 24/7 on the MSI (WSL2 + Docker). See [docs/architecture.md](docs/architecture.md).

## Status

Milestone 1: The Architect sends morning and evening briefs to Discord, tracks spend against a monthly cap, and reports host health.

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
  main.py       scheduler entry point
```
