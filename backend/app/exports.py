"""Phase 5b + 6d + 10h: scan-history export formatters.

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
  * ``pdf``  — Phase 6d: server-side PDF via ``weasyprint``.  Falls back
    to ``html`` when weasyprint is not installed (optional ``[pdf]``
    extra).
  * ``xmind`` — Phase 10h: XMind8-compatible mind-map (zip of
    ``content.xml`` + ``meta.xml`` + ``META-INF/manifest.xml``) so an
    operator can drop the dossier into XMind, FreeMind, or any
    XMind-compatible viewer for branching investigation work. Built
    with stdlib ``zipfile`` + ``xml.etree`` only — no new deps.
  * ``d3`` — Phase 10h: self-contained HTML with an embedded D3.js
    force-directed graph of the CLAIMED-hit topology (matches the
    SPA's graph view but as a static, shareable artefact). The D3
    runtime is loaded from a public CDN; the graph data is embedded
    inline so the file works offline once cached.
  * ``txt`` — Phase 10h: plain-text dossier matching maigret's
    ``--txt`` output (header line + per-CLAIMED block + footer
    summary) so existing maigret tooling can consume Reckora reports
    interchangeably.

The endpoints live in ``main.py`` (``GET
/api/history/<id>/export.<format>``) and stream a single
``Content-Disposition: attachment`` response.
"""

from __future__ import annotations

import csv
import html
import io
import json
import zipfile
from collections.abc import Iterable
from typing import Any, Final, Literal

from .history import ScanSummary

ExportFormat = Literal["csv", "json", "markdown", "html", "pdf", "xmind", "d3", "txt"]

ALLOWED_FORMATS: Final[frozenset[str]] = frozenset(
    {"csv", "json", "markdown", "html", "pdf", "xmind", "d3", "txt"}
)

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
    "pdf": "application/pdf",
    # XMind reserved its own vendor MIME type but most XMind builds also
    # accept the generic zip MIME — we emit the vendor type so a
    # double-click on the file launches XMind on systems that have it
    # registered as the default handler.
    "xmind": "application/vnd.xmind.workbook",
    # The D3 graph export is just an HTML document; serve it with the
    # standard ``text/html`` MIME so a browser opens it inline if the
    # operator clicks the download link.
    "d3": "text/html; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}

