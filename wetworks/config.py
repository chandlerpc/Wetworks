"""Settings, loaded from environment variables (prefix WETWORKS_) or .env."""

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WETWORKS_", env_file=".env", extra="ignore")

    # Secrets. ANTHROPIC_API_KEY / XAI_API_KEY are also read without the prefix.
    anthropic_api_key: str = ""
    xai_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
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
    revenue_lead_minutes: int = 45  # Revenue hunts once a day, this long before the morning brief
    learner_time: str = "23:30"     # nightly: learn from the day, archive year-old missions

    # C.L.a.i.R's Cloudflare Worker (private /wetworks route). Leave blank to run C.L.a.i.R locally.
    clair_url: str = ""
    clair_token: str = ""
    owner_name: str = "Patrick"

    # Voice. Hearing: Whisper container on the GPU. Speaking: Azure neural TTS (free tier
    # 500k characters/month; the cap below stops speech a little before that).
    whisper_url: str = "http://whisper:9000"
    whisper_fast_url: str = "http://whisper-fast:9000"  # small model for wake-phrase checks ("" = off)
    voice_vocab: str = ""  # extra names for Whisper, comma separated (people, businesses, games)
    azure_speech_key: str = ""
    azure_speech_region: str = ""
    voice_name: str = "en-IE-EmilyNeural"
    tts_monthly_char_cap: int = 480_000
    wake_phrase: str = "hey clair"
    stop_phrases: str = "that's all clair|goodbye clair|that's all|goodbye"
    voice_idle_seconds: int = 90

    # Code cell. Works on code_repo through pull requests only; tests run in the sandbox.
    github_token: str = ""
    code_repo: str = "chandlerpc/Wetworks"
    code_brain: str = "local"   # local (free) | claude-api (paid, budget-capped); claude-code from Friday
    code_max_files: int = 6
    code_repair_rounds: int = 1
    sandbox_url: str = "http://sandbox:8800"

    # Agents. agent_brain: local (free) | claude-api (paid, budget-capped). The Architect checks in at
    # commander_times (local), plus up to commander_max_extra extra check-ins a day when something major
    # happens (a failed mission, a finished code mission, an act-today finding, a top-fit lead).
    agent_brain: str = "local"
    agent_max_steps: int = 8
    # The Architect can think on his own brain. "" = same as agent_brain. claude-api uses the think tier
    # (Opus) with room for longer plans; every call is priced in the ledger and stops at the monthly budget.
    architect_brain: str = ""
    architect_claude_steps: int = 14     # steps per command cycle on Claude
    architect_claude_chat_steps: int = 10  # steps per chat reply on Claude
    architect_claude_step_tokens: int = 3500

    @property
    def architect_brain_effective(self) -> str:
        return self.architect_brain or self.agent_brain
    commander_times: str = "07:30,21:30"
    commander_max_extra: int = 3
    architect_cycle_tier: str = "work"   # check-ins on Claude: work = Sonnet; chats with Patrick use think (Opus)
    agent_daily_missions: int = 6
    cells_agentic: bool = True      # Intel, Revenue and Splinter research run as agents

    # The Forge (training). Jobs run in forge_window (local hours, start-end) unless started "now".
    forge_url: str = "http://forge:8900"
    forge_base_model: str = "unsloth/Qwen3-4B-Instruct-2507"
    forge_window: str = "1-5"
    # Forge drills: practice runs on Gemini that bank lessons. Gemini's free tier is shared with real
    # chats and the portfolio recruiter, so drills stop at drill_daily_steps calls a day.
    drill_enabled: bool = True
    drill_every_minutes: int = 15
    drill_hours: str = "7-23"        # local hours, start-end
    drill_daily_steps: int = 120
    drill_target: int = 600          # stop drilling once this many lessons are banked

    # Command API / ops console. The console refuses to start without a token.
    console_token: str = ""
    console_port: int = 8700

    data_dir: Path = Path("/data")
    config_dir: Path = Path("config")


def load_settings() -> Settings:
    s = Settings()
    if not s.anthropic_api_key:
        s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not s.azure_speech_key:
        s.azure_speech_key = os.environ.get("AZURE_SPEECH_KEY", "")
    if not s.azure_speech_region:
        s.azure_speech_region = os.environ.get("AZURE_SPEECH_REGION", "")
    # Forgive common copy-paste slips: quotes, <placeholder brackets>, spaces, "East US" for "eastus".
    def tidy(v: str) -> str:
        return v.strip().strip("\"'<>").strip()

    s.azure_speech_key = tidy(s.azure_speech_key)
    s.azure_speech_region = tidy(s.azure_speech_region).replace(" ", "").lower()
    s.clair_token = tidy(s.clair_token)
    if not s.github_token:
        s.github_token = os.environ.get("GITHUB_TOKEN", "")
    s.github_token = tidy(s.github_token)
    if not s.xai_api_key:
        s.xai_api_key = os.environ.get("XAI_API_KEY", "")
    if not s.openai_api_key:
        s.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    if not s.gemini_api_key:
        s.gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
    for k in ("anthropic_api_key", "xai_api_key", "openai_api_key", "gemini_api_key"):
        setattr(s, k, tidy(getattr(s, k)))
    return s
