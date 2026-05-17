"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Anthropic
    anthropic_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""

    # Xiaohongshu
    xhs_cookie: str = ""

    # Database
    database_path: str = "data/findit.db"

    # Crawl settings
    crawl_city: str = "深圳"
    crawl_interval_hours: int = 24
    crawl_request_delay_min: float = 2.0
    crawl_request_delay_max: float = 5.0
    # Delay between profile-page scrapes. What trips XHS 风控 is robotic
    # *uniformity* (a fixed-cadence burst), not the raw gap length — so
    # keep this short but well-jittered. These numbers are a tunable
    # starting point: adjust them by testing, never collapse the range
    # to a constant. See memory feedback_xhs_rate_limit.
    crawl_profile_delay_min: float = 3.0
    crawl_profile_delay_max: float = 10.0
    # After every N profiles, take an extra long break (no requests at all).
    crawl_profile_batch_size: int = 5
    # Randomized batch pause: pick uniformly from [min, max] each batch.
    crawl_profile_batch_pause_min: float = 40.0
    crawl_profile_batch_pause_max: float = 60.0
    crawl_max_retries: int = 3
    crawl_retry_base_delay: float = 5.0
    crawl_request_timeout: float = 30.0
    crawl_proxy: str = ""
    # Generic dating-search keywords used by the B2 click path. A random
    # one is picked per profile to mirror what a real user types into
    # search: "深圳找对象" not "深圳90年男生身高180蹲一个". The post-title
    # path was tried first and produced too-narrow queries that almost
    # never surfaced the target post in results.
    crawl_dating_search_keywords: tuple[str, ...] = (
        "深圳找对象", "深圳找男友", "深圳找女友",
        "深圳脱单", "深圳交友", "深圳cpdd",
        "深圳相亲", "深圳恋爱",
    )

    # Playwright browser timeouts (milliseconds)
    playwright_page_timeout: int = 30000
    playwright_sign_timeout: int = 15000

    # Persistent Chromium profile dir (used by BrowserSession; cookies/session
    # live here so a single QR-login persists across runs).
    browser_profile_dir: str = "data/chromium_profile"
    browser_headless: bool = False  # headed = closer fingerprint match to real users

    # Crawler service
    crawler_continuous: bool = True  # True = run_forever, False = run_once
    crawl_include_profiles: bool = False  # profile scraping needs login, off by default

    # Recommendation
    daily_match_count: int = 5
    high_match_count: int = 3
    push_hour: int = 20
    push_minute: int = 0

    @property
    def db_path(self) -> Path:
        p = Path(self.database_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
