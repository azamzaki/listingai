"""Dashboard badges, filters and a static HTML view."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import quote

from .config import DEFAULT_CONFIG, ExclusiveAgentConfig
from .models import ExclusiveAgentStatus as S, Listing


@dataclass(frozen=True)
class Badge:
    label: str
    priority: int  # 1 = highest
    color: str


BADGES: dict[S, Badge] = {
    S.SEEKING_EXCLUSIVE_AGENT: Badge("Mencari Ejen Eksklusif", 1, "#0a7d38"),
    S.OPEN_TO_AGENT_APPOINTMENT: Badge("Terbuka Melantik Ejen", 2, "#1f6feb"),
    S.UNCERTAIN: Badge("Perlu Semakan", 3, "#b26b00"),
    S.NO_EVIDENCE: Badge("Tiada Bukti", 4, "#6e7781"),
    S.ALREADY_HAS_EXCLUSIVE_AGENT: Badge("Sudah Ada Ejen Eksklusif", 5, "#8250df"),
    S.REJECTS_AGENTS: Badge("Tidak Mahu Ejen", 6, "#cf222e"),
}

FILTER_OPTIONS: list[tuple[str, str]] = [(s.value, BADGES[s].label) for s in sorted(BADGES, key=lambda s: BADGES[s].priority)]


def status_of(listing: Listing) -> S:
    return listing.exclusive_agent.exclusive_agent_status if listing.exclusive_agent else S.NO_EVIDENCE


def badges_for(listing: Listing) -> list[Badge]:
    """Primary status badge, plus "Perlu Semakan" when review is required."""
    status = status_of(listing)
    out = [BADGES[status]]
    if status is not S.UNCERTAIN and listing.exclusive_agent and listing.exclusive_agent.exclusive_agent_review_required:
        out.append(BADGES[S.UNCERTAIN])
    return out


def filter_by_status(listings: Iterable[Listing], statuses: Optional[Iterable[S | str]] = None) -> list[Listing]:
    if not statuses:
        return list(listings)
    wanted = {S(s) for s in statuses}
    return [l for l in listings if status_of(l) in wanted]


def sort_for_dashboard(listings: Iterable[Listing]) -> list[Listing]:
    return sorted(listings, key=lambda l: (BADGES[status_of(l)].priority, -l.base_opportunity_score))


def dashboard_link(listing: Listing, config: ExclusiveAgentConfig = DEFAULT_CONFIG) -> str:
    return f"{config.dashboard_base_url.rstrip('/')}/listings/{quote(listing.id)}"


def render_dashboard_html(listings: Iterable[Listing], config: ExclusiveAgentConfig = DEFAULT_CONFIG) -> str:
    from .pipeline import owner_instruction  # local import avoids a cycle

    rows = []
    for l in sort_for_dashboard(listings):
        ea = l.exclusive_agent
        badges = "".join(
            f"<span class='badge' style='background:{b.color}'>{html.escape(b.label)}</span>" for b in badges_for(l)
        )
        evidence = ""
        if ea and ea.exclusive_agent_evidence:
            src = ea.exclusive_agent_evidence_source.value if ea.exclusive_agent_evidence_source else ""
            evidence = (f"<q>{html.escape(ea.exclusive_agent_evidence)}</q> <small>({html.escape(src)}, "
                        f"{round(ea.exclusive_agent_confidence * 100)}%)</small>")
        warning = owner_instruction(l)
        warning_html = f"<div class='warn'>&#9888; {html.escape(warning)}</div>" if warning else ""
        agent = ""
        if ea and (ea.already_appointed_agent_name or ea.already_appointed_agent_ren):
            agent = html.escape(" ".join(x for x in (ea.already_appointed_agent_name, ea.already_appointed_agent_ren) if x))
        rows.append(
            f"<tr data-status='{status_of(l).value}'><td>{badges}{warning_html}</td>"
            f"<td><a href='{html.escape(l.post_url)}'>{html.escape(l.location)}</a></td>"
            f"<td>{'RM{:,}'.format(l.price) if l.price is not None else '&mdash;'}</td>"
            f"<td>{evidence}</td><td>{agent}</td>"
            f"<td>{html.escape(l.public_contact or '')}</td></tr>"
        )
    options = "".join(
        f"<label><input type='checkbox' value='{v}' checked> {html.escape(label)}</label>" for v, label in FILTER_OPTIONS
    )
    return f"""<!doctype html>
<html lang="ms"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ListingAI Leads</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#fff;color:#1f2328}}
.badge{{display:inline-block;color:#fff;border-radius:10px;padding:2px 8px;margin:0 4px 4px 0;font-size:12px}}
.warn{{color:#cf222e;font-weight:600;font-size:13px}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #d0d7de;padding:6px;text-align:left;vertical-align:top}}
#filters label{{margin-right:12px;white-space:nowrap}}
</style></head><body>
<div id="filters">{options}</div>
<table><thead><tr><th>Status</th><th>Lokasi</th><th>Harga</th><th>Bukti</th><th>Ejen dilantik</th><th>Hubungi</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<script>
document.querySelectorAll('#filters input').forEach(function(box){{
  box.addEventListener('change', function(){{
    var on = Array.from(document.querySelectorAll('#filters input:checked')).map(function(b){{return b.value}});
    document.querySelectorAll('tbody tr').forEach(function(tr){{tr.hidden = on.indexOf(tr.dataset.status) < 0}});
  }});
}});
</script></body></html>"""
