"""Dashboard badges, filters and the HTML dashboard."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional
from urllib.parse import quote

from .config import DEFAULT_CONFIG, ExclusiveAgentConfig
from .models import ExclusiveAgentStatus as S, Listing


@dataclass(frozen=True)
class Badge:
    label: str
    priority: int  # 1 = highest
    color: str
    key: str  # CSS class suffix


BADGES: dict[S, Badge] = {
    S.SEEKING_EXCLUSIVE_AGENT: Badge("Mencari Ejen Eksklusif", 1, "#b7791f", "seeking"),
    S.OPEN_TO_AGENT_APPOINTMENT: Badge("Terbuka Melantik Ejen", 2, "#2f6fb3", "open"),
    S.UNCERTAIN: Badge("Perlu Semakan", 3, "#c2570c", "review"),
    S.NO_EVIDENCE: Badge("Tiada Bukti", 4, "#6b7a74", "none"),
    S.ALREADY_HAS_EXCLUSIVE_AGENT: Badge("Sudah Ada Ejen Eksklusif", 5, "#6d52b5", "appointed"),
    S.REJECTS_AGENTS: Badge("Tidak Mahu Ejen", 6, "#b42318", "rejects"),
}

FILTER_OPTIONS: list[tuple[str, str]] = [(s.value, BADGES[s].label) for s in sorted(BADGES, key=lambda s: BADGES[s].priority)]

_REASON_LABELS = {
    "no_eligible_contact": "No valid public contact",
    "privacy_blocked": "Blocked for privacy",
    "scam_risk_too_high": "Scam risk too high",
    "outside_target_location": "Outside target locations",
    "confirmed_duplicate": "Confirmed duplicate",
    "too_old": "Post is too old",
    "unknown_post_age": "Post date unknown",
    "not_seeking_exclusive_agent": "Owner is not seeking an exclusive agent",
    "confidence_below_threshold": "Confidence below threshold",
    "pending_review": "Waiting for manual review",
    "owner_rejects_agents": "Owner rejects agents",
    "conflicting_signals": "Post has conflicting statements",
    "truncated_text": "Text is cut off",
    "low_source_confidence": "Low OCR / transcript quality",
    "below_threshold": "Confidence below threshold",
    "question": "Phrased as a question",
    "hedged": "Hedged wording",
    "sarcasm": "Possible sarcasm",
    "negated": "Negated wording",
}


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


def _reason_labels(codes: Iterable[str]) -> list[str]:
    out = []
    for code in codes:
        parts = []
        for note in code.split(";"):
            note = note.strip()
            parts.extend(note.split(",") if re.fullmatch(r"[a-z_,]+", note) else [note])
        for part in parts:
            if not part:
                continue
            label = _REASON_LABELS.get(part, part.replace("_", " "))
            if label not in out:
                out.append(label)
    return out


_BY = {"rules": "Phrase rules", "openai": "OpenAI", "rules+openai": "Phrase rules + OpenAI", "manual": "Manually verified"}


def listing_view(listing: Listing, config: ExclusiveAgentConfig = DEFAULT_CONFIG, now: Optional[datetime] = None,
                 campaign_names: Optional[dict[str, str]] = None) -> dict:
    """Everything the dashboard shows for one listing, as plain JSON data."""
    from .alerts import build_subject, exclusive_opportunity_decision
    from .pipeline import can_enter_to_contact, owner_instruction
    from .scoring import compute_opportunity_score

    now = now or datetime.now(timezone.utc)
    ea = listing.exclusive_agent
    status = status_of(listing)
    score = compute_opportunity_score(listing, config, now)
    alert = exclusive_opportunity_decision(listing, config, now)
    allowed, block = can_enter_to_contact(listing)
    age_days = (now - listing.posted_at).days if listing.posted_at else None
    return {
        "id": listing.id,
        "url": listing.post_url,
        "location": listing.location,
        "price": listing.price,
        "caption": listing.caption,
        "phone": listing.public_phone,
        "email": listing.public_email,
        "direct_owner": listing.is_direct_owner,
        "age_days": age_days,
        "status": status.value,
        "badge": BADGES[status].label,
        "badge_key": BADGES[status].key,
        "priority": BADGES[status].priority,
        "classified": ea is not None,
        "confidence": ea.exclusive_agent_confidence if ea else 0,
        "probability": ea.exclusive_agent_probability if ea else 0,
        "evidence": ea.exclusive_agent_evidence if ea else None,
        "source": ea.exclusive_agent_evidence_source.value if ea and ea.exclusive_agent_evidence_source else None,
        "review": bool(ea and ea.exclusive_agent_review_required),
        "review_reasons": _reason_labels([ea.review_reason]) if ea and ea.review_reason else [],
        "preference": ea.agent_contact_preference.value if ea else "unknown",
        "agent_name": ea.already_appointed_agent_name if ea else None,
        "agent_ren": ea.already_appointed_agent_ren if ea else None,
        "detected_at": ea.exclusive_agent_detected_at.isoformat() if ea else None,
        "score": score.score,
        "base_score": score.base_score,
        "adjustment": score.exclusive_agent_adjustment,
        "gate_failures": _reason_labels(score.hard_gate_failures),
        "email_ready": alert.send,
        "email_blockers": _reason_labels(alert.reasons),
        "subject": build_subject(listing, config, now),
        "owner_instruction": owner_instruction(listing),
        "can_contact": allowed,
        "contact_block": _reason_labels([block]) if block else [],
        "classified_by": _BY.get(ea.classified_by, ea.classified_by) if ea else None,
        "campaigns": [(campaign_names or {}).get(c, c) for c in listing.campaign_ids if c in (campaign_names or {})],
        "override": f"{listing.contact_override.user}: {listing.contact_override.reason}" if listing.contact_override else None,
    }


def render_dashboard_html(
    listings: Iterable[Listing],
    config: ExclusiveAgentConfig = DEFAULT_CONFIG,
    now: Optional[datetime] = None,
    source_name: str = "",
    full_document: bool = True,
    nav_html: str = "",
    flash_html: str = "",
    campaign_names: Optional[dict[str, str]] = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    views = [listing_view(l, config, now, campaign_names) for l in sort_for_dashboard(listings)]
    data = json.dumps(views, ensure_ascii=False).replace("</", "<\\/")
    chips = "".join(
        f'<button type="button" class="chip c-{BADGES[S(v)].key}" data-status="{v}" aria-pressed="false">'
        f'<span class="dot"></span><span class="chip-label">{html.escape(label)}</span>'
        f'<span class="chip-count" data-count="{v}">0</span></button>'
        for v, label in FILTER_OPTIONS
    )
    meta = f"{len(views)} listings"
    if source_name:
        meta += f" from {html.escape(source_name)}"
    meta += f" · analysed {now.astimezone().strftime('%d %b %Y, %H:%M')}"
    page = (_TEMPLATE
            .replace("__CHIPS__", chips)
            .replace("__META__", meta)
            .replace("__NAV__", nav_html)
            .replace("__FLASH__", flash_html)
            .replace("__THRESHOLD__", str(round(config.confidence_threshold * 100)))
            .replace("__DATA__", data))
    if not full_document:
        return page
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f"</head><body>\n{page}\n</body></html>")


_TEMPLATE = r"""<title>ListingAI Lead Desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@500;700;800&family=Public+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
:root{
  color-scheme:light;
  --paper:#f3f5f2; --surface:#ffffff; --surface-2:#eaeee9; --line:#d7ddd6;
  --ink:#16211d; --ink-2:#4a5752; --ink-3:#7a8781;
  --accent:#0d6b57; --accent-soft:#dbeee7;
  --gold:#b7791f; --gold-soft:#fbf0dc;
  --seeking:#9a6414; --seeking-bg:#fbefd6;
  --open:#2a62a0; --open-bg:#e2edf9;
  --review:#b0510b; --review-bg:#fdeadb;
  --none:#5f6d68; --none-bg:#e9edeb;
  --appointed:#5f46a6; --appointed-bg:#ece7fa;
  --rejects:#a8201a; --rejects-bg:#fbe4e2;
  --good:#1f7a4d; --bad:#b42318;
  --mark:#ffe29a;
  --shadow:0 1px 2px rgba(22,33,29,.06),0 8px 24px rgba(22,33,29,.06);
  --display:"Schibsted Grotesk",ui-sans-serif,system-ui,sans-serif;
  --body:"Public Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  color-scheme:dark;
  --paper:#0f1513; --surface:#161e1b; --surface-2:#1d2723; --line:#2a3632;
  --ink:#e6ede9; --ink-2:#aab8b2; --ink-3:#7d8b85;
  --accent:#4fc2a2; --accent-soft:#16332b;
  --gold:#e0a84a; --gold-soft:#2f2615;
  --seeking:#f0bd62; --seeking-bg:#33270f;
  --open:#7fb2ec; --open-bg:#15263a;
  --review:#f39a5a; --review-bg:#351f10;
  --none:#a3b1ab; --none-bg:#222c28;
  --appointed:#b7a3f0; --appointed-bg:#251d3b;
  --rejects:#f28b82; --rejects-bg:#3a1714;
  --good:#5fd39a; --bad:#f28b82; --mark:#6b5210;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.25);
}}
:root[data-theme="dark"]{
  color-scheme:dark;
  --paper:#0f1513; --surface:#161e1b; --surface-2:#1d2723; --line:#2a3632;
  --ink:#e6ede9; --ink-2:#aab8b2; --ink-3:#7d8b85;
  --accent:#4fc2a2; --accent-soft:#16332b;
  --gold:#e0a84a; --gold-soft:#2f2615;
  --seeking:#f0bd62; --seeking-bg:#33270f;
  --open:#7fb2ec; --open-bg:#15263a;
  --review:#f39a5a; --review-bg:#351f10;
  --none:#a3b1ab; --none-bg:#222c28;
  --appointed:#b7a3f0; --appointed-bg:#251d3b;
  --rejects:#f28b82; --rejects-bg:#3a1714;
  --good:#5fd39a; --bad:#f28b82; --mark:#6b5210;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.25);
}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 var(--body);-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px;padding-block:28px 64px;display:flex;flex-direction:column;gap:22px}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* Header */
.head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px}
.brand{display:flex;flex-direction:column;gap:4px}
.eyebrow{font:600 11px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}
h1{font:800 clamp(26px,4vw,36px)/1.05 var(--display);letter-spacing:-.02em;margin:0;text-wrap:balance}
.meta{color:var(--ink-3);font-size:13px}
.kpis{display:flex;gap:10px;flex-wrap:wrap}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:10px 14px;min-width:120px;display:flex;flex-direction:column;gap:2px}
.kpi b{font:700 24px/1.1 var(--display);font-variant-numeric:tabular-nums}
.kpi span{font-size:12px;color:var(--ink-3)}
.kpi.hot{background:var(--gold-soft);border-color:transparent}
.kpi.hot b{color:var(--gold)}

