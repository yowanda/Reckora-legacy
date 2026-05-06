"""Phase 5b: scan-history export formatters.

Each renderer takes a ``(ScanSummary, results)`` pair and produces a
single self-contained artefact:

  * ``csv``  — RFC-4180 CSV of (site, status, confidence, ml_confidence,
    url, parent_username). Stable column order so spreadsheet pivots
    don't break across exports.
  * ``json`` — ``{"scan": {...}, "results": [...]}``. Same payload the
    SPA gets from ``GET /api/history/<id>``, just dumped as a file.
  * ``markdown`` — short summary block + a table of CLAIMED hits.
    Suitable for pasting into a ticket or a changelog.
  * ``html`` — full standalone document (inline styles, no external
    assets) — opens directly in a browser, prints cleanly to PDF via
    "File → Print → Save as PDF".

Why no ``reportlab``-rendered PDF? Adding it would pull in a 12 MB
dep tree that the rest of the app doesn't need; the HTML export
already prints to a clean A4 PDF. We can add it in a follow-up if
operators report the print path is too clunky.

The endpoints live in ``main.py`` (``GET
/api/history/<id>/export.<format>``) and stream a single
``Content-Disposition: attachment`` response.
"""

from __future__ import annotations

import csv
import html
import io
import json
from collections.abc import Iterable
from typing import Any, Final, Literal

from .history import ScanSummary

ExportFormat = Literal["csv", "json", "markdown", "html"]

ALLOWED_FORMATS: Final[frozenset[str]] = frozenset({"csv", "json", "markdown", "html"})

# Status label map — kept here so all exports use the same casing.
_STATUS_LABEL: Final[dict[str, str]] = {
    "claimed": "CLAIMED",
    "free": "FREE",
    "blocked": "BLOCKED",
    "unknown": "UNKNOWN",
    "error": "ERROR",
}

# Stable CSV header. Adding a new column is a one-line change here;
# removing or reordering one is a breaking change for downstream
# spreadsheets, so don't.
_CSV_HEADER: Final[tuple[str, ...]] = (
    "site",
    "status",
    "confidence",
    "ml_confidence",
    "url",
    "parent_username",
)

# Media types per format. ``markdown`` deliberately uses ``text/markdown``
# (RFC 7763) rather than ``text/x-markdown`` so curl/wget download it
# straight to a ``.md`` file instead of trying to render.
MEDIA_TYPES: Final[dict[str, str]] = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}

# Filename suffix per format.
EXTENSIONS: Final[dict[str, str]] = {
    "csv": "csv",
    "json": "json",
    "markdown": "md",
    "html": "html",
}


def filename_for(scan: ScanSummary, fmt: ExportFormat) -> str:
    """Build a safe ``reckora-<id>-<username>.<ext>`` filename.

    The username is sanitised because it ends up in a ``Content-Disposition``
    header — drop anything that isn't an allow-listed identifier char.
    """
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in scan.username)
    return f"reckora-{scan.id}-{safe}.{EXTENSIONS[fmt]}"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def to_csv(scan: ScanSummary, results: Iterable[dict[str, Any]]) -> str:
    """Serialise ``results`` to a UTF-8 CSV string with header row."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(_CSV_HEADER)
    for r in results:
        writer.writerow([r.get(k) if r.get(k) is not None else "" for k in _CSV_HEADER])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def to_json(scan: ScanSummary, results: list[dict[str, Any]]) -> str:
    """Serialise the full payload (summary + results) as indented JSON.

    Uses ``ensure_ascii=False`` so non-ASCII usernames serialise as-is
    instead of ``\\uXXXX`` escapes — saves bytes and keeps grep-ability.
    """
    payload = {"scan": scan.to_dict(), "results": results}
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _md_escape_pipe(s: str) -> str:
    """Markdown tables use ``|`` as the column separator; escape any
    occurrence inside a cell."""
    return s.replace("|", "\\|")


def to_markdown(scan: ScanSummary, results: list[dict[str, Any]]) -> str:
    """Compact summary block + a CLAIMED-hits table.

    We deliberately *don't* dump every site — most of them are FREE/UNKNOWN
    and add noise. The CSV/JSON exports carry the full payload for callers
    who need it.
    """
    counts = (
        f"**{scan.claimed}** claimed · {scan.free} free · {scan.blocked} blocked · "
        f"{scan.unknown} unknown · {scan.error_count} errors"
    )
    when = (
        f"started {scan.started_at} · finished {scan.finished_at} "
        f"({(scan.finished_at - scan.started_at):.0f}s)"
    )
    lines: list[str] = []
    lines.append(f"# Reckora scan #{scan.id} — `{scan.username}`")
    lines.append("")
    lines.append(f"- **Mode:** `{scan.mode}`")
    lines.append(f"- **Sites probed:** {scan.site_count}")
    lines.append(f"- **Outcome:** {counts}")
    lines.append(f"- **Timing:** {when}")
    lines.append("")
    claimed = [r for r in results if r.get("status") == "claimed"]
    if not claimed:
        lines.append("_No CLAIMED hits — every site responded FREE or UNKNOWN._")
        lines.append("")
        return "\n".join(lines)
    lines.append("## CLAIMED hits")
    lines.append("")
    lines.append("| Site | URL | Confidence | ML | Parent |")
    lines.append("|---|---|---:|---:|---|")
    for r in claimed:
        site = _md_escape_pipe(str(r.get("site") or ""))
        url = str(r.get("url") or "")
        url_cell = f"[{_md_escape_pipe(url)}]({url})" if url else ""
        conf = "" if r.get("confidence") is None else str(r["confidence"])
        ml = "" if r.get("ml_confidence") is None else str(r["ml_confidence"])
        parent = _md_escape_pipe(str(r.get("parent_username") or ""))
        lines.append(f"| {site} | {url_cell} | {conf} | {ml} | {parent} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML — print-to-PDF friendly
# ---------------------------------------------------------------------------


_HTML_STYLE = """
:root {
  color-scheme: light;
  --fg: #18181b;
  --muted: #71717a;
  --line: #e4e4e7;
  --good: #0e7490;
  --bg: #ffffff;
  --bg-alt: #fafafa;
}
* { box-sizing: border-box; }
body {
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui,
        "Helvetica Neue", Arial, sans-serif;
  color: var(--fg);
  background: var(--bg);
  margin: 0;
  padding: 32px;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 24px 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--line); }
