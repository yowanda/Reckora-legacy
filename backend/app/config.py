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
    # Phase 3: identity extraction and graph recursion knobs.
    # ``extract_min_confidence`` is the smallest CLAIMED confidence we will
    # run the extractor on — below this we treat the result as too noisy
    # to surface identifiers from. ``graph_max_depth=1`` and
    # ``graph_max_secondary=5`` together cap the worst-case graph search
    # at 1 + N*5 scans where N is the number of root CLAIMED hits with
    # extractable secondary usernames; in practice we cap N before
    # spreading via ``graph_max_seed_hits``.
    extract_min_confidence: int = 70
    graph_max_depth: int = 1
    graph_max_secondary: int = 5
    graph_max_seed_hits: int = 8
    # Phase 4: ML-confidence classifier. Disable to drop the
    # onnxruntime cost entirely; the rule-based classifier remains
    # untouched. The threshold below picks borderline rule-based
    # CLAIMED/FREE results for which the SPA shows an ``ml-disagree``
    # signal when the model disagrees by ≥ this many points.
    enable_ml_confidence: bool = True
    ml_disagree_threshold: int = 30
    # Phase 4: site-overrides editor. When True the admin DB editor
    # endpoints are exposed; the merged catalog (sites.json + overrides
    # table) is what the runtime probes. Setting to False reverts to
    # phase-3 behaviour where ``sites.json`` is authoritative.
    enable_site_overrides: bool = True
    # Phase 4: per-member scan history. Each finished /api/search +
    # /api/search/graph stream is recorded so members can diff scans
    # later. ``scan_history_max_per_user`` is the rolling window we
    # keep — older scans are pruned on insert.
    enable_scan_history: bool = True
    scan_history_max_per_user: int = 50
    # Phase 5e: optional outbound proxy. ``proxy_url`` is read once at
    # process start (env var: ``RECKORA_PROXY_URL``) and supports any
    # scheme httpx understands (``socks5://`` / ``socks5h://`` /
    # ``http://`` / ``https://``). Leaving it blank disables the
    # feature entirely. The runtime gate is a separate
    # admin-configurable boolean knob (``proxy_enabled``) so an
    # operator can flip the switch without restarting; an unset
    # ``proxy_url`` makes the knob a no-op.
    proxy_url: str = ""
    # Default value of the ``proxy_enabled`` admin knob. Set to 1 via
    # ``RECKORA_PROXY_ENABLED_DEFAULT=1`` to opt every freshly-deployed
    # instance into the proxy by default; admins can still override at
    # runtime via ``PATCH /api/admin/settings``.
    proxy_enabled_default: int = 0


settings = Settings()