# Filename suffix per format.
EXTENSIONS: Final[dict[str, str]] = {
    "csv": "csv",
    "json": "json",
    "markdown": "md",
    "html": "html",
    "pdf": "pdf",
    "xmind": "xmind",
    # D3 graph is an HTML document; the ``.html`` suffix is recognised
    # by every browser. We include the ``-graph`` token in the
    # filename builder below so the operator can tell it apart from the
    # plain ``html`` export.
    "d3": "html",
    "txt": "txt",
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
# PDF (Phase 6d) — optional weasyprint dependency
# ---------------------------------------------------------------------------


def _weasyprint_available() -> bool:
    try:
        import weasyprint as _wp  # noqa: F401

        return True
    except ImportError:
        return False


def to_pdf(scan: ScanSummary, results: list[dict[str, Any]]) -> bytes:
    """Render the HTML report to PDF bytes via weasyprint.

    Falls back to the HTML export (UTF-8 bytes) when weasyprint is not
    installed.
    """
    html_str = to_html(scan, results)
    try:
        import weasyprint

        return weasyprint.HTML(string=html_str).write_pdf()
    except ImportError:
        return html_str.encode("utf-8")


# ---------------------------------------------------------------------------
# TXT (Phase 10h) — plain-text dossier matching maigret's --txt output
# ---------------------------------------------------------------------------


def to_txt(scan: ScanSummary, results: list[dict[str, Any]]) -> str:
    """Render a plain-text dossier of CLAIMED hits + summary footer.

    The format mirrors maigret's ``--txt`` so an analyst already
    familiar with maigret reports can read a Reckora export without
    re-learning the layout. Each CLAIMED hit gets a stanza:

        [+] <Site>: <URL>

    followed by optional ``Confidence``/``ML``/``Parent`` lines when
    those fields are populated. FREE/UNKNOWN/BLOCKED rows are summarised
    in the footer count line — the per-row detail lives in the JSON or
    CSV exports for callers who need it.
    """
    duration_s = max(0, scan.finished_at - scan.started_at)
    lines: list[str] = []
    lines.append(f"Reckora scan #{scan.id} — {scan.username}")
    lines.append("=" * 60)
    lines.append(f"Mode:         {scan.mode}")
    lines.append(f"Member:       {scan.member}")
    lines.append(f"Sites probed: {scan.site_count}")
    lines.append(f"Started:      {scan.started_at}")
    lines.append(f"Finished:     {scan.finished_at} ({duration_s}s)")
    lines.append("")
    claimed = [r for r in results if r.get("status") == "claimed"]
    lines.append(f"[+] Total Sites Checked: {scan.site_count}")
    lines.append(f"[+] Total Sites with CLAIMED profile: {len(claimed)}")
    lines.append("")
    if claimed:
        for r in claimed:
            site = str(r.get("site") or "")
            url = str(r.get("url") or "")
            lines.append(f"[+] {site}: {url}")
            conf = r.get("confidence")
            if conf is not None:
                lines.append(f"    Confidence: {conf}")
            ml = r.get("ml_confidence")
            if ml is not None:
                lines.append(f"    ML score:   {ml}")
            parent = r.get("parent_username")
            if parent:
                lines.append(f"    Parent:     {parent}")
            lines.append("")
    else:
        lines.append("[-] No CLAIMED hits — every probed site responded FREE or UNKNOWN.")
        lines.append("")
    lines.append("-" * 60)
    lines.append(
        f"Outcome: {scan.claimed} claimed · {scan.free} free · "
        f"{scan.blocked} blocked · {scan.unknown} unknown · "
        f"{scan.error_count} errors"
    )
    lines.append("Generated by Reckora — https://github.com/yowanda/Reckora")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# D3 graph HTML (Phase 10h) — self-contained force-directed graph
# ---------------------------------------------------------------------------


_D3_GRAPH_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<title>Reckora graph #{scan_id} — {username}</title>
<style>
  body {{ margin: 0; font: 14px/1.4 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; color: #18181b; background: #fafafa; }}
  header {{ padding: 12px 20px; background: #ffffff; border-bottom: 1px solid #e4e4e7; }}
  header h1 {{ margin: 0; font-size: 16px; }}
  header p {{ margin: 4px 0 0; color: #71717a; font-size: 12px; }}
  #graph {{ width: 100%; height: calc(100vh - 60px); display: block; }}
  .node circle {{ stroke: #ffffff; stroke-width: 1.5px; }}
  .node text {{ font-size: 10px; fill: #18181b; pointer-events: none; }}
  .link {{ stroke: #d4d4d8; stroke-opacity: 0.8; }}
  .legend {{ position: absolute; right: 20px; top: 80px; background: #ffffff; border: 1px solid #e4e4e7; border-radius: 6px; padding: 8px 12px; font-size: 11px; }}
  .legend dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }}
</style>
</head>
<body>
<header>
  <h1>Reckora graph #{scan_id} — <code>{username}</code></h1>
  <p>{node_count} nodes · {edge_count} edges · generated {finished_at}</p>
</header>
<svg id='graph'></svg>
<div class='legend'>
  <div><span class='dot' style='background:#0e7490'></span> CLAIMED hit</div>
  <div><span class='dot' style='background:#a855f7'></span> Identity</div>
</div>
<script id='graph-data' type='application/json'>{graph_json}</script>
<script src='https://cdn.jsdelivr.net/npm/d3@7.8.5/dist/d3.min.js'></script>
<script>
(function () {{
  var data = JSON.parse(document.getElementById('graph-data').textContent);
  var svg = d3.select('#graph');
  var width = window.innerWidth;
  var height = window.innerHeight - 60;
  svg.attr('viewBox', [0, 0, width, height]);
  var sim = d3.forceSimulation(data.nodes)
      .force('link', d3.forceLink(data.links).id(function (d) {{ return d.id; }}).distance(80))
      .force('charge', d3.forceManyBody().strength(-160))
      .force('center', d3.forceCenter(width / 2, height / 2));
  var link = svg.append('g')
      .selectAll('line')
      .data(data.links)
      .enter().append('line')
      .attr('class', 'link')
      .attr('stroke-width', 1.2);
  var node = svg.append('g')
      .selectAll('g')
      .data(data.nodes)
      .enter().append('g')
      .attr('class', 'node');
  node.append('circle')
      .attr('r', function (d) {{ return d.kind === 'identity' ? 8 : 5; }})
      .attr('fill', function (d) {{ return d.kind === 'identity' ? '#a855f7' : '#0e7490'; }});
  node.append('text')
      .attr('x', 8)
      .attr('y', 3)
      .text(function (d) {{ return d.label; }});
  sim.on('tick', function () {{
    link
        .attr('x1', function (d) {{ return d.source.x; }})
        .attr('y1', function (d) {{ return d.source.y; }})
        .attr('x2', function (d) {{ return d.target.x; }})
        .attr('y2', function (d) {{ return d.target.y; }});
    node.attr('transform', function (d) {{ return 'translate(' + d.x + ',' + d.y + ')'; }});
  }});
}})();
</script>
</body>
</html>
"""


def _d3_graph_payload(scan: ScanSummary, results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the ``{nodes, links}`` payload embedded into the D3 HTML.

    Identity nodes (one per distinct ``parent_username`` plus the
    primary ``scan.username``) are rendered as purple bubbles; CLAIMED
    sites attach to the identity that spawned them so a multi-hop
    Phase 5d/10d graph stays legible.
    """
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _add_identity(username: str) -> str:
        node_id = f"identity::{username}"
        if node_id not in seen_ids:
            seen_ids.add(node_id)
            nodes.append({"id": node_id, "label": username, "kind": "identity"})
        return node_id

    primary_id = _add_identity(scan.username)
    for r in results:
        if r.get("status") != "claimed":
            continue
        site = str(r.get("site") or "")
        if not site:
            continue
        parent = r.get("parent_username")
        identity_id = _add_identity(str(parent)) if parent else primary_id
        site_id = f"site::{identity_id}::{site}"
        if site_id in seen_ids:
            continue
        seen_ids.add(site_id)
        nodes.append(
            {
                "id": site_id,
                "label": site,
                "kind": "site",
                "url": str(r.get("url") or ""),
                "confidence": r.get("confidence"),
                "ml_confidence": r.get("ml_confidence"),
            }
        )
        links.append({"source": identity_id, "target": site_id})
    return {"nodes": nodes, "links": links}


def to_d3_html(scan: ScanSummary, results: list[dict[str, Any]]) -> str:
    """Self-contained HTML page that renders the CLAIMED-hit graph in D3.

    The graph data is JSON-encoded into a ``<script type=application/json>``
    block (the safest way to embed structured data without needing a
    JS-string escape pass), and the D3 v7 runtime is loaded from
    jsDelivr. Once the browser has cached the runtime once, the page
    works fully offline on subsequent loads.
    """
    payload = _d3_graph_payload(scan, results)
    # ``</script>`` inside the JSON would terminate the surrounding script
    # tag — escape the slash so the embedded JSON is always parseable
    # without ever being executed as JavaScript.
    graph_json = (
        json.dumps(payload, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return _D3_GRAPH_HTML_TEMPLATE.format(
        scan_id=scan.id,
        username=html.escape(scan.username, quote=True),
        node_count=len(payload["nodes"]),
        edge_count=len(payload["links"]),
        finished_at=scan.finished_at,
        graph_json=graph_json,
    )


# ---------------------------------------------------------------------------
# XMind8 (Phase 10h) — pure stdlib zip + xml.etree
# ---------------------------------------------------------------------------


# XMind8 file format references:
#   https://github.com/xmindltd/xmind/wiki/XMindFileFormat
# A valid workbook is a zip containing at minimum ``content.xml`` (the
# topic tree), ``meta.xml`` (workbook metadata), and
# ``META-INF/manifest.xml`` (file index). XMind 2020+ also opens these
# files when bundled as ``.xmind`` archives.
_XMIND_CONTENT_NS = "urn:xmind:xmap:xmlns:content:2.0"
_XMIND_META_NS = "urn:xmind:xmap:xmlns:meta:2.0"
_XMIND_MANIFEST_NS = "urn:xmind:xmap:xmlns:manifest:1.0"


def _xmind_content_xml(scan: ScanSummary, results: list[dict[str, Any]]) -> bytes:
    """Build the workbook's ``content.xml`` (topic tree).

    Layout: a single sheet whose root topic is the scan target; each
    distinct ``parent_username`` becomes a sub-topic; CLAIMED hits hang
    off their parent identity. FREE/UNKNOWN rows are deliberately
    omitted — the mind-map is a *dossier* of confirmed signal, not a
    raw probe log (use the CSV/JSON export for that).
    """
    import xml.etree.ElementTree as ET

    ET.register_namespace("", _XMIND_CONTENT_NS)
    root = ET.Element(f"{{{_XMIND_CONTENT_NS}}}xmap-content", attrib={"version": "2.0"})
    sheet = ET.SubElement(
        root,
        f"{{{_XMIND_CONTENT_NS}}}sheet",
        attrib={"id": f"reckora-sheet-{scan.id}"},
    )
    central = ET.SubElement(
        sheet,
        f"{{{_XMIND_CONTENT_NS}}}topic",
        attrib={"id": f"reckora-root-{scan.id}"},
    )
    title = ET.SubElement(central, f"{{{_XMIND_CONTENT_NS}}}title")
    title.text = f"{scan.username} — Reckora scan #{scan.id}"
    notes = ET.SubElement(central, f"{{{_XMIND_CONTENT_NS}}}notes")
    note_plain = ET.SubElement(notes, f"{{{_XMIND_CONTENT_NS}}}plain")
    note_plain.text = (
        f"Mode: {scan.mode}\nSites probed: {scan.site_count}\n"
        f"Outcome: {scan.claimed} claimed / {scan.free} free / "
        f"{scan.blocked} blocked / {scan.unknown} unknown / "
        f"{scan.error_count} errors"
    )

    claimed = [r for r in results if r.get("status") == "claimed"]
    if claimed:
        children = ET.SubElement(central, f"{{{_XMIND_CONTENT_NS}}}children")
        topics = ET.SubElement(
            children,
            f"{{{_XMIND_CONTENT_NS}}}topics",
            attrib={"type": "attached"},
        )
        # Group claimed hits by parent_username so a Phase 5d/10d
        # secondary graph reads as a tree, not a flat list.
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in claimed:
            parent_key = str(r.get("parent_username") or scan.username)
            groups.setdefault(parent_key, []).append(r)
        for idx, (parent_name, rows) in enumerate(sorted(groups.items())):
            parent_topic = ET.SubElement(
                topics,
                f"{{{_XMIND_CONTENT_NS}}}topic",
                attrib={"id": f"reckora-parent-{scan.id}-{idx}"},
            )
            parent_title = ET.SubElement(parent_topic, f"{{{_XMIND_CONTENT_NS}}}title")
            parent_title.text = parent_name
            parent_children = ET.SubElement(parent_topic, f"{{{_XMIND_CONTENT_NS}}}children")
            parent_topics = ET.SubElement(
                parent_children,
                f"{{{_XMIND_CONTENT_NS}}}topics",
                attrib={"type": "attached"},
            )
            for r_idx, r in enumerate(rows):
                site_topic = ET.SubElement(
                    parent_topics,
                    f"{{{_XMIND_CONTENT_NS}}}topic",
                    attrib={"id": f"reckora-site-{scan.id}-{idx}-{r_idx}"},
                )
                site_title = ET.SubElement(site_topic, f"{{{_XMIND_CONTENT_NS}}}title")
                site_title.text = str(r.get("site") or "")
                url = str(r.get("url") or "")
                if url:
                    site_topic.set(
                        f"{{{_XMIND_CONTENT_NS}}}href",
                        url,
                    )
                    site_topic.set("xlink:href", url)
                conf = r.get("confidence")
                ml = r.get("ml_confidence")
                if conf is not None or ml is not None:
                    site_notes = ET.SubElement(site_topic, f"{{{_XMIND_CONTENT_NS}}}notes")
                    site_plain = ET.SubElement(site_notes, f"{{{_XMIND_CONTENT_NS}}}plain")
                    parts: list[str] = []
                    if url:
                        parts.append(f"URL: {url}")
                    if conf is not None:
                        parts.append(f"Confidence: {conf}")
                    if ml is not None:
                        parts.append(f"ML score: {ml}")
                    site_plain.text = "\n".join(parts)
    sheet_title = ET.SubElement(sheet, f"{{{_XMIND_CONTENT_NS}}}title")
    sheet_title.text = "Reckora"
    return _xml_with_decl(root)


def _xmind_meta_xml(scan: ScanSummary) -> bytes:
    import xml.etree.ElementTree as ET

    ET.register_namespace("", _XMIND_META_NS)
    root = ET.Element(f"{{{_XMIND_META_NS}}}meta")
    creator = ET.SubElement(root, f"{{{_XMIND_META_NS}}}Creator")
    name = ET.SubElement(creator, f"{{{_XMIND_META_NS}}}Name")
    name.text = "Reckora"
    version = ET.SubElement(creator, f"{{{_XMIND_META_NS}}}Version")
    version.text = "1"
    create_time = ET.SubElement(root, f"{{{_XMIND_META_NS}}}Create")
    create_time_node = ET.SubElement(create_time, f"{{{_XMIND_META_NS}}}Time")
    create_time_node.text = str(scan.finished_at)
    return _xml_with_decl(root)


def _xmind_manifest_xml() -> bytes:
    import xml.etree.ElementTree as ET

    ET.register_namespace("", _XMIND_MANIFEST_NS)
    root = ET.Element(f"{{{_XMIND_MANIFEST_NS}}}manifest")
    for path, mime in (
        ("content.xml", "text/xml"),
        ("meta.xml", "text/xml"),
        ("META-INF/", ""),
    ):
        ET.SubElement(
            root,
            f"{{{_XMIND_MANIFEST_NS}}}file-entry",
            attrib={"full-path": path, "media-type": mime},
        )
    return _xml_with_decl(root)


def _xml_with_decl(root: Any) -> bytes:
    import xml.etree.ElementTree as ET

    payload = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n' + payload


def to_xmind(scan: ScanSummary, results: list[dict[str, Any]]) -> bytes:
    """Build a minimal XMind8-compatible workbook archive.

    Returns the raw bytes of a zip containing ``content.xml``,
    ``meta.xml``, and ``META-INF/manifest.xml`` — the smallest valid
    set XMind 8 / XMind 2020 will open without complaint.
    """
    buf = io.BytesIO()
    # ZIP_DEFLATED keeps the file small without an extra dep; XMind has
    # accepted DEFLATE since 2014 so this is safe across every supported
    # build.
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.xml", _xmind_content_xml(scan, results))
        zf.writestr("meta.xml", _xmind_meta_xml(scan))
        zf.writestr("META-INF/manifest.xml", _xmind_manifest_xml())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def render(
    fmt: ExportFormat,
    scan: ScanSummary,
    results: list[dict[str, Any]],
) -> tuple[bytes, str, str]:
    """Render ``(scan, results)`` to ``(body, media_type, filename)``.

    Returns bytes so the FastAPI ``Response`` can ship them directly.
    For text formats the bytes are UTF-8 encoded text; for ``pdf`` /
    ``xmind`` the bytes are the raw binary document.
    """
    if fmt == "csv":
        text = to_csv(scan, results)
    elif fmt == "json":
        text = to_json(scan, results)
    elif fmt == "markdown":
        text = to_markdown(scan, results)
    elif fmt == "html":
        text = to_html(scan, results)
    elif fmt == "txt":
        text = to_txt(scan, results)
    elif fmt == "d3":
        text = to_d3_html(scan, results)
    elif fmt == "pdf":
        pdf_bytes = to_pdf(scan, results)
        return pdf_bytes, MEDIA_TYPES["pdf"], filename_for(scan, fmt)
    elif fmt == "xmind":
        xmind_bytes = to_xmind(scan, results)
        return xmind_bytes, MEDIA_TYPES["xmind"], filename_for(scan, fmt)
    else:  # pragma: no cover — guarded by ALLOWED_FORMATS at the route
        raise ValueError(f"unknown export format: {fmt}")
    return text.encode("utf-8"), MEDIA_TYPES[fmt], filename_for(scan, fmt)
