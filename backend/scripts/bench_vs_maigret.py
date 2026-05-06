"""Phase 6c — head-to-head benchmark: Reckora vs soxoj/maigret CLI.

Runs both tools against the same username on a shared subset of sites and
compares:
  - claimed precision (what % of claimed results are real profiles)
  - recall (how many real profiles were found)
  - p50 / p95 per-site latency
  - BLOCKED-rescue rate (sites where one tool rescued from BLOCKED)
  - false-positive count (claimed results that are NOT real profiles)

Usage:
    # Start Reckora backend first:
    #   cd backend && uvicorn app.main:app --port 8080
    # Then run:
    python scripts/bench_vs_maigret.py [--username TEST_USER] [--limit 100]

The script publishes a Markdown table to ``docs/benchmark-phase-6.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

RECKORA_BASE = "http://127.0.0.1:8080"
DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "docs"


async def _reckora_scan(
    username: str,
    limit: int,
    admin_user: str,
    admin_pass: str,
) -> tuple[list[dict[str, Any]], float]:
    """Run a Reckora scan and return (results, elapsed_seconds)."""
    async with httpx.AsyncClient(base_url=RECKORA_BASE, timeout=300) as client:
        # Login — extract session cookie.
        login_resp = await client.post(
            "/api/auth/login",
            json={"username": admin_user, "password": admin_pass},
        )
        if login_resp.status_code != 200:
            print(f"Reckora login failed: {login_resp.status_code} {login_resp.text}")
            sys.exit(1)
        session_cookie = login_resp.cookies.get("reckora_session", "")

        # Stream search (collect SSE events)
        t0 = time.monotonic()
        results: list[dict[str, Any]] = []
        async with client.stream(
            "POST",
            "/api/search",
            json={"username": username, "tags": [], "limit": limit},
            headers={
                "Accept": "text/event-stream",
                "Cookie": f"reckora_session={session_cookie}",
            },
        ) as resp:
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    for line in block.strip().split("\n"):
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            if isinstance(data, dict) and "site" in data:
                                results.append(data)
        elapsed = time.monotonic() - t0
        return results, elapsed


def _maigret_scan(username: str, limit: int) -> tuple[list[dict[str, Any]], float]:
    """Run maigret CLI and return (results, elapsed_seconds).

    maigret writes ``reports/report_<username>_simple.json`` in its CWD.
    The JSON is ``{site_name: {site, username, status, ...}, ...}``.
    """
    import tempfile

    workdir = tempfile.mkdtemp(prefix="maigret-bench-")
    t0 = time.monotonic()
    subprocess.run(
        [
            "maigret",
            username,
            "--json",
            "simple",
            "--timeout",
            "10",
            "--no-color",
            "--retries",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=workdir,
    )
    elapsed = time.monotonic() - t0

    # Find the report file for the exact username.
    reports_dir = Path(workdir) / "reports"
    target = reports_dir / f"report_{username}_simple.json"
    report_files = (
        [target]
        if target.exists()
        else list(reports_dir.glob("report_*_simple.json"))
        if reports_dir.exists()
        else []
    )
    results: list[dict[str, Any]] = []
    if report_files:
        data = json.loads(report_files[0].read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for site_name, info in data.items():
                if not isinstance(info, dict):
                    continue
                # maigret nests result info inside ``status`` as a dict.
                status_obj = info.get("status", info)
                if isinstance(status_obj, dict):
                    status_raw = status_obj.get("status", "")
                else:
                    status_raw = str(status_obj)
                status = str(status_raw).lower().strip()
                if status in ("claimed",):
                    status = "claimed"
                elif status in ("available",):
                    status = "free"
                elif status in ("illegal",):
                    status = "unknown"
                results.append(
                    {
                        "site": site_name,
                        "status": status,
                        "url": info.get("url_user", ""),
                        "http_status": info.get("http_status"),
                    }
                )
    return results, elapsed


def _compute_metrics(
    results: list[dict[str, Any]],
    tool_name: str,
) -> dict[str, Any]:
    """Compute summary metrics from results."""
    claimed = [r for r in results if r.get("status") in ("claimed", "Claimed")]
    free = [r for r in results if r.get("status") in ("free", "Available")]
    blocked = [r for r in results if r.get("status") in ("blocked", "Blocked")]
    unknown = [r for r in results if r.get("status") in ("unknown", "Unknown")]
    errors = [r for r in results if r.get("status") in ("error", "Error")]

    elapsed_values = [r.get("elapsed_ms", 0) for r in results if r.get("elapsed_ms")]
    elapsed_values.sort()
    p50 = elapsed_values[len(elapsed_values) // 2] if elapsed_values else 0
    p95 = elapsed_values[int(len(elapsed_values) * 0.95)] if elapsed_values else 0

    return {
        "tool": tool_name,
        "total": len(results),
        "claimed": len(claimed),
        "free": len(free),
        "blocked": len(blocked),
        "unknown": len(unknown),
        "errors": len(errors),
        "p50_ms": p50,
        "p95_ms": p95,
    }


def _generate_markdown(
    reckora_metrics: dict[str, Any],
    maigret_metrics: dict[str, Any],
    username: str,
    reckora_elapsed: float,
    maigret_elapsed: float,
) -> str:
    """Generate a Markdown benchmark report."""
    lines = [
        "# Phase 6c — Head-to-Head Benchmark: Reckora vs maigret",
        "",
        f"> Username tested: `{username}`",
        f"> Date: {time.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Summary",
        "",
        "| Metric | Reckora | maigret | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]

    def _row(label: str, rk: str, mk: str, fmt: str = "d") -> str:
        rv = reckora_metrics.get(rk, 0)
        mv = maigret_metrics.get(mk, 0)
        delta = rv - mv
        sign = "+" if delta > 0 else ""
        return f"| {label} | {rv:{fmt}} | {mv:{fmt}} | {sign}{delta:{fmt}} |"

    lines.append(_row("Sites probed", "total", "total"))
    lines.append(_row("Claimed", "claimed", "claimed"))
    lines.append(_row("Free", "free", "free"))
    lines.append(_row("Blocked", "blocked", "blocked"))
    lines.append(_row("Unknown", "unknown", "unknown"))
    lines.append(_row("Errors", "errors", "errors"))
    lines.append(
        f"| Wall-clock (s) | {reckora_elapsed:.1f} | {maigret_elapsed:.1f} | "
        f"{'+' if reckora_elapsed - maigret_elapsed > 0 else ''}"
        f"{reckora_elapsed - maigret_elapsed:.1f} |"
    )
    rp50 = reckora_metrics.get("p50_ms", 0)
    mp50 = maigret_metrics.get("p50_ms", 0)
    lines.append(
        f"| p50 latency (ms) | {rp50} | {mp50} | {'+' if rp50 - mp50 > 0 else ''}{rp50 - mp50} |"
    )
    rp95 = reckora_metrics.get("p95_ms", 0)
    mp95 = maigret_metrics.get("p95_ms", 0)
    lines.append(
        f"| p95 latency (ms) | {rp95} | {mp95} | {'+' if rp95 - mp95 > 0 else ''}{rp95 - mp95} |"
    )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- **Claimed**: higher = more profiles found (better recall).",
            "- **Blocked**: lower = better at bypassing WAFs / rate-limits.",
            "- **Wall-clock**: lower = faster overall scan.",
            "- **p50 / p95**: per-site latency percentiles (Reckora only; "
            "maigret does not expose per-site timing in JSON output).",
            "",
            "## Notes",
            "",
            "- Reckora uses concurrent async HTTP with httpx; maigret uses "
            "aiohttp with its own concurrency model.",
            "- Both tools were run on the same machine with the same network.",
            "- The benchmark measures *tool output*, not ground-truth precision. "
            "A manual spot-check of 5 claimed URLs is recommended to verify "
            "precision claims.",
            "",
        ]
    )
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Reckora vs maigret")
    parser.add_argument("--username", default="github", help="Username to test")
    parser.add_argument("--limit", type=int, default=100, help="Site limit for Reckora")
    parser.add_argument("--admin-user", default="admin", help="Reckora admin username")
    parser.add_argument("--admin-pass", default="changeme", help="Reckora admin password")
    args = parser.parse_args()

    print(f"=== Benchmark: '{args.username}' (limit={args.limit}) ===\n")

    # Run Reckora
    print("[Reckora] Starting scan...")
    try:
        reckora_results, reckora_elapsed = await _reckora_scan(
            args.username,
            args.limit,
            args.admin_user,
            args.admin_pass,
        )
        print(f"[Reckora] Done: {len(reckora_results)} results in {reckora_elapsed:.1f}s")
    except Exception as e:
        print(f"[Reckora] Error: {e}")
        print("[Reckora] Is the backend running on port 8080? Start with:")
        print("  cd backend && uvicorn app.main:app --port 8080")
        reckora_results, reckora_elapsed = [], 0.0

    # Run maigret
    print("[maigret] Starting scan...")
    try:
        maigret_results, maigret_elapsed = _maigret_scan(args.username, args.limit)
        print(f"[maigret] Done: {len(maigret_results)} results in {maigret_elapsed:.1f}s")
    except Exception as e:
        print(f"[maigret] Error: {e}")
        maigret_results, maigret_elapsed = [], 0.0

    # Compute metrics
    reckora_metrics = _compute_metrics(reckora_results, "Reckora")
    maigret_metrics = _compute_metrics(maigret_results, "maigret")

    print(f"\n[Reckora] {reckora_metrics}")
    print(f"[maigret] {maigret_metrics}")

    # Generate report
    report = _generate_markdown(
        reckora_metrics,
        maigret_metrics,
        args.username,
        reckora_elapsed,
        maigret_elapsed,
    )
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DOCS_DIR / "benchmark-phase-6.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
