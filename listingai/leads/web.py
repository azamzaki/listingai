"""Owner Leads page: tabs, rejection reasons, run button and the per-stage run log."""

from __future__ import annotations

import html
import json
import threading
from typing import Callable, Optional

from . import db
from .pipeline import RunOptions, run_pipeline

_e = html.escape

TAB_LABELS = [
    ("new", "New Owner Leads"),
    ("priority", "High Priority"),
    ("reviewed", "Reviewed"),
    ("rejected_agent", "Rejected Agent"),
    ("rejected_filter", "Filtered out"),
]

CSS = """<style>
.tabs{display:flex;flex-wrap:wrap;gap:6px}
.tabs a{padding:8px 12px;border-radius:9px;border:1px solid var(--line);background:var(--surface);color:var(--ink);text-decoration:none;font-weight:600;font-size:14px}
.tabs a.on{background:var(--accent);border-color:var(--accent);color:var(--surface)}
.tabs .n{font:600 12px var(--mono);margin-left:6px;opacity:.8}
.lead-row{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px 16px}
.lead-row h3{margin:0;font:700 16px/1.3 var(--display)}
.chips2{display:flex;flex-wrap:wrap;gap:6px;font-size:12px}
.chip2{padding:2px 9px;border-radius:999px;border:1px solid var(--line);color:var(--ink-2)}
.chip2.vh{background:var(--good);color:var(--surface);border-color:transparent}
.chip2.h{background:var(--accent-soft);color:var(--accent);border-color:transparent;font-weight:600}
.chip2.m{background:var(--review-bg);color:var(--review);border-color:transparent}
.chip2.l{background:var(--rejects-bg);color:var(--rejects);border-color:transparent}
.reason{color:var(--rejects);font-weight:600;font-size:14px}
.desc{color:var(--ink-2);font-size:14px;max-width:80ch;overflow-wrap:anywhere}
.scores{display:flex;gap:14px;text-align:right}
.scores b{display:block;font:700 22px var(--display);font-variant-numeric:tabular-nums}
.scores span{font-size:11px;color:var(--ink-3)}
.why{font-size:12.5px;color:var(--ink-3)}
.runlog{font:12.5px/1.5 var(--mono);background:var(--surface-2);border-radius:8px;padding:10px 12px;max-height:420px;overflow:auto;white-space:pre-wrap}
.runlog .error{color:var(--rejects);font-weight:600}
.runlog .warn{color:var(--review)}
.result{font-weight:600;padding:10px 12px;border-radius:8px}
.result.ok{background:var(--accent-soft);color:var(--accent)}
.result.bad{background:var(--rejects-bg);color:var(--rejects)}
table.runs{border-collapse:collapse;width:100%;font-size:13px}
table.runs td,table.runs th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
@media (max-width:720px){.lead-row{grid-template-columns:1fr}.scores{text-align:left}}
</style>"""