.muted { color: var(--muted); font-size: 12px; }
.summary { margin: 12px 0 0; padding: 12px 16px; background: var(--bg-alt); border: 1px solid var(--line); border-radius: 6px; }
.kvs { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 0; }
.kvs dt { color: var(--muted); font-weight: 500; }
.kvs dd { margin: 0; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { padding: 6px 8px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; text-transform: uppercase; font-size: 11px; letter-spacing: 0.04em; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 11px; font-weight: 500; }
.badge-claimed { background: #ecfeff; color: var(--good); border: 1px solid #cffafe; }
a { color: var(--good); text-decoration: none; }
a:hover { text-decoration: underline; }
footer { margin-top: 32px; color: var(--muted); font-size: 11px; }
@media print {
  body { padding: 0; }
  h2 { page-break-after: avoid; }
  tr { page-break-inside: avoid; }
}
""".strip()


def _h(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def to_html(scan: ScanSummary, results: list[dict[str, Any]]) -> str:
    """Self-contained HTML report (no external assets).

    Renders the summary block + a full results table (every site, not
    just CLAIMED — printed reports often include FREE rows so the
    reader can see what was checked). Browser "Print → Save as PDF"
    produces a clean A4 page.
    """
    counts_row = (
        f"<span class='badge badge-claimed'>{scan.claimed} claimed</span> · "
        f"{scan.free} free · {scan.blocked} blocked · "
        f"{scan.unknown} unknown · {scan.error_count} errors"
    )
    rows: list[str] = []
    for r in results:
        status = str(r.get("status") or "")
        url = str(r.get("url") or "")
        url_cell = f"<a href='{_h(url)}'>{_h(url)}</a>" if url else ""
        conf_cell = "" if r.get("confidence") is None else _h(r["confidence"])
        ml_cell = "" if r.get("ml_confidence") is None else _h(r["ml_confidence"])
        parent_cell = _h(r.get("parent_username") or "")
        status_label = _STATUS_LABEL.get(status, status.upper())
        if status == "claimed":
            status_cell = f"<span class='badge badge-claimed'>{status_label}</span>"
        else:
            status_cell = _h(status_label)
        rows.append(
            "<tr>"
            f"<td>{_h(r.get('site'))}</td>"
            f"<td>{status_cell}</td>"
            f"<td class='num'>{conf_cell}</td>"
            f"<td class='num'>{ml_cell}</td>"
            f"<td>{url_cell}</td>"
            f"<td>{parent_cell}</td>"
            "</tr>"
        )
    duration_s = scan.finished_at - scan.started_at
    body = f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<title>Reckora scan #{scan.id} — {_h(scan.username)}</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
<h1>Reckora scan #{scan.id} — <code>{_h(scan.username)}</code></h1>
<p class='muted'>Generated for member <strong>{_h(scan.member)}</strong></p>
<section class='summary'>
  <dl class='kvs'>
    <dt>Mode</dt><dd>{_h(scan.mode)}</dd>
    <dt>Sites probed</dt><dd>{scan.site_count}</dd>
    <dt>Outcome</dt><dd>{counts_row}</dd>
    <dt>Started</dt><dd>{scan.started_at}</dd>
    <dt>Finished</dt><dd>{scan.finished_at} ({duration_s}s)</dd>
  </dl>
</section>
<h2>Results</h2>
<table>
  <thead><tr>
    <th>Site</th><th>Status</th><th>Conf.</th><th>ML</th><th>URL</th><th>Parent</th>
  </tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
<footer>
  reckora · scan #{scan.id} · {len(results)} rows · use File → Print to save as PDF
</footer>
</body>
</html>
"""
    return body


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def render(
    fmt: ExportFormat,
    scan: ScanSummary,
    results: list[dict[str, Any]],
) -> tuple[bytes, str, str]:
    """Render ``(scan, results)`` to ``(body, media_type, filename)``.

    Returns bytes (UTF-8 encoded) so the FastAPI ``Response`` can ship
    them directly.
    """
    if fmt == "csv":
        text = to_csv(scan, results)
    elif fmt == "json":
        text = to_json(scan, results)
    elif fmt == "markdown":
        text = to_markdown(scan, results)
    elif fmt == "html":
        text = to_html(scan, results)
    else:  # pragma: no cover — guarded by ALLOWED_FORMATS at the route
        raise ValueError(f"unknown export format: {fmt}")
    return text.encode("utf-8"), MEDIA_TYPES[fmt], filename_for(scan, fmt)
