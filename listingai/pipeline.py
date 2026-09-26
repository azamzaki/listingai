"""To Contact pipeline gating and the Review Queue."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from .models import ContactOverride, ExclusiveAgentStatus as S, Listing

TO_CONTACT = "to_contact"


def owner_instruction(listing: Listing) -> Optional[str]:
    """The owner's own words asking agents not to contact them, if any."""
    ea = listing.exclusive_agent
    if ea and ea.exclusive_agent_status is S.REJECTS_AGENTS:
        phrase = ea.exclusive_agent_evidence or "Owner does not want agents"
        return f"Pemilik tidak mahu dihubungi oleh ejen: “{phrase}”"
    return None


def to_contact_block_reason(listing: Listing) -> Optional[str]:
    ea = listing.exclusive_agent
    if ea is None:
        return None
    if ea.exclusive_agent_status is S.REJECTS_AGENTS:
        return "owner_rejects_agents"
    if ea.exclusive_agent_status is S.UNCERTAIN or ea.exclusive_agent_review_required:
        return "pending_review"
    return None


def can_enter_to_contact(listing: Listing) -> tuple[bool, Optional[str]]:
    reason = to_contact_block_reason(listing)
    if reason and listing.contact_override is None:
        return False, reason
    return True, None


def override_contact_block(listing: Listing, user: str, reason: str, at: Optional[datetime] = None) -> None:
    """Record a user's explicit decision to contact despite a block."""
    if not user or not reason:
        raise ValueError("A manual override needs both the user and a reason")
    listing.contact_override = ContactOverride(user=user, reason=reason, at=at or datetime.now(timezone.utc))


def move_to_contact(listing: Listing) -> None:
    allowed, reason = can_enter_to_contact(listing)
    if not allowed:
        raise PermissionError(f"Listing {listing.id} cannot enter To Contact: {reason}")
    listing.pipeline_stage = TO_CONTACT


def review_queue(listings: Iterable[Listing]) -> list[Listing]:
    return [l for l in listings if l.exclusive_agent and l.exclusive_agent.exclusive_agent_review_required]
