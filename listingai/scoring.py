"""Opportunity score with the exclusive-agent adjustment.

The exclusive-agent adjustment only changes priority. It never overrides the
hard requirements (fraud, privacy, contact validation, location, duplicates,
age): those are evaluated independently in `hard_gate_failures`, and a
listing failing any of them receives no positive bonus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import DEFAULT_CONFIG, ExclusiveAgentConfig
from .models import ExclusiveAgentStatus as S, Listing


@dataclass
class ScoreResult:
    score: int
    base_score: int
    exclusive_agent_adjustment: int
    held_for_review: bool
    excluded_from_immediate_alerts: bool
    hard_gate_failures: list[str] = field(default_factory=list)


def hard_gate_failures(
    listing: Listing,
    config: ExclusiveAgentConfig = DEFAULT_CONFIG,
    now: Optional[datetime] = None,
) -> list[str]:
    now = now or datetime.now(timezone.utc)
    failures = []
    if not listing.has_eligible_contact:
        failures.append("no_eligible_contact")
    if listing.privacy_blocked:
        failures.append("privacy_blocked")
    if listing.scam_risk_score >= config.max_scam_risk_score:
        failures.append("scam_risk_too_high")
    if not listing.matches_target_location:
        failures.append("outside_target_location")
    if listing.is_confirmed_duplicate:
        failures.append("confirmed_duplicate")
    if listing.posted_at is None:
        failures.append("unknown_post_age")
    elif now - listing.posted_at > timedelta(days=config.max_listing_age_days):
        failures.append("too_old")
    return failures


def exclusive_agent_adjustment(listing: Listing, config: ExclusiveAgentConfig = DEFAULT_CONFIG) -> int:
    ea = listing.exclusive_agent
    if ea is None:
        return 0
    return {
        S.SEEKING_EXCLUSIVE_AGENT: config.seeking_exclusive_bonus,
        S.OPEN_TO_AGENT_APPOINTMENT: config.open_to_appointment_bonus,
        S.ALREADY_HAS_EXCLUSIVE_AGENT: config.already_has_agent_penalty,
    }.get(ea.exclusive_agent_status, 0)


def compute_opportunity_score(
    listing: Listing,
    config: ExclusiveAgentConfig = DEFAULT_CONFIG,
    now: Optional[datetime] = None,
) -> ScoreResult:
    failures = hard_gate_failures(listing, config, now)
    adjustment = exclusive_agent_adjustment(listing, config)
    if failures and adjustment > 0:
        adjustment = 0  # a bonus must never lift a listing that fails a hard gate
    score = max(0, min(100, listing.base_opportunity_score + adjustment))
    ea = listing.exclusive_agent
    status = ea.exclusive_agent_status if ea else None
    return ScoreResult(
        score=score,
        base_score=listing.base_opportunity_score,
        exclusive_agent_adjustment=adjustment,
        held_for_review=status is S.UNCERTAIN or bool(ea and ea.exclusive_agent_review_required),
        excluded_from_immediate_alerts=status is S.REJECTS_AGENTS and not config.alert_on_rejects_agents,
        hard_gate_failures=failures,
    )