class LeadsRunner:
    """Runs the pipeline in a background thread so the page stays responsive."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.last_result: Optional[dict] = None

    def start(self, opts: Optional[RunOptions] = None, on_done: Optional[Callable[[dict], None]] = None) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True

        def work():
            try:
                self.last_result = run_pipeline(opts or RunOptions(), printer=lambda line: print(f"[leads] {line}"))
            except Exception as e:  # never let the thread die silently
                self.last_result = {"status": "failed", "diagnosis": f"Pipeline crashed: {e!r}"}
                print(f"[leads] Pipeline crashed: {e!r}")
            finally:
                self.running = False
                if on_done:
                    on_done(self.last_result)

        threading.Thread(target=work, daemon=True).start()
        return True


def _level_chip(level: str, score: int) -> str:
    cls = {"VERY HIGH": "vh", "HIGH": "h", "MEDIUM": "m", "LOW": "l"}.get(level, "")
    label = {"MEDIUM": "MEDIUM · check advertiser"}.get(level, level)
    return f'<span class="chip2 {cls}">Owner {label} · {score}</span>'


def _row(l: dict, tab: str, csrf: str) -> str:
    price = f"RM{l['price']:,}" if l.get("price") else "No price shown"
    posted = (l.get("posted_at") or "")[:10] or (l.get("posted_text") or "date not shown")
    chips = [_level_chip(l.get("owner_level") or "", l.get("owner_score") or 0)]
    for v in (l.get("property_type"), l.get("state"), l.get("advertiser_type") and f"Advertiser: {l['advertiser_type']}"):
        if v:
            chips.append(f'<span class="chip2">{_e(v)}</span>')
    reason = f'<div class="reason">{_e(l["rejection_reason"])}</div>' if l.get("rejection_reason") else ""
    indicators = ""
    if l.get("agent_indicators"):
        indicators = f'<div class="why">Agent signals: {_e("; ".join(l["agent_indicators"]))}</div>'
    owner = f'<div class="why">Owner wording: {_e(", ".join(l["owner_wording"]))}</div>' if l.get("owner_wording") else ""
    opp = (f'<div class="why">Opportunity: {_e("; ".join(l["opportunity_reasons"]))}</div>'
           if l.get("opportunity_reasons") else "")
    desc = (l.get("description") or "").strip()
    desc_html = f'<div class="desc">{_e(desc[:360])}{"…" if len(desc) > 360 else ""}</div>' if desc else ""

    def action(status: str, label: str, danger: bool = False) -> str:
        return (f'<form class="inline" method="post" action="/leads/status"><input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="id" value="{l["id"]}"><input type="hidden" name="status" value="{status}">'
                f'<input type="hidden" name="tab" value="{tab}">'
                f'<button class="b{" danger" if danger else ""}" type="submit">{label}</button></form>')

    if tab in ("new", "priority"):
        actions = action("reviewed", "Mark reviewed") + action("rejected_agent", "It's an agent", danger=True)
    elif tab == "reviewed":
        actions = action("new", "Move back to New")
    else:
        actions = action("new", "Not an agent / restore")
    scores = ""
    if tab in ("new", "priority", "reviewed"):
        scores = (f'<div class="scores"><div><b>{l.get("owner_score") or 0}</b><span>Owner confidence</span></div>'
                  f'<div><b>{l.get("opportunity_score") or 0}</b><span>Opportunity</span></div></div>')
    return f"""<article class="lead-row">
  <div style="display:flex;flex-direction:column;gap:6px;min-width:0">
    <div class="chips2">{''.join(chips)}</div>
    <h3>{_e(l.get('title') or '')}</h3>
    <div class="muted">{_e(price)} · {_e(l.get('location') or 'location not shown')} · posted {_e(posted)} · Mudah #{_e(l.get('source_id') or '')}</div>
    {reason}{indicators}{owner}{opp}{desc_html}
    <div class="row"><a class="b" href="{_e(l['url'])}" target="_blank" rel="noopener">Open listing ↗</a>{actions}</div>
  </div>
  {scores}
</article>"""


def leads_page(tab: str, csrf: str, runner: LeadsRunner, auto_hours: int) -> str:
    tab = tab if tab in dict(TAB_LABELS) else "new"
    counts = db.tab_counts()
    tabs = "".join(f'<a href="/leads?tab={k}" class="{"on" if k == tab else ""}">{_e(label)}<span class="n">{counts.get(k, 0)}</span></a>'
                   for k, label in TAB_LABELS)
    rows = db.list_leads(tab)
    if rows:
        body = "".join(_row(l, tab, csrf) for l in rows)
    else:
        body = {"new": "No owner leads yet. Run the search below and read its log if it finds nothing.",
                "priority": "No high-priority leads: these need owner confidence ≥ 80 and opportunity ≥ 60.",
                "reviewed": "Nothing reviewed yet.",
                "rejected_agent": "No listings rejected as agents.",
                "rejected_filter": "No listings filtered out."}[tab]
        body = f'<p class="muted">{_e(body)}</p>'

    runs = db.recent_runs(8)
    status_line = ""
    if runner.running:
        status_line = '<div class="result ok">A search is running now. Refresh this page in a minute to see the results.</div>'
    elif runs:
        r = runs[0]
        status_line = f'<div class="result {"ok" if r["status"] == "ok" else "bad"}">Last run #{r["id"]} ({_e((r["started_at"] or "")[:16].replace("T", " "))} UTC): {_e(r["diagnosis"] or r["status"])}</div>'
    run_rows = "".join(
        f'<tr><td><a href="/leads/run?id={r["id"]}">#{r["id"]}</a></td><td>{_e((r["started_at"] or "")[:16].replace("T", " "))}</td>'
        f'<td>{_e(r["status"] or "")}</td><td class="num">{r["counts"].get("pages_fetched", 0)}</td>'
        f'<td class="num">{r["counts"].get("listings_parsed", 0)}</td><td class="num">{r["counts"].get("passed_filter", 0)}</td>'
        f'<td class="num">{r["counts"].get("new_owner_leads", 0)}</td><td>{_e(r["diagnosis"] or "")}</td></tr>'
        for r in runs)
    every = "".join(f'<option value="{h}"{" selected" if h == auto_hours else ""}>{label}</option>'
                    for h, label in [(0, "Off (run by hand)"), (6, "Every 6 hours"), (12, "Every 12 hours"), (24, "Once a day")])
    return f"""{CSS}