/* Filters */
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:7px 12px 7px 10px;cursor:pointer;font-size:13px;font-weight:500;transition:background .15s,border-color .15s}
.chip:hover{border-color:var(--ink-3)}
.chip .dot{width:9px;height:9px;border-radius:50%;background:var(--c)}
.chip-count{font:600 12px var(--mono);color:var(--ink-3);font-variant-numeric:tabular-nums}
.chip[aria-pressed="true"]{background:var(--cbg);border-color:var(--c);color:var(--c)}
.chip[aria-pressed="true"] .chip-count{color:var(--c)}
.chip.all{--c:var(--accent);--cbg:var(--accent-soft)}
.c-seeking{--c:var(--seeking);--cbg:var(--seeking-bg)}
.c-open{--c:var(--open);--cbg:var(--open-bg)}
.c-review{--c:var(--review);--cbg:var(--review-bg)}
.c-none{--c:var(--none);--cbg:var(--none-bg)}
.c-appointed{--c:var(--appointed);--cbg:var(--appointed-bg)}
.c-rejects{--c:var(--rejects);--cbg:var(--rejects-bg)}
.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.search{flex:1 1 260px;position:relative;min-width:0}
.search input{width:100%;padding:10px 12px 10px 36px;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
.search svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);width:16px;height:16px;stroke:var(--ink-3);fill:none}
select{padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--surface);max-width:100%}
.toggle{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--ink-2);cursor:pointer;user-select:none}
.toggle input{accent-color:var(--gold);width:16px;height:16px}

