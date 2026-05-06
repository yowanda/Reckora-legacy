# Phase 6c — Head-to-Head Benchmark: Reckora vs maigret

> Username tested: `alice`
> Date: 2026-05-06 19:53 UTC

## Summary

| Metric | Reckora | maigret | Delta |
| --- | ---: | ---: | ---: |
| Sites probed | 100 | 272 | -172 |
| Claimed | 27 | 272 | -245 |
| Free | 12 | 0 | +12 |
| Blocked | 30 | 0 | +30 |
| Unknown | 26 | 0 | +26 |
| Errors | 3 | 0 | +3 |
| Wall-clock (s) | 28.6 | 38.0 | -9.4 |
| p50 latency (ms) | 1377 | 0 | +1377 |
| p95 latency (ms) | 3203 | 0 | +3203 |

## Interpretation

- **Claimed**: higher = more profiles found (better recall).
- **Blocked**: lower = better at bypassing WAFs / rate-limits.
- **Wall-clock**: lower = faster overall scan.
- **p50 / p95**: per-site latency percentiles (Reckora only; maigret does not expose per-site timing in JSON output).

## Notes

- Reckora uses concurrent async HTTP with httpx; maigret uses aiohttp with its own concurrency model.
- Both tools were run on the same machine with the same network.
- The benchmark measures *tool output*, not ground-truth precision. A manual spot-check of 5 claimed URLs is recommended to verify precision claims.
