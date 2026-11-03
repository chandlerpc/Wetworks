"""Settings, loaded from environment variables (prefix WETWORKS_) or .env."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WETWORKS_", env_file=".env", extra="ignore")

    # Secrets. ANTHROPIC_API_KEY is read without the prefix so the SDK sees it too.
    anthropic_api_key: str = ""
    discord_webhook_url: str = ""

    # Behaviour
    dry_run: bool = False  # no real API calls or Discord posts; prints instead
    timezone: str = "America/New_York"
    morning_brief: str = "07:00"
    evening_brief: str = "21:00"
    monthly_budget_usd: float = 250.0
    budget_warn_ratio: float = 0.8

    # Model tiers
    model_route: str = "claude-haiku-5-5"
    model_work: str = "claude-sonnet-5-5"
    model_think: str = "claude-opus-5-5"

    data_dir: Path = Path("/data")


def load_settings() -> Settings:
    import os

    s = Settings()
    if not s.anthropic_api_key:
        s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    return s