/* Lead list */
.list{display:flex;flex-direction:column;gap:10px}
.lead{display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:18px;align-items:start;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;cursor:pointer;text-align:left;width:100%;transition:border-color .15s,box-shadow .15s}
.lead:hover{border-color:var(--ink-3);box-shadow:var(--shadow)}
.lead.is-seeking{border-color:color-mix(in srgb,var(--seeking) 45%,var(--line));background:linear-gradient(90deg,var(--seeking-bg),var(--surface) 38%)}
.score{width:64px;height:64px;border-radius:50%;display:grid;place-items:center;position:relative;background:conic-gradient(var(--sc) calc(var(--v)*1%),var(--surface-2) 0)}
.score::after{content:"";position:absolute;inset:6px;border-radius:50%;background:var(--surface)}
.score b{position:relative;z-index:1;font:700 20px/1 var(--display);font-variant-numeric:tabular-nums}
.score small{position:absolute;bottom:-18px;left:0;right:0;text-align:center;font:500 10px var(--mono);color:var(--ink-3);letter-spacing:.06em}
.s-hi{--sc:var(--accent)} .s-mid{--sc:var(--gold)} .s-lo{--sc:var(--ink-3)}
.lead-main{display:flex;flex-direction:column;gap:8px;min-width:0}
.tags{display:flex;flex-wrap:wrap;gap:6px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;background:var(--cbg);color:var(--c)}
.badge .dot{width:7px;height:7px;border-radius:50%;background:var(--c)}
.tag{font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--ink-2)}
.tag.mail{border-color:transparent;background:var(--gold);color:var(--paper);font-weight:600}
.lead h3{margin:0;font:700 18px/1.25 var(--display);letter-spacing:-.01em}
.lead h3 .price{font-family:var(--mono);font-weight:600;font-size:16px;color:var(--ink-2);margin-left:6px}
.caption{margin:0;color:var(--ink-2);max-width:72ch;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
mark{background:var(--mark);color:inherit;border-radius:3px;padding:0 2px}
.facts{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--ink-3)}
.facts b{color:var(--ink-2);font-weight:600}
.warn{display:flex;gap:8px;align-items:flex-start;background:var(--rejects-bg);color:var(--rejects);border-radius:8px;padding:8px 10px;font-size:13px;font-weight:600}
.lead-side{display:flex;flex-direction:column;align-items:flex-end;gap:8px;font-size:13px}
.contact{font-family:var(--mono);font-size:13px;color:var(--ink);white-space:nowrap}
.postlink{color:var(--accent);text-decoration:none;font-weight:600}
.postlink:hover{text-decoration:underline}
.empty{padding:48px 16px;text-align:center;color:var(--ink-3);border:1px dashed var(--line);border-radius:14px}