<section class="card">
  <h2>Mudah · Penang &amp; Kedah · residential for sale</h2>
  <p>Reads public Mudah search pages for Penang and Kedah, opens new ads to read their public details, rejects agents
  (REN number, agency name, company advertiser) and keeps owner listings. It obeys Mudah's robots.txt, waits between
  requests, never presses “show number” and never contacts anyone.</p>
  {status_line}
  <div class="row">
    <form class="inline" method="post" action="/leads/run"><input type="hidden" name="csrf" value="{csrf}">
      <button class="b primary" type="submit"{" disabled" if runner.running else ""}>Run search now</button></form>
    <form class="inline" method="post" action="/leads/auto"><input type="hidden" name="csrf" value="{csrf}">
      <label class="muted" for="auto_h">Automatic:</label>
      <select id="auto_h" name="hours" onchange="this.form.submit()">{every}</select></form>
  </div>
</section>
<nav class="tabs" aria-label="Lead status">{tabs}</nav>
<section style="display:flex;flex-direction:column;gap:10px">{body}</section>
<section class="card">
  <h2>Pipeline runs</h2>
  <p class="muted">Each run logs every stage. When a run finds nothing, the last column says where it stopped:
  FETCHING (site blocked, robots.txt, wrong address), PARSING (page format changed), FILTERING, CLASSIFICATION or DATABASE.</p>
  <div style="overflow-x:auto"><table class="runs"><thead><tr><th>Run</th><th>Started (UTC)</th><th>Status</th>
  <th>Pages</th><th>Parsed</th><th>Passed filters</th><th>New owner leads</th><th>Result</th></tr></thead>
  <tbody>{run_rows or '<tr><td colspan="8">No runs yet.</td></tr>'}</tbody></table></div>
</section>"""


def run_log_page(run_id: int) -> str:
    runs = [r for r in db.recent_runs(200) if r["id"] == run_id]
    if not runs:
        return f'{CSS}<p class="muted">Run #{run_id} not found.</p>'
    r = runs[0]
    lines = []
    for ev in db.run_events(run_id):
        extra = ""
        if ev.get("data"):
            data = json.loads(ev["data"])
            extra = "  " + json.dumps(data, ensure_ascii=False)[:300]
        lines.append(f'<span class="{_e(ev["level"])}">{_e(ev["ts"][11:19])} {_e(ev["stage"].upper()):8} '
                     f'{_e(ev["level"].upper()):5} {_e(ev["message"])}{_e(extra)}</span>')
    counts = ", ".join(f"{k}={v}" for k, v in r["counts"].items())
    return f"""{CSS}
<section class="card">
  <h2>Run #{run_id} · {_e(r['status'] or '')}</h2>
  <div class="result {'ok' if r['status'] == 'ok' else 'bad'}">{_e(r['diagnosis'] or '')}</div>
  <p class="muted">Counters: {_e(counts)}</p>
  <div class="runlog">{chr(10).join(lines)}</div>
  <p><a class="b" href="/leads">Back to Owner Leads</a></p>
</section>"""
