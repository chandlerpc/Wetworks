"""Settings, loaded from environment variables (prefix WETWORKS_) or .env."""

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WETWORKS_", env_file=".env", extra="ignore")

    # Secrets. ANTHROPIC_API_KEY / XAI_API_KEY are also read without the prefix.
    anthropic_api_key: str = ""
    xai_api_key: str = ""
    discord_webhook_url: str = ""

    # Behaviour
    dry_run: bool = False  # no real API calls or Discord posts; prints instead
    timezone: str = "America/New_York"
    morning_brief: str = "07:00"
    evening_brief: str = "21:00"
    monthly_budget_usd: float = 250.0
    budget_warn_ratio: float = 0.8

    # Provider for briefs and Intel: "local" (free: Ollama + SearXNG), "claude", or "grok".
    # Paid providers are never used unless chosen here.
    provider: str = "local"

    # Local (free) stack
    ollama_url: str = "http://ollama:11434"
    local_model: str = "gemma4:12b"
    local_num_ctx: int = 16384
    searxng_url: str = "http://searxng:8080"

    # Claude model tiers
    model_route: str = "claude-haiku-5-5"
    model_work: str = "claude-sonnet-5-5"
    model_think: str = "claude-opus-5-5"
    claude_web_search_tool: str = "web_search_20260209"

    # Grok. Prices are USD per million tokens; check docs.x.ai and update if they differ.
    grok_model: str = "grok-4.6"
    grok_price_in: float = 3.0
    grok_price_out: float = 15.0

    # Sleepers
    intel_lead_minutes: int = 20  # Intel runs this long before each brief
    intel_tier: str = "work"  # Claude tier for Intel when provider = claude

    # C.L.a.i.R's Cloudflare Worker (private /wetworks route). Leave blank to run C.L.a.i.R locally.
    clair_url: str = ""
    clair_token: str = ""
    owner_name: str = "Patrick"

    # Command API / ops console. The console refuses to start without a token.
    console_token: str = ""
    console_port: int = 8700

    data_dir: Path = Path("/data")
    config_dir: Path = Path("config")


def load_settings() -> Settings:
    s = Settings()
    if not s.anthropic_api_key:
        s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not s.xai_api_key:
        s.xai_api_key = os.environ.get("XAI_API_KEY", "")
    return s
