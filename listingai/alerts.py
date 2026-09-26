"""Email alerts, including the [EXCLUSIVE OPPORTUNITY] alert."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Optional

from .config import DEFAULT_CONFIG, ExclusiveAgentConfig
from .dashboard import BADGES, dashboard_link
from .models import ExclusiveAgentStatus as S, Listing
from .scoring import compute_opportunity_score

EXCLUSIVE_TAG = "[EXCLUSIVE OPPORTUNITY]"


@dataclass
class AlertDecision:
    send: bool
    reasons: list[str] = field(default_factory=list)


def exclusive_opportunity_decision(
    listing: Listing,
    config: ExclusiveAgentConfig = DEFAULT_CONFIG,
    now: Optional[datetime] = None,
) -> AlertDecision:
    """Every business rule must hold for an exclusive-opportunity email."""
    score = compute_opportunity_score(listing, config, now)
    reasons = list(score.hard_gate_failures)
    ea = listing.exclusive_agent
    if ea is None or ea.exclusive_agent_status is not S.SEEKING_EXCLUSIVE_AGENT:
        reasons.append("not_seeking_exclusive_agent")
    elif ea.exclusive_agent_confidence < config.confidence_threshold:
        reasons.append("confidence_below_threshold")
    elif ea.exclusive_agent_review_required:
        reasons.append("pending_review")
    return AlertDecision(send=not reasons, reasons=reasons)


def immediate_alert_decision(
    listing: Listing,
    config: ExclusiveAgentConfig = DEFAULT_CONFIG,
    now: Optional[datetime] = None,
) -> AlertDecision:
    """Any immediate lead alert (exclusive or not)."""
    score = compute_opportunity_score(listing, config, now)
    reasons = list(score.hard_gate_failures)
    if score.excluded_from_immediate_alerts:
        reasons.append("owner_rejects_agents")
    if score.held_for_review:
        reasons.append("pending_review")
    return AlertDecision(send=not reasons, reasons=reasons)


def _headline(listing: Listing) -> str:
    who = "Direct Owner" if listing.is_direct_owner else "Listing"
    price = f" RM{listing.price:,}" if listing.price is not None else ""
    return f"{who} – {listing.location}{price}"


def build_subject(listing: Listing, config: ExclusiveAgentConfig = DEFAULT_CONFIG, now: Optional[datetime] = None) -> str:
    score = compute_opportunity_score(listing, config, now).score
    ea = listing.exclusive_agent
    tag = EXCLUSIVE_TAG if ea and ea.exclusive_agent_status is S.SEEKING_EXCLUSIVE_AGENT else ""
    return f"{tag}[{score}/100] {_headline(listing)}"


def build_alert_email(
    listing: Listing,
    config: ExclusiveAgentConfig = DEFAULT_CONFIG,
    now: Optional[datetime] = None,
    to: Optional[str] = None,
    sender: Optional[str] = None,
) -> EmailMessage:
    ea = listing.exclusive_agent
    status = ea.exclusive_agent_status if ea else S.NO_EVIDENCE
    rows = [
        ("Exclusive-agent status", f"{BADGES[status].label} ({status.value})"),
        ("Confidence score", f"{round((ea.exclusive_agent_confidence if ea else 0) * 100)}%"),
        ("Supporting phrase", f"“{ea.exclusive_agent_evidence}”" if ea and ea.exclusive_agent_evidence else "—"),
        ("Source of evidence", ea.exclusive_agent_evidence_source.value if ea and ea.exclusive_agent_evidence_source else "—"),
        ("Public phone", listing.public_phone or "—"),
        ("Public email", listing.public_email or "—"),
        ("Original post", listing.post_url),
        ("Dashboard", dashboard_link(listing, config)),
    ]

    msg = EmailMessage()
    msg["Subject"] = build_subject(listing, config, now)
    if to:
        msg["To"] = to
    if sender:
        msg["From"] = sender
    text = "\n".join(f"{k}: {v}" for k, v in rows)
    msg.set_content(f"{_headline(listing)}\n\n{text}\n")
    cells = "".join(
        f"<tr><th align='left'>{html.escape(k)}</th><td>"
        + (f"<a href='{html.escape(v)}'>{html.escape(v)}</a>" if v.startswith("http") else html.escape(v))
        + "</td></tr>"
        for k, v in rows
    )
    msg.add_alternative(
        f"<html><body><h2>{html.escape(_headline(listing))}</h2><table>{cells}</table></body></html>",
        subtype="html",
    )
    return msg
