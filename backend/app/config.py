"""Runtime configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, populated from env vars (prefix RECKORA_)."""

    model_config = SettingsConfigDict(env_prefix="RECKORA_", env_file=".env", extra="ignore")

    sites_path: Path = Field(default=Path(__file__).parent / "data" / "sites.json")
    static_dir: Path | None = Field(default=Path(__file__).parent / "static")
    db_path: Path = Field(default=Path(__file__).parent.parent / "var" / "reckora.db")
    login_rate_limit: str = "8/minute"
    request_timeout: float = 8.0
    per_host_concurrency: int = 16
    total_concurrency: int = 64
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Reckora/0.1"
    )
    # Per-IP rate limit for /api/search
    search_rate_limit: str = "20/minute"
    cache_ttl_seconds: int = 60 * 30
    cache_max_entries: int = 512
    cors_origins: str = "*"
    # Phase 1 retry knobs. With ``checker_max_attempts=3`` and
    # ``checker_retry_base_delay=0.25`` the worst-case delay added on a
    # site that always 503s is 0.25 + 0.5 = 0.75s.
    checker_max_attempts: int = 3
    checker_retry_base_delay: float = 0.25
    # Phase 2.2: rescue path. When httpx primary classifies a site as
    # BLOCKED, retry with curl-cffi impersonating Chrome's TLS+H2
    # fingerprint. ``cffi_impersonate`` matches the curl-cffi target
    # browser (chrome120/chrome131/chrome131_android etc.). Setting
    # ``enable_curl_cffi=False`` disables the rescue entirely (saves
    # ~30 MB image + 100ms warm-up per scan).
    enable_curl_cffi: bool = True
    cffi_impersonate: str = "chrome131"
    cffi_timeout: float = 8.0


settings = Settings()
