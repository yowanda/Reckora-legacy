"""Runtime configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, populated from env vars (prefix CACADOXX_)."""

    model_config = SettingsConfigDict(env_prefix="CACADOXX_", env_file=".env", extra="ignore")

    sites_path: Path = Field(default=Path(__file__).parent / "data" / "sites.json")
    static_dir: Path | None = Field(default=Path(__file__).parent / "static")
    db_path: Path = Field(default=Path(__file__).parent.parent / "var" / "cacadoxx.db")
    login_rate_limit: str = "8/minute"
    request_timeout: float = 8.0
    per_host_concurrency: int = 16
    total_concurrency: int = 64
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Cacadoxx/0.1"
    )
    # Per-IP rate limit for /api/search
    search_rate_limit: str = "20/minute"
    cache_ttl_seconds: int = 60 * 30
    cache_max_entries: int = 512
    cors_origins: str = "*"


settings = Settings()
