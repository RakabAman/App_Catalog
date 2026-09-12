"""
html_report.py -- one self-contained HTML report renderer, shared by
app_manager.py's reorganize feature and monitor.py's monitor-folder
feature (checkpoint 21).

WHY: both features already wrote a full JSON move-log with every item's
outcome and, for failures, the error string -- but the GUI only ever
showed a message box with bare COUNTS ("Failed: 3"), pointing at that
JSON file for detail. Reading raw JSON to find out what actually failed
and why is a bad end-user experience, especially across a run of
hundreds of items. This renders the exact same information the JSON log
already had into a report that's actually meant to be read: failures
and skips listed FIRST with their reason in plain text, everything else
below it, both in one scrollable/searchable page.

Deliberately dependency-free HTML/CSS/JS (no CDN, no external font, no
build step) -- this app is offline-first end to end; a report that
needs a network connection to render correctly would undercut that.
Every string that came from the filesystem (paths, app names -- always
attacker-uncontrolled here, but still) is HTML-escaped before being
embedded, since this file gets opened directly in a real browser.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ReportRow:
    name: str            # human-readable app/item name (+ version if known)
    source: str
    dest: str
    status_label: str     # short badge text, e.g. "Moved", "Failed", "Skipped"
    status_class: str     # "good" | "warn" | "bad" | "neutral" -- controls badge color
    detail: str = ""      # the reason/error/note -- THE thing the old report was missing


_CSS = """
:root {
  --bg: #0f1115; --panel: #171a21; --border: #2a2f3a; --text: #e6e8eb;
  --muted: #9aa4b2; --good: #2ea043; --good-bg: #12261a; --warn: #d29922;
  --warn-bg: #2b2210; --bad: #f85149; --bad-bg: #2b1315; --accent: #58a6ff;
}
* { box-sizing: border-box; }
body {
  background: var(--bg); color: var(--text); margin: 0; padding: 24px 32px 64px;
  font: 14px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
h1 { margin: 0 0 2px; font-size: 22px; }
.subtitle { color: var(--muted); margin: 0 0 22px; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 28px; }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 18px; min-width: 130px;
}
.card .n { font-size: 26px; font-weight: 700; }
.card .l { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.card.good .n { color: var(--good); } .card.warn .n { color: var(--warn); } .card.bad .n { color: var(--bad); }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
     border-bottom: 1px solid var(--border); padding-bottom: 8px; margin: 30px 0 12px; }
.controls { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
input[type=search] {
  background: var(--panel); border: 1px solid var(--border); color: var(--text);
  border-radius: 6px; padding: 7px 10px; font-size: 13px; min-width: 260px;
}
.toggle { color: var(--muted); font-size: 13px; cursor: pointer; user-select: none; }
.toggle input { margin-right: 5px; }
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255,255,255,0.03); }
.path { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12.5px; color: var(--muted);
        word-break: break-all; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 600; }
.badge.good { background: var(--good-bg); color: var(--good); }
.badge.warn { background: var(--warn-bg); color: var(--warn); }
.badge.bad  { background: var(--bad-bg);  color: var(--bad); }
.badge.neutral { background: #1c2028; color: var(--muted); }
.detail-bad { color: var(--bad); }
.empty { color: var(--muted); padding: 18px; text-align: center; }
.footer { color: var(--muted); font-size: 12px; margin-top: 26px; }
.footer .path { display: inline; }
"""

_JS = """
function filterTable(tableId, inputId) {
  var q = document.getElementById(inputId).value.toLowerCase();
  var rows = document.querySelectorAll('#' + tableId + ' tbody tr');
  rows.forEach(function (r) {
    r.style.display = r.textContent.toLowerCase().indexOf(q) === -1 ? 'none' : '';
  });
}
function toggleSuccess(show) {
  document.querySelectorAll('#all-table tbody tr.ok-row').forEach(function (r) {
    r.style.display = show ? '' : 'none';
  });
}
"""


def _esc(s: Optional[str]) -> str:
    return html.escape(str(s) if s is not None else "", quote=True)


def _row_html(r: ReportRow, extra_class: str = "") -> str:
    return (
        f'<tr class="{extra_class}">'
        f'<td>{_esc(r.name) or "&mdash;"}</td>'
        f'<td><span class="badge {_esc(r.status_class)}">{_esc(r.status_label)}</span></td>'
        f'<td class="path">{_esc(r.source)}</td>'
        f'<td class="path">{_esc(r.dest)}</td>'
        f'<td class="{"detail-bad" if r.status_class == "bad" else ""}">{_esc(r.detail)}</td>'
        f'</tr>'
    )


def render_operation_html_report(
    *, title: str, subtitle: str,
    summary_cards: list[tuple[str, int, str]],   # (label, number, "good"|"warn"|"bad"|"neutral")
    rows: list[ReportRow],
    json_log_path: Optional[str] = None,
) -> str:
    """
    Renders one complete, self-contained HTML document. Returns the HTML
    string -- the caller decides where to write it (see
    generate_reorganize_html_report / monitor.py's equivalent).
    """
    needs_attention = [r for r in rows if r.status_class in ("bad", "warn")]

    cards_html = "".join(
        f'<div class="card {cls}"><div class="n">{n}</div><div class="l">{_esc(label)}</div></div>'
        for label, n, cls in summary_cards
    )

    if needs_attention:
        attention_table = (
            '<table id="attn-table"><thead><tr>'
            '<th>App</th><th>Status</th><th>Source</th><th>Destination</th><th>Reason</th>'
            '</tr></thead><tbody>'
            + "".join(_row_html(r) for r in needs_attention)
            + "</tbody></table>"
        )
    else:
        attention_table = '<div class="empty">Nothing needs attention -- every item succeeded.</div>'

    all_table = (
        '<table id="all-table"><thead><tr>'
        '<th>App</th><th>Status</th><th>Source</th><th>Destination</th><th>Reason</th>'
        '</tr></thead><tbody>'
        + "".join(_row_html(r, extra_class=("" if r.status_class in ("bad", "warn") else "ok-row"))
                   for r in rows)
        + "</tbody></table>"
        if rows else '<div class="empty">No items were processed.</div>'
    )

    log_line = (
        f'<div class="footer">Full machine-readable JSON log: <span class="path">{_esc(json_log_path)}</span></div>'
        if json_log_path else ""
    )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>{_esc(title)}</h1>
<p class="subtitle">{subtitle} &middot; generated {generated}</p>

<div class="cards">{cards_html}</div>

<h2>Needs attention ({len(needs_attention)})</h2>
<div class="controls">
  <input type="search" id="attn-search" placeholder="Filter by app name or path..."
         oninput="filterTable('attn-table','attn-search')">
</div>
{attention_table}

<h2>Everything ({len(rows)})</h2>
<div class="controls">
  <input type="search" id="all-search" placeholder="Filter by app name or path..."
         oninput="filterTable('all-table','all-search')">
  <label class="toggle"><input type="checkbox" checked onchange="toggleSuccess(this.checked)"> Show successful items</label>
</div>
{all_table}

{log_line}
<script>{_JS}</script>
</body>
</html>
"""