/* Drawer */
.scrim{position:fixed;inset:0;background:rgba(10,16,14,.35);z-index:20}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(520px,100%);background:var(--surface);z-index:21;box-shadow:-12px 0 40px rgba(0,0,0,.18);display:flex;flex-direction:column;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
.d-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:20px 22px 16px;border-bottom:1px solid var(--line)}
.d-head h2{margin:6px 0 0;font:800 22px/1.2 var(--display);text-wrap:balance}
.x{border:1px solid var(--line);background:var(--surface);border-radius:8px;width:34px;height:34px;cursor:pointer;flex:none;font-size:18px;line-height:1}
.d-body{overflow-y:auto;padding:18px 22px 28px;display:flex;flex-direction:column;gap:20px}
.sect h4{margin:0 0 8px;font:600 11px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--ink-3)}
.quote{margin:0;padding:12px 14px;border-left:3px solid var(--c,var(--accent));background:var(--surface-2);border-radius:0 8px 8px 0;font-size:15px}
.quote cite{display:block;margin-top:6px;font:500 12px var(--mono);color:var(--ink-3);font-style:normal}
.full{margin:0;white-space:pre-wrap;color:var(--ink-2);overflow-wrap:anywhere}
dl{display:grid;grid-template-columns:minmax(120px,40%) 1fr;gap:8px 14px;margin:0;font-size:14px}
dt{color:var(--ink-3)} dd{margin:0;font-weight:500;overflow-wrap:anywhere}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.bar{height:6px;border-radius:3px;background:var(--surface-2);overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;background:var(--c,var(--accent))}
.checks{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px;font-size:14px}
.checks li{display:flex;gap:8px;align-items:flex-start}
.ok{color:var(--good);font-weight:700} .no{color:var(--bad);font-weight:700}
.subject{font:500 13px/1.5 var(--mono);background:var(--surface-2);padding:10px 12px;border-radius:8px;overflow-wrap:anywhere}
.copyrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.copy{border:1px solid var(--line);background:var(--surface);border-radius:6px;padding:3px 9px;font-size:12px;cursor:pointer}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 14px;border-radius:10px;background:var(--accent);color:var(--surface);text-decoration:none;font-weight:600;font-size:14px}
.foot{color:var(--ink-3);font-size:12.5px;max-width:72ch}
.tag.camp{border-color:transparent;background:var(--accent-soft);color:var(--accent);font-weight:600}
.tag.ai{border-style:dashed}
.appnav{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.appnav a,.appnav button{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:9px;border:1px solid var(--line);background:var(--surface);color:var(--ink);text-decoration:none;font-size:13px;font-weight:600;cursor:pointer}
.appnav a:hover,.appnav button:hover{border-color:var(--ink-3)}
.appnav .primary{background:var(--accent);border-color:var(--accent);color:var(--surface)}
.appnav form{display:contents}
.flash{padding:10px 14px;border-radius:10px;font-size:14px;font-weight:500;background:var(--accent-soft);color:var(--accent)}
.flash.err{background:var(--rejects-bg);color:var(--rejects)}

@media (max-width:720px){
  .lead{grid-template-columns:52px minmax(0,1fr);gap:14px;padding:14px}
  .tools select{flex:1 1 140px}
  .contact{white-space:normal;overflow-wrap:anywhere}
  .score{width:52px;height:52px} .score b{font-size:17px}
  .lead-side{grid-column:1/-1;flex-direction:row;justify-content:space-between;align-items:center;padding-top:6px;border-top:1px solid var(--line)}
  .kpi{min-width:0;flex:1 1 90px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>

<div class="wrap">
  <header class="head">
    <div class="brand">
      <span class="eyebrow">ListingAI · Exclusive-agent intent</span>
      <h1>Lead Desk</h1>
      <span class="meta">__META__</span>
      __NAV__
    </div>
    <div class="kpis" aria-label="Summary">
      <div class="kpi"><b id="k-total">0</b><span>Listings</span></div>
      <div class="kpi hot"><b id="k-email">0</b><span>Exclusive alerts</span></div>
      <div class="kpi"><b id="k-review">0</b><span>Review queue</span></div>
    </div>
  </header>

  __FLASH__
  <nav class="chips" aria-label="Filter by status">
    <button type="button" class="chip all" data-status="" aria-pressed="true"><span class="dot"></span><span class="chip-label">All</span><span class="chip-count" data-count="">0</span></button>
    __CHIPS__
  </nav>

  <div class="tools">
    <label class="search"><svg viewBox="0 0 24 24" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="q" type="search" placeholder="Search caption, location, phone, agent…" aria-label="Search"></label>
    <select id="camp" aria-label="Campaign" hidden><option value="">All campaigns</option></select>
    <select id="loc" aria-label="Location"><option value="">All locations</option></select>
    <select id="sort" aria-label="Sort">
      <option value="priority">Sort: priority</option>
      <option value="score">Sort: score</option>
      <option value="newest">Sort: newest</option>
      <option value="price-asc">Sort: price ↑</option>
      <option value="price-desc">Sort: price ↓</option>
    </select>
    <label class="toggle"><input id="emailOnly" type="checkbox"> Alert-ready only</label>
  </div>

  <section class="list" id="list" aria-live="polite"></section>

  <p class="foot">Status comes from the owner's own words in the post. Being a direct owner is not treated as wanting an agent. Anything below __THRESHOLD__% confidence goes to the review queue. Click a listing for the evidence and alert checks.</p>
</div>

<div class="scrim" id="scrim" hidden></div>
<aside class="drawer" id="drawer" hidden role="dialog" aria-modal="true" aria-labelledby="d-title"></aside>

<script type="application/json" id="lead-data">__DATA__</script>
<script>
(function(){
  var DATA = JSON.parse(document.getElementById('lead-data').textContent);
  var state = {statuses:new Set(), q:'', loc:'', camp:'', sort:'priority', emailOnly:false};
  var $ = function(s){return document.querySelector(s)};
  var esc = function(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})};
  var rm = function(n){return n==null?'':'RM'+Number(n).toLocaleString('en-MY')};
  var pct = function(x){return Math.round((x||0)*100)+'%'};
  var ago = function(d){return d==null?'date unknown':d===0?'today':d===1?'1 day ago':d+' days ago'};
  var SRC = {caption:'Caption',image_ocr:'Image text (OCR)',video_transcript:'Video transcript',profile:'Profile',comment:'Owner comment',manually_verified:'Manually verified'};
  var PREF = {exclusive_agent_wanted:'Wants an exclusive agent',agents_welcome:'Agents welcome',appointed_agent_only:'Contact appointed agent only',no_agents:'No agents',unknown:'Unknown'};

  try{ var saved = JSON.parse(localStorage.getItem('leaddesk')||'{}');
    if(saved.sort) state.sort = saved.sort; if(saved.emailOnly) state.emailOnly = true; }catch(e){}

  // Summary and filter counts
  var counts = {};
  DATA.forEach(function(d){counts[d.status]=(counts[d.status]||0)+1});
  document.querySelectorAll('[data-count]').forEach(function(el){
    var k = el.getAttribute('data-count'); el.textContent = k ? (counts[k]||0) : DATA.length;
  });
  $('#k-total').textContent = DATA.length;
  $('#k-email').textContent = DATA.filter(function(d){return d.email_ready}).length;
  $('#k-review').textContent = DATA.filter(function(d){return d.review}).length;
  Array.from(new Set(DATA.map(function(d){return d.location}).filter(Boolean))).sort().forEach(function(l){
    var o = document.createElement('option'); o.value = l; o.textContent = l; $('#loc').appendChild(o);
  });
  var camps = Array.from(new Set([].concat.apply([], DATA.map(function(d){return d.campaigns||[]})))).sort();
  if(camps.length){ $('#camp').hidden = false; camps.forEach(function(c){
    var o = document.createElement('option'); o.value = c; o.textContent = c; $('#camp').appendChild(o); }); }
  try{ var qc = new URLSearchParams(location.search).get('campaign');
    if(qc && camps.indexOf(qc)>=0){ state.camp = qc; $('#camp').value = qc; } }catch(e){}
  $('#sort').value = state.sort; $('#emailOnly').checked = state.emailOnly;

  function highlight(text, phrase){
    if(!phrase) return esc(text);
    var i = text.toLowerCase().indexOf(phrase.toLowerCase());
    if(i<0) return esc(text);
    return esc(text.slice(0,i))+'<mark>'+esc(text.slice(i,i+phrase.length))+'</mark>'+esc(text.slice(i+phrase.length));
  }
  function scoreClass(s){return s>=85?'s-hi':s>=65?'s-mid':'s-lo'}
  function badge(d){return '<span class="badge c-'+d.badge_key+'"><span class="dot"></span>'+esc(d.badge)+'</span>'}

  function visible(){
    var q = state.q.toLowerCase();
    var rows = DATA.filter(function(d){
      if(state.statuses.size && !state.statuses.has(d.status)) return false;
      if(state.loc && d.location!==state.loc) return false;
      if(state.camp && (d.campaigns||[]).indexOf(state.camp)<0) return false;
      if(state.emailOnly && !d.email_ready) return false;
      if(q){
        var hay = [d.caption,d.location,d.phone,d.email,d.evidence,d.agent_name,d.agent_ren,d.badge,d.id].join(' ').toLowerCase();
        if(hay.indexOf(q)<0) return false;
      }
      return true;
    });
    var by = {
      priority:function(a,b){return a.priority-b.priority||b.score-a.score},
      score:function(a,b){return b.score-a.score},
      newest:function(a,b){return (a.age_days==null?1e9:a.age_days)-(b.age_days==null?1e9:b.age_days)},
      'price-asc':function(a,b){return (a.price||0)-(b.price||0)},
      'price-desc':function(a,b){return (b.price||0)-(a.price||0)}
    }[state.sort];
    return rows.sort(by);
  }

  function render(){
    var rows = visible();
    if(!rows.length){ $('#list').innerHTML = '<div class="empty">No listings match these filters.</div>'; return; }
    $('#list').innerHTML = rows.map(function(d){
      var tags = badge(d);
      if(d.review && d.status!=='uncertain') tags += '<span class="badge c-review"><span class="dot"></span>Perlu Semakan</span>';
      if(d.email_ready) tags += '<span class="tag mail">Exclusive alert</span>';
      tags += '<span class="tag">'+(d.direct_owner?'Direct owner':'Owner unverified')+'</span>';
      (d.campaigns||[]).forEach(function(c){ tags += '<span class="tag camp">'+esc(c)+'</span>'; });
      if(d.classified_by && d.classified_by.indexOf('OpenAI')>=0) tags += '<span class="tag ai">AI checked</span>';
      var facts = [];
      if(d.evidence) facts.push('<span><b>'+pct(d.confidence)+'</b> confidence · '+esc(SRC[d.source]||d.source)+'</span>');
      if(d.agent_name||d.agent_ren) facts.push('<span>Agent <b>'+esc([d.agent_name,d.agent_ren].filter(Boolean).join(' · '))+'</b></span>');
      facts.push('<span>Posted '+ago(d.age_days)+'</span>');
      if(!d.classified) facts.push('<span>No public contact, not analysed</span>');
      return '<article class="lead'+(d.status==='seeking_exclusive_agent'?' is-seeking':'')+'" tabindex="0" role="button" data-id="'+esc(d.id)+'" aria-label="Open '+esc(d.location)+' listing">'+
        '<div class="score '+scoreClass(d.score)+'" style="--v:'+d.score+'"><b>'+d.score+'</b><small>/100</small></div>'+
        '<div class="lead-main"><div class="tags">'+tags+'</div>'+
          '<h3>'+esc(d.location||'Unknown location')+'<span class="price">'+rm(d.price)+'</span></h3>'+
          '<p class="caption">'+highlight(d.caption||'',d.evidence)+'</p>'+
          (d.owner_instruction?'<div class="warn"><span aria-hidden="true">⚠</span><span>'+esc(d.owner_instruction)+'</span></div>':'')+
          '<div class="facts">'+facts.join('')+'</div></div>'+
        '<div class="lead-side"><span class="contact">'+esc(d.phone||d.email||'—')+'</span>'+
          (d.url?'<a class="postlink" href="'+esc(d.url)+'" target="_blank" rel="noopener">Original post ↗</a>':'')+'</div>'+
      '</article>';
    }).join('');
  }

  function copyBtn(v){return v?'<span class="copyrow"><span class="num">'+esc(v)+'</span><button type="button" class="copy" data-copy="'+esc(v)+'">Copy</button></span>':'—'}
  function check(ok,label){return '<li><span class="'+(ok?'ok':'no')+'">'+(ok?'✓':'✕')+'</span><span>'+esc(label)+'</span></li>'}

  var lastFocus = null;
  function openDrawer(id){
    var d = DATA.find(function(x){return x.id===id}); if(!d) return;
    lastFocus = document.activeElement;
    var adj = d.adjustment ? (d.adjustment>0?'+':'')+d.adjustment : '0';
    var alertChecks = d.email_ready ? check(true,'All exclusive-alert rules pass') : d.email_blockers.map(function(r){return check(false,r)}).join('');
    var contactCheck = d.can_contact ? check(true, d.override ? 'Allowed by manual override ('+d.override+')' : 'Can enter the To Contact pipeline')
                                     : d.contact_block.map(function(r){return check(false,'Blocked from To Contact: '+r)}).join('');
    $('#drawer').innerHTML =
      '<div class="d-head"><div>'+badge(d)+'<h2 id="d-title">'+esc(d.location)+' · '+rm(d.price)+'</h2></div>'+
      '<button type="button" class="x" id="close" aria-label="Close">×</button></div>'+
      '<div class="d-body">'+
        (d.owner_instruction?'<div class="warn"><span aria-hidden="true">⚠</span><span>'+esc(d.owner_instruction)+'</span></div>':'')+
        '<div class="sect c-'+d.badge_key+'"><h4>Supporting phrase</h4>'+
          (d.evidence?'<blockquote class="quote">“'+esc(d.evidence)+'”<cite>'+esc(SRC[d.source]||d.source||'')+'</cite></blockquote>':'<p class="full">No statement about agents found.</p>')+
          (d.review_reasons.length?'<p class="full">Needs review: '+esc(d.review_reasons.join(', '))+'</p>':'')+'</div>'+
        '<div class="sect"><h4>Classification</h4><dl>'+
          '<dt>Status</dt><dd>'+esc(d.badge)+' <span class="num" style="color:var(--ink-3)">'+esc(d.status)+'</span></dd>'+
          '<dt>Confidence</dt><dd><span class="num">'+pct(d.confidence)+'</span><div class="bar c-'+d.badge_key+'"><i style="width:'+pct(d.confidence)+'"></i></div></dd>'+
          '<dt>Probability seeking</dt><dd class="num">'+pct(d.probability)+'</dd>'+
          '<dt>Contact preference</dt><dd>'+esc(PREF[d.preference]||d.preference)+'</dd>'+
          (d.agent_name||d.agent_ren?'<dt>Appointed agent</dt><dd>'+esc(d.agent_name||'—')+'</dd><dt>REN</dt><dd class="num">'+esc(d.agent_ren||'—')+'</dd>':'')+
          '<dt>Checked by</dt><dd>'+esc(d.classified_by||'—')+'</dd>'+
          '<dt>Campaigns</dt><dd>'+esc((d.campaigns||[]).join(', ')||'None')+'</dd>'+
          '<dt>Direct owner</dt><dd>'+(d.direct_owner?'Yes':'No / unknown')+'</dd>'+
          '<dt>Detected</dt><dd class="num">'+esc(d.detected_at?d.detected_at.slice(0,16).replace('T',' '):'—')+'</dd>'+
        '</dl></div>'+
        '<div class="sect"><h4>Opportunity score</h4><dl>'+
          '<dt>Base score</dt><dd class="num">'+d.base_score+'</dd>'+
          '<dt>Agent-intent adjustment</dt><dd class="num">'+adj+'</dd>'+
          '<dt>Final score</dt><dd class="num"><b>'+d.score+'/100</b></dd>'+
        '</dl></div>'+
        '<div class="sect"><h4>Exclusive alert</h4><ul class="checks">'+alertChecks+contactCheck+'</ul>'+
          '<p class="subject" style="margin-top:10px">'+esc(d.subject)+'</p></div>'+
        '<div class="sect"><h4>Public contact</h4><dl><dt>Phone</dt><dd>'+copyBtn(d.phone)+'</dd><dt>Email</dt><dd>'+copyBtn(d.email)+'</dd></dl></div>'+
        '<div class="sect"><h4>Full post</h4><p class="full">'+highlight(d.caption||'',d.evidence)+'</p></div>'+
        (d.url?'<div><a class="btn" href="'+esc(d.url)+'" target="_blank" rel="noopener">Open original post ↗</a></div>':'')+
      '</div>';
    $('#drawer').hidden = false; $('#scrim').hidden = false;
    $('#close').focus();
  }
  function closeDrawer(){ $('#drawer').hidden = true; $('#scrim').hidden = true; if(lastFocus) lastFocus.focus(); }

  document.querySelectorAll('.chip').forEach(function(chip){
    chip.addEventListener('click', function(){
      var s = chip.getAttribute('data-status');
      if(!s) state.statuses.clear(); else if(state.statuses.has(s)) state.statuses.delete(s); else state.statuses.add(s);
      document.querySelectorAll('.chip').forEach(function(c){
        var k = c.getAttribute('data-status');
        c.setAttribute('aria-pressed', String(k ? state.statuses.has(k) : state.statuses.size===0));
      });
      render();
    });
  });
  function save(){ try{ localStorage.setItem('leaddesk', JSON.stringify({sort:state.sort, emailOnly:state.emailOnly})) }catch(e){} }
  $('#q').addEventListener('input', function(e){ state.q = e.target.value; render(); });
  $('#loc').addEventListener('change', function(e){ state.loc = e.target.value; render(); });
  $('#camp').addEventListener('change', function(e){ state.camp = e.target.value; render(); });
  $('#sort').addEventListener('change', function(e){ state.sort = e.target.value; save(); render(); });
  $('#emailOnly').addEventListener('change', function(e){ state.emailOnly = e.target.checked; save(); render(); });
  $('#list').addEventListener('click', function(e){
    if(e.target.closest('a')) return;
    var card = e.target.closest('.lead'); if(card) openDrawer(card.getAttribute('data-id'));
  });
  $('#list').addEventListener('keydown', function(e){
    if((e.key==='Enter'||e.key===' ') && e.target.classList.contains('lead')){ e.preventDefault(); openDrawer(e.target.getAttribute('data-id')); }
  });
  $('#scrim').addEventListener('click', closeDrawer);
  $('#drawer').addEventListener('click', function(e){
    if(e.target.id==='close') closeDrawer();
    var b = e.target.closest('.copy');
    if(b){ var v = b.getAttribute('data-copy');
      var done = function(){ b.textContent='Copied'; setTimeout(function(){b.textContent='Copy'},1400) };
      try{ navigator.clipboard.writeText(v).then(done, function(){ selectText(b.previousSibling) }); }catch(err){ selectText(b.previousSibling) }
    }
  });
  function selectText(el){ var r=document.createRange(); r.selectNodeContents(el); var s=getSelection(); s.removeAllRanges(); s.addRange(r); }
  document.addEventListener('keydown', function(e){ if(e.key==='Escape' && !$('#drawer').hidden) closeDrawer(); });
  render();
})();
</script>"""
