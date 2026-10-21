"""Runtime settings, read once from environment variables.

Nothing secret lives in code. Copy .env.example to .env and fill in keys.
With no YouTube or Anthropic key set, the app runs in OFFLINE mode:
recorded YouTube fixtures and a deterministic fake model. That is how the
test suite runs, and how you can demo the dashboard without spending credits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _s(name: str, default: str = "") -> str:
    """Blank values in .env count as 'not set'."""
    return os.getenv(name) or default


def _f(name: str, default: float) -> float:
    return float(_s(name) or default)


def _i(name: str, default: int) -> int:
    return int(_s(name) or default)


def normalise_db_url(url: str) -> str:
    """Supabase and Render hand out postgres:// URLs; SQLAlchemy wants a driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str = normalise_db_url(_s("DATABASE_URL", f"sqlite:///{ROOT / 'trendforge.db'}"))
    # Set on hosted deploys so the cost ledger can never sit on a wiped disk.
    require_postgres: bool = _s("REQUIRE_POSTGRES") == "1"
    youtube_api_key: str = _s("YOUTUBE_API_KEY")
    anthropic_api_key: str = _s("ANTHROPIC_API_KEY")
    admin_token: str = _s("ADMIN_TOKEN")

    # YouTube Data API v3: 10,000 units/day default, search.list = 100 units,
    # videos.list = 1 unit per call (up to 50 ids). Quota resets midnight Pacific.
    youtube_daily_quota: int = _i("YOUTUBE_DAILY_QUOTA", 10_000)
    youtube_quota_reserve: int = _i("YOUTUBE_QUOTA_RESERVE", 500)

    # Model routing. Routine work on Haiku, creative work on Sonnet. No Opus.
    model_fast: str = _s("MODEL_FAST", "claude-haiku-5-5")
    model_smart: str = _s("MODEL_SMART", "claude-sonnet-5-5")
    # USD per million tokens (input, output). Estimates: check current pricing
    # at https://www.anthropic.com/pricing and override via env if different.
    price_fast_in: float = _f("PRICE_FAST_IN", 1.0)
    price_fast_out: float = _f("PRICE_FAST_OUT", 5.0)
    price_smart_in: float = _f("PRICE_SMART_IN", 3.0)
    price_smart_out: float = _f("PRICE_SMART_OUT", 15.0)
    monthly_llm_budget_usd: float = _f("MONTHLY_LLM_BUDGET_USD", 50.0)
    per_run_llm_budget_usd: float = _f("PER_RUN_LLM_BUDGET_USD", 2.0)

    # Pipeline shape
    top_trends: int = _i("TOP_TRENDS", 10)
    briefs_per_run: int = _i("BRIEFS_PER_RUN", 3)

    # Master switch. Off unless PUBLISHING_ENABLED=1 is set, so nothing can leave
    # the app until you turn it on yourself.
    publishing_enabled: bool = _s("PUBLISHING_ENABLED") == "1"
    # Publishing adapters (all optional). Nothing publishes without approval.
    exports_dir: Path = Path(_s("EXPORTS_DIR", str(ROOT / "exports")))
    webhook_url: str = _s("PUBLISH_WEBHOOK_URL")
    fixtures_dir: Path = ROOT / "fixtures"

    offline_override: str = _s("OFFLINE")

    @property
    def youtube_offline(self) -> bool:
        return self.offline_override == "1" or not self.youtube_api_key

    @property
    def llm_offline(self) -> bool:
        return self.offline_override == "1" or not self.anthropic_api_key


settings = Settings()
