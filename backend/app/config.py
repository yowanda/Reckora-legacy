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
    # Phase 10d: deep recursion knobs.
    #
    # ``graph_max_depth`` now ranges 0-3 (previously 0-1). To prevent the
    # exponential blow-up that comes with depth>1 we add two cross-cutting
    # caps:
    #
    # * ``graph_secondary_min_confidence`` (50-100, default 80) — every
    #   *child* CLAIMED hit must clear this rule-based confidence floor
    #   before its extracted handles are allowed to spawn the next
    #   generation. The Phase 5h ``min_claimed_confidence=60`` floor
    #   already gates the row from staying CLAIMED in the first place;
    #   this stricter floor on top recognises that a 70% score at depth
    #   2 is much shakier than the same score at depth 1 because errors
    #   compound across hops.
    # * ``graph_max_total_subscans`` (50-500, default 200) — hard cap on
    #   the total number of secondary + tertiary sub-scans spawned
    #   across the whole graph search, regardless of the per-parent
    #   ``graph_max_secondary`` cap. Hitting the cap surfaces a
    #   SKIPPED row with reason ``graph_subscan_cap`` so the SPA can
    #   render a counter; existing behaviour at depth 1 is unchanged
    #   because the worst-case at depth 1 is N_seed * max_secondary =
    #   8 * 5 = 40, well below the default cap of 200.
    graph_secondary_min_confidence: int = 80
    graph_max_total_subscans: int = 200
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
    # Phase 5h: claimed precision floor.
    #
    # Even after Phase 1 signal scoring, Phase 2 verify, and Phase 4 ML
    # rerank, a small fraction of CLAIMED hits in 2,506-site scans are
    # actually borderline (low confidence, ML disagreed but didn't tip
    # past the disagree threshold, verify wasn't run because the site
    # is HEAD-only, etc). The three knobs below let an operator define
    # a precision floor that demotes such hits from CLAIMED to UNKNOWN
    # before the SSE event is emitted, so the SPA's claimed bucket only
    # surfaces hits the operator is willing to vouch for. Defaults are
    # tuned from a Phase-4 history-scan analysis: the top-decile
    # known-FP claims had mean confidence 42 and mean ml_confidence 31
    # (on a 0-100 scale), so 60 / 50 cleanly separates them from real
    # claims (mean confidence 87, mean ml_confidence 79).
    min_claimed_confidence: int = 60
    min_claimed_ml_confidence: int = 50  # 0-100, mapped to 0.0-1.0
    require_verify_for_claim: int = 0  # 1 = strict (verify must agree)
    # Phase 5h Layer D: post-claim link-liveness probe.
    #
    # After every CLAIMED row survives the precision floor, do one
    # extra HEAD/GET on the human-facing display URL (post username
    # substitution). Demote when the URL is dead, redirected to the
    # site homepage, redirected to a login/signup wall, or looks like
    # a soft-404. Disabling this knob skips the extra probe — useful
    # behind a heavily rate-limited egress where every claim already
    # costs 2-3 requests via verify + ML.
    enable_link_liveness: int = 1
    # Short timeout — the claim itself already paid for the slow path.
    link_liveness_timeout: float = 4.0
    # Phase 7a: perceptual-hash graph stitching.
    #
    # When ``enable_image_phash`` is 1, every CLAIMED hit whose
    # extractor pass produced an ``avatar_url`` triggers one extra
    # bounded GET on that URL; the response body is fed through an
    # 8x8 dHash to produce a 64-bit fingerprint stored on
    # ``CheckResult.extracted['avatar_phash']``. The
    # ``/api/search/graph`` stream then emits ``phash_edge`` events
    # for every pair of CLAIMED hits whose hashes differ by at most
    # ``image_phash_max_distance`` bits — strong evidence the same
    # person owns both accounts even if usernames diverge. Default
    # off because the extra HTTP-per-claim is not free; admins flip
    # the knob via PATCH /api/admin/settings.
    enable_image_phash: int = 0
    # 0-32. 6 separates "same image, mild reencoding" from
    # "different but similar avatars" reasonably well on the
    # 64-bit dHash.
    image_phash_max_distance: int = 6
    # Hard cap on the avatar bytes we'll buffer per hit. Modern
    # platforms usually serve avatars between 5-50 KB; 256 KB
    # leaves comfortable headroom and prevents a malicious hit
    # from streaming a 10 MB PNG into RAM.
    image_phash_max_bytes: int = 262144
    # Short timeout — the avatar fetch is enrichment, not a
    # blocking part of the claim verification.
    image_phash_timeout: float = 3.0

    # Phase 10b: catalog parity / tentative-sites opt-in.
    #
    # ``scripts/build_sites.py`` previously *trimmed* sites whose
    # status-200 fingerprint was ambiguous (e.g. the site returns 200
    # for any path so a username probe can't classify cleanly). The
    # trimmed sites were dropped entirely from ``sites.json``. Phase
    # 10b ships them with ``tentative=true`` so the operator can flip
    # ``enable_tentative_sites=1`` for a max-recall scan when they
    # know the precision trade-off and want the extra coverage.
    #
    # The default (0) preserves the pre-Phase-10b probe set byte-for-
    # byte: ``SiteCatalog.filtered`` skips ``tentative=true`` sites,
    # the SPA tag picker hides the ``"tentative"`` filter chip, and
    # ``checker._confidence`` never sees a tentative-site row. When
    # the knob is on, tentative sites are probed but their final
    # rule-based confidence is capped at ``max_tentative_confidence``
    # (default 70) so the Phase 5h precision floor still rejects them
    # unless ML + verify both vouch strongly.
    enable_tentative_sites: int = 0
    max_tentative_confidence: int = 70

    # Phase 7b: WHOIS / HIBP-style breach enrichment for email scans.
    #
    # When ``enable_email_enrichment`` is 1, the SPA's email scan
    # surfaces a side panel that calls ``POST /api/enrich/email``;
    # the handler runs an RDAP lookup on the email's domain and an
    # HIBP breach lookup on the address (if an API key is
    # configured). Both lookups are read-only, key-or-keyless OSINT
    # signals — they do not bypass authentication, scrape private
    # data, or attempt credential discovery. Default off because
    # the upstream APIs have their own quota / API-key limits and
    # casual list-mode operators don't need the extra side panel.
    enable_email_enrichment: int = 0
    # Per-actor sliding-window rate limit (requests / minute) so a
    # member can't accidentally drain the upstream API quota by
    # mashing the enrichment button.
    enrichment_rate_per_min: int = 5
    # RDAP endpoint. ``rdap.org`` aggregates and proxies the IANA
    # bootstrap registry across all registrars, returns structured
    # JSON, and crucially needs no API key. Operators can swap in
    # a private RDAP mirror if they want to avoid leaking the
    # query domain to a third party.
    enrichment_whois_url_template: str = "https://rdap.org/domain/{domain}"
    # HIBP API v3. ``truncateResponse=false`` returns the full
    # ``Breach`` objects (name, title, breachDate, dataClasses,
    # ...). Without the explicit param the API returns just the
    # breach names and the SPA would have to follow up with N
    # extra GETs.
    enrichment_hibp_url_template: str = (
        "https://haveibeenpwned.com/api/v3/breachedaccount/{email}?truncateResponse=false"
    )
    # HIBP requires a paid API key in the ``hibp-api-key`` header.
    # **Never** exposed via the public ``/api/settings`` route — only
    # the ``/api/enrich/email`` handler reads it. Empty string
    # disables the breach side of enrichment cleanly (the WHOIS half
    # still works).
    enrichment_hibp_api_key: str = ""
    # Per-lookup timeout. Each ``enrich_email`` call runs WHOIS +
    # HIBP concurrently with this timeout, so the SPA's worst-case
    # wait is one ``enrichment_timeout`` and not the sum.
    enrichment_timeout: float = 5.0

    # Phase 7d: ML reranker drift detector.
    #
    # ``ml_drift_ks_threshold_pct`` is interpreted as a percentage
    # (0-100) so it fits the existing integer-only admin-knob
    # surface; the runtime divides by 100 before passing it to the
    # KS-statistic gate. 10 (== 0.10) is a reasonable starting
    # threshold for the synthetic training set's row count;
    # operators with a noisier production stream should crank it up.
    ml_drift_ks_threshold_pct: int = 10
    # Maximum number of feature vectors kept in the in-memory ring
    # buffer the drift detector samples from. 2048 covers ~10 scans
    # of a 200-site catalog without leaking memory.
    ml_drift_window_size: int = 2048

    # Phase 7c: reverse-image lookup hooks.
    #
    # When ``enable_reverse_image`` is 1, every CLAIMED hit whose
    # extractor pass produced an ``avatar_url`` gains a small row of
    # provider chips in the SPA (Yandex / Google Lens / TinEye) that
    # deep-link to the upstream's "search-by-image-URL" landing page
    # in a new tab. No backend HTTP call is made — the SPA simply
    # builds the URL client-side from the extracted ``avatar_url``
    # plus the templates below. Each provider is a public, free,
    # no-API-key endpoint. Default on because there's no upstream
    # quota cost (the SPA never calls these endpoints itself; the
    # operator's browser does, on click). Admins can disable the row
    # entirely or swap in a private mirror.
    enable_reverse_image: int = 1
    # ``{url}`` is replaced with the URL-encoded ``avatar_url``.
    # Yandex Images: <https://yandex.com/images/search?rpt=imageview>.
    reverse_image_yandex_template: str = "https://yandex.com/images/search?rpt=imageview&url={url}"
    # Google Lens accepts an image URL via ``uploadbyurl``.
    reverse_image_google_template: str = "https://lens.google.com/uploadbyurl?url={url}"
    # TinEye supports the same query-string protocol.
    reverse_image_tineye_template: str = "https://tineye.com/search?url={url}"

    # Phase 8a: distributed worker fleet via Celery + Redis.
    #
    # When ``use_celery`` is 1 the per-site ``check_site`` call is fanned
    # out via a Celery group instead of an in-process asyncio worker
    # pool. The API process keeps the SSE stream open and yields each
    # task's result back to the operator as soon as it lands in the
    # result backend. Default 0 — HF Space's single-container deployment
    # keeps the in-process path so it does not require a separate
    # broker. Operators who set ``RECKORA_USE_CELERY=1`` are expected to
    # have a Redis broker reachable via ``celery_broker_url`` and one or
    # more workers running ``celery -A app.tasks worker``. The flag is
    # read at request time (not process start) so flipping the env and
    # reloading is enough to switch back to the in-process path.
    use_celery: int = 0
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    # Per-task timeout. A worker that exceeds this is killed and the
    # task transitions to FAILURE; the API-side collector synthesises an
    # ERROR ``CheckResult`` for that site so the SSE stream stays
    # consistent with the in-process path. 30s sits comfortably above
    # the worst-case per-site budget (verify + extract + phash ~= 12s).
    celery_task_timeout: float = 30.0
    # Polling interval used by the API-side collector to poll
    # ``AsyncResult.ready()`` on each in-flight task. Smaller values
    # reduce per-result tail latency at the cost of CPU on the API
    # process; 25 ms is a reasonable default given typical per-site
    # check latency is in the 200-2000 ms range.
    celery_poll_interval: float = 0.025

    # Phase 8b: rate-limit-aware proxy rotator.
    #
    # Two independent capabilities, both opt-in via admin knobs:
    #
    #  1. Multi-proxy egress pool (`enable_proxy_rotator=1`). When on,
    #     every outbound request through `_new_async_client` is routed
    #     via one of the URLs in `proxy_pool_urls` (comma-separated env
    #     `RECKORA_PROXY_POOL_URLS`). Selection is round-robin with a
    #     short health cooldown after a transport error so a flaky
    #     proxy doesn't keep getting picked. The pool **supersedes**
    #     the Phase 5e single `proxy_url` while it's enabled, so
    #     operators with one proxy can continue to use 5e's gate; only
    #     flip 8b on when there is more than one egress IP.
    #
    #  2. Per-host token-bucket rate limit (`enable_host_rate_limit=1`).
    #     When on, each request waits for a token from the host's
    #     bucket before dispatch. `host_rate_per_min` is the steady-
    #     state refill rate; the bucket's capacity matches it so a
    #     1-minute idle period absorbs a 60-request burst on a 60/min
    #     bucket. Limits are per-host (request URL host, lowercased),
    #     so Reddit's bucket is independent from Twitter's.
    #
    # Defaults are 0 / 0 — HF Space's single-container deployment is
    # untouched until an operator turns either knob on.
    proxy_pool_urls: str = ""
    enable_proxy_rotator: int = 0
    enable_host_rate_limit: int = 0
    # 1-600 requests / minute / host. 60 (== 1 rps / host) is a safe
    # default that keeps the largest sites well below their public
    # documented limits while still letting a 2,506-site scan finish
    # in a reasonable time on a fan-out of ~50 concurrent workers.
    host_rate_per_min: int = 60
    # 1-600 seconds. After a transport error against a proxy URL the
    # pool will skip that URL for this many seconds before reusing it.
    # 60s matches the default per-host bucket window so a tripped
    # proxy gets one full bucket cycle to recover.
    proxy_health_cooldown_sec: int = 60

    # Phase 8c: GraphQL gateway as an alternate transport.
    #
    # When ``enable_graphql_gateway`` is 1 the route at
    # ``/api/graphql`` exposes a Strawberry-served schema that mirrors
    # the REST surface — health, sites, current member, scan history,
    # the search mutation (collected from the SSE stream into a single
    # response), permute strategies, and admin settings reads. Default
    # 0 because the REST surface is the supported transport for the
    # bundled SPA and exposing a second one increases the attack
    # surface; operators who want to drive Reckora from a third-party
    # dashboard (Hasura / Apollo / their own client) flip the knob via
    # PATCH /api/admin/settings without redeploying.
    enable_graphql_gateway: int = 0

    # Phase 8d: public share links + edge cache for shared scans.
    #
    # When ``enable_share_links`` is 1 a scan owner can mint a
    # URL-safe random token via ``POST /api/history/{scan_id}/share``
    # and the public endpoint ``GET /share/{token}`` returns the scan
    # summary + results without authentication. ``Cache-Control``
    # uses ``s-maxage`` so a CDN edge keeps the response in front of
    # the backend for ``share_link_cache_max_age_sec`` seconds.
    enable_share_links: int = 0
    # 0 = links never expire until explicitly revoked. Anything > 0
    # is treated as the default TTL applied when the owner doesn't
    # specify ``ttl_sec`` on the mint request; the owner is also
    # capped at this value when the knob is non-zero so a misbehaving
    # SPA can't lock a publicly shared scan in for longer than admin
    # policy allows.
    share_link_default_ttl_sec: int = 0
    # 1-86400 seconds. Edge ``s-maxage``. 60s is short enough that a
    # revoke propagates within a minute on Cloudflare-class CDNs but
    # long enough to absorb a viral scan landing on Reddit /
    # HackerNews. Operators who don't care about revoke latency can
    # bump this to e.g. 3600 to maximise edge-hit rate.
    share_link_cache_max_age_sec: int = 60
    # Per-scan cap so a single scan doesn't accumulate dozens of live
    # tokens in the database. Tightened by raising this knob; new
    # mint attempts past the cap return 409.
    share_link_max_per_scan: int = 5

    # Phase 9a: anti-bot probe pipeline (closes residual risk C1
    # Cloudflare/Akamai/Datadome JS challenge + C2 TLS / JA3 / JA4
    # fingerprint blocking, both CRITICAL).
    #
    # When ``enable_anti_bot_pipeline`` is 1, every outbound site
    # probe is dispatched via :mod:`app.anti_bot_transport` —
    # an :class:`httpx.AsyncBaseTransport` backed by ``curl-cffi``
    # impersonation. Combined with the Phase 8b ``RotatingProxyTransport``
    # (which stacks *outside* the anti-bot transport), an operator
    # gets browser-shaped TLS fingerprints across every egress IP in
    # the proxy pool.
    #
    # ``anti_bot_default_impersonation`` selects the default profile
    # — ``chrome120`` / ``chrome131`` / ``safari17`` / ``firefox120``.
    # Unknown / mistyped values silently fall back to ``chrome120`` so
    # a typo doesn't disable the pipeline. Per-site overrides via
    # ``site_overrides.<name>.patch.antiBotProfile`` win over this
    # default.
    #
    # ``anti_bot_playwright_fallback`` controls whether sites that
    # *still* return a JS-challenge HTML after the curl-cffi
    # impersonation are re-fetched through a headless chromium. Off
    # by default because chromium has a ~200 MB image footprint and a
    # warm-up cost — operators only flip it on when they have a
    # custom Docker image with playwright pre-installed.
    #
    # ``anti_bot_playwright_max_per_scan`` is a hard ceiling on
    # chromium spawns per scan so a worst-case 2,500-site scan can't
    # fan out 2,500 chromium processes. ``0`` disables the fallback
    # entirely without needing to flip the boolean knob.
    enable_anti_bot_pipeline: int = 0
    anti_bot_default_impersonation: str = "chrome120"
    anti_bot_playwright_fallback: int = 0
    anti_bot_playwright_max_per_scan: int = 5
    anti_bot_playwright_timeout_sec: int = 10

    # Phase 9b: distributed host rate limit (closes residual risk M1
    # per-process token bucket; MEDIUM).
    #
    # The Phase 8b ``host_rate_limit.py`` ships a per-process token
    # bucket. That works for a single API container but a Celery
    # fan-out (Phase 8a) gives every worker its own bucket so the
    # effective rate towards a host is N x ``host_rate_per_min`` when N
    # workers are scanning concurrently. 9b adds a Redis-backed
    # sliding-window counter that uses one atomic Lua script for the
    # ``INCR + EXPIRE + check`` round-trip — N workers now share one
    # bucket per host so the rate stays at the configured value
    # regardless of fan-out width.
    #
    # Default ``host_rate_limit_use_redis=0`` keeps the in-memory
    # bucket — HF Space's single-container deployment doesn't need
    # the Redis hop. Operators who run Celery workers flip the knob
    # to ``1`` after wiring up ``host_rate_limit_redis_url`` (or
    # leaving it blank to reuse the Phase 8a Celery broker URL).
    #
    # If the Redis transport ever fails at runtime the limiter falls
    # *open* to the in-memory bucket so a Redis outage doesn't
    # fail-closed every probe — being briefly too-fast on rate is
    # better than being completely stuck.
    host_rate_limit_use_redis: int = 0
    host_rate_limit_redis_url: str = ""

    # Phase 9c: geo-aware proxy selection (closes residual risk M2
    # round-robin pool ignores region locks; MEDIUM).
    #
    # Phase 8b's ``proxy_pool`` rotates URLs round-robin without
    # caring about egress region. That means a Yandex / VK probe
    # may exit through an EU proxy and trip the geo filter even
    # though a "ru" proxy is sitting idle in the same pool. 9c
    # extends the pool URL parser to accept a ``region:url``
    # prefix (e.g. ``"us-east:socks5://a.example,ru:socks5://b.example"``)
    # and a per-site ``site_overrides.<name>.patch.preferredRegion``
    # tag; selection prefers a proxy whose region matches the
    # site's preferred region, falls back to the ``"default"``
    # region, then to any healthy proxy as a last resort so a
    # single-region pool still works.
    #
    # Default ``enable_geo_aware_proxy=0`` preserves the Phase 8b
    # round-robin behaviour bit-for-bit. Operators with a
    # multi-region pool flip the knob to ``1`` and tag relevant
    # sites via the existing site-override admin UI; the
    # ``X-Reckora-Site-Name`` request header set by the checker is
    # how the transport layer knows which site (and therefore
    # which preferred region) the in-flight request belongs to.
    enable_geo_aware_proxy: int = 0

    # Phase 9d: vendor-specific CAPTCHA classifier (closes residual
    # risk M3 silent UNKNOWN on CAPTCHA challenge; MEDIUM).
    #
    # When ``enable_captcha_classifier`` is 1, the per-site checker
    # consults :mod:`app.captcha_signatures` against the response body
    # whenever the BLOCKED branch could fire. A vendor-specific
    # signature match (reCAPTCHA v2 / v3, hCaptcha, Cloudflare
    # Turnstile, Datadome, AWS WAF Captcha, FunCaptcha, Geetest,
    # PerimeterX, generic ``<form action="/captcha">``) sets the
    # new ``CheckResult.reason`` field to ``"captcha_<vendor>"`` so
    # the SPA can render a vendor-aware badge instead of the generic
    # ``body-fingerprint-block`` signal. Per-vendor counts surface
    # via ``GET /api/admin/ml/status.captcha_vendors_seen`` so an
    # operator can see e.g. "47 sites returned Cloudflare Turnstile
    # this week".
    #
    # Default ``0`` keeps the runtime byte-for-byte equivalent to the
    # pre-9d behaviour — the classifier module is never imported on
    # the hot path, ``CheckResult.reason`` stays ``None``, and the
    # admin endpoint reports an empty ``captcha_vendors_seen`` map.
    enable_captcha_classifier: int = 0

    # Phase 9f: per-scan hard budget + SKIPPED bucket (closes residual
    # risk M5 scan tail-latency runaway; MEDIUM).
    #
    # When ``enable_scan_budget`` is 1 the in-process scan walker
    # instantiates a :class:`backend.app.scan_budget.ScanBudget` and
    # gates every worker on three thresholds:
    #
    # * ``scan_max_total_sec`` — hard wall-clock ceiling for the whole
    #   scan. Once exceeded, every still-queued worker emits a
    #   :data:`backend.app.checker.Status.SKIPPED` row with
    #   ``reason="scan_total_killed"`` and the SSE stream closes.
    # * ``scan_per_site_timeout_sec`` — ``asyncio.wait_for`` budget
    #   wrapped around each :func:`check_site` call. On timeout the
    #   site emits :data:`Status.SKIPPED` with ``reason="probe_timeout"``
    #   and the consecutive-skip counter ticks.
    # * ``scan_max_concurrent_skips`` — once this many consecutive
    #   probes time out (without an intervening success) the budget
    #   latches into bulk-skip mode and the rest of the queue emits
    #   :data:`Status.SKIPPED` with ``reason="budget_exceeded"``.
    #
    # Default ``enable_scan_budget=0`` preserves the pre-9f
    # ``stream_checks`` walk byte-for-byte — no per-probe wrapping,
    # no SKIPPED rows, no ``ScanBudget`` allocation on the hot path.
    enable_scan_budget: int = 0
    scan_max_total_sec: int = 300
    scan_per_site_timeout_sec: int = 8
    scan_max_concurrent_skips: int = 50

    # Phase 9g: proxy reputation warm-up + weighted selection (closes
    # residual risk M6 round-robin pool ignores per-proxy reliability;
    # MEDIUM).
    #
    # The Phase 8b ``ProxyPool`` rotates URLs strictly round-robin and
    # the Phase 9c geo-aware extension only adds a region preference
    # on top. A flaky proxy that intermittently times out is recycled
    # back into the rotation as soon as its short cooldown expires
    # so it keeps tripping host rate limits and burning probe budget
    # on the same buckets. 9g adds a *reputation* signal — the running
    # success ratio per URL — and biases the pool selector toward
    # proxies with a higher ratio. New proxies get a configurable
    # warm-up window during which they are treated as 1.0-weight
    # regardless of failures so they can collect enough samples to be
    # ranked fairly.
    #
    # Default ``enable_proxy_reputation=0`` keeps the pre-9g pool walk
    # byte-for-byte identical — no weighted pick, no per-URL
    # ``ProxyReputation`` allocation, no extra hot-path counter
    # mutation on ``mark_failed`` / ``mark_success``.
    enable_proxy_reputation: int = 0
    # Days a freshly-seen URL stays at full warm-up weight before its
    # recorded success ratio takes over. ``0`` disables the warm-up
    # entirely (every URL is ranked by its recorded ratio from the
    # first probe). Range 0-365.
    proxy_warmup_window_days: int = 7

    # Phase 10a: auto-PDF preliminary export on initial CLAIMED hit.
    #
    # Master switch (``0``=off, ``1``=on). When off, the
    # ``InitialHitTracker`` wired into every SSE search stream is a
    # no-op — no PDF is rendered, no event is emitted, no disk is
    # touched. Default-off so deploys can flip the feature on after
    # verifying behaviour against the live site catalog.
    enable_initial_hit_pdf: int = 0
    # Confidence floor (0-100) the first CLAIMED primary-pass hit must
    # clear before the latch fires. ``80`` matches Phase 6c's strict
    # claimed floor — preliminary PDFs that capture an UNKNOWN-shaded
    # signal would be misleading to the operator.
    initial_hit_min_confidence: int = 80
    # When ``1`` the latch additionally requires ``verified is True``
    # (Phase 6b verify-pass agreement). Off means a result that hit
    # the confidence floor but skipped the verify pass — for
    # example because the engine flagged it as cached or because the
    # verify pass couldn't reach the site — can still latch.
    initial_hit_require_verify: int = 1
    # Lifetime of a persisted preliminary PDF on disk before the
    # janitor task purges it. Default 24h. Operators tuning this on a
    # public deploy should keep it short — the file leaks the scan's
    # CLAIMED hit URL on a path that, while unguessable, is sent to
    # the SPA on the SSE channel and may be cached in browser
    # history. Range 600-604800 (10 minutes - 7 days).
    initial_hit_pdf_ttl_sec: int = 86400
    # Whether the partial PDF includes the per-site extractor extras
    # (avatar URL, secondary handles, ML confidence, demote signals).
    # Useful when the operator wants the lightweight "did we hit?"
    # report; off-the-shelf operators usually want the full picture.
    initial_hit_include_extras: int = 1

    # Phase 10g: AI investigation summary.
    #
    # Master switch (``0``=off, ``1``=on). When off, the
    # ``POST /api/history/{scan_id}/ai_summary`` endpoint returns 503
    # so the SPA's "AI summary" tab knows to render a "feature
    # disabled" hint instead of the live preview. Default-off so
    # the live HF Space mirror stays byte-for-byte identical until
    # an operator explicitly opts in (which also means configuring
    # an API key in ``secrets.json`` — see ``app.secrets_store``).
    enable_ai_summary: int = 0
    # Provider name. One of ``openai`` / ``anthropic`` / ``openrouter``
    # / ``ollama``. The provider determines the wire format and the
    # default base URL when ``ai_summary_base_url`` is empty.
    # Ranged knobs are int-only in the admin DB so the runtime stores
    # the *index* of this list; the string surface lives on this
    # ``Settings`` field for the env-var override path.
    ai_summary_provider: str = "openai"
    # HTTP base URL for the chat-completion endpoint. Empty means
    # "use the provider default" — for ``openai`` that is
    # ``https://api.openai.com/v1``. Operators using a private
    # OpenAI-compatible proxy point this at their own host so calls
    # never leave their VPC.
    ai_summary_base_url: str = ""
    # Model id, e.g. ``gpt-4o-mini`` for OpenAI, ``llama3.1`` for a
    # local Ollama deploy. Empty means "use the provider default".
    ai_summary_model: str = ""


settings = Settings()
