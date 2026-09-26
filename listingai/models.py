"""Core data models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ExclusiveAgentStatus(str, Enum):
    SEEKING_EXCLUSIVE_AGENT = "seeking_exclusive_agent"
    OPEN_TO_AGENT_APPOINTMENT = "open_to_agent_appointment"
    ALREADY_HAS_EXCLUSIVE_AGENT = "already_has_exclusive_agent"
    REJECTS_AGENTS = "rejects_agents"
    NO_EVIDENCE = "no_evidence"
    UNCERTAIN = "uncertain"


class EvidenceSource(str, Enum):
    CAPTION = "caption"
    IMAGE_OCR = "image_ocr"
    VIDEO_TRANSCRIPT = "video_transcript"
    PROFILE = "profile"
    COMMENT = "comment"
    MANUALLY_VERIFIED = "manually_verified"


class AgentContactPreference(str, Enum):
    EXCLUSIVE_AGENT_WANTED = "exclusive_agent_wanted"
    AGENTS_WELCOME = "agents_welcome"
    APPOINTED_AGENT_ONLY = "appointed_agent_only"
    NO_AGENTS = "no_agents"
    UNKNOWN = "unknown"


@dataclass
class TextEvidence:
    """A piece of text attached to a listing that may reveal agent intent.

    `source_confidence` is the OCR / transcription confidence (0-1). Captions,
    profiles and comments are typed text and default to 1.0.

    `author_is_owner` must be True for comments to count: a comment written by
    somebody else says nothing about the owner's intent.

    For `manually_verified` evidence a reviewer sets `verified_status` and the
    supporting `text`; it supersedes every automated signal.
    """

    source: EvidenceSource
    text: str
    source_confidence: float = 1.0
    author_is_owner: bool = True
    verified_status: Optional[ExclusiveAgentStatus] = None
    verified_by: Optional[str] = None


@dataclass
class ExclusiveAgentResult:
    exclusive_agent_status: ExclusiveAgentStatus
    exclusive_agent_probability: float
    exclusive_agent_confidence: float
    exclusive_agent_evidence: Optional[str]
    exclusive_agent_evidence_source: Optional[EvidenceSource]
    exclusive_agent_review_required: bool
    agent_contact_preference: AgentContactPreference
    already_appointed_agent_name: Optional[str]
    already_appointed_agent_ren: Optional[str]
    exclusive_agent_detected_at: datetime
    review_reason: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Enum):
                data[key] = value.value
            elif isinstance(value, datetime):
                data[key] = value.isoformat()
        return data


@dataclass
class ContactOverride:
    """A user's manual decision to contact a listing despite a block."""

    user: str
    reason: str
    at: datetime


@dataclass
class Listing:
    id: str
    post_url: str
    location: str
    price: Optional[int] = None
    caption: str = ""
    is_direct_owner: bool = False
    public_phone: Optional[str] = None
    public_email: Optional[str] = None
    has_eligible_contact: bool = False
    matches_target_location: bool = False
    is_confirmed_duplicate: bool = False
    posted_at: Optional[datetime] = None
    scam_risk_score: int = 0  # 0-100
    privacy_blocked: bool = False
    base_opportunity_score: int = 0  # 0-100, before exclusive-agent adjustment
    evidence: list[TextEvidence] = field(default_factory=list)
    exclusive_agent: Optional[ExclusiveAgentResult] = None
    pipeline_stage: Optional[str] = None
    contact_override: Optional[ContactOverride] = None

    @property
    def public_contact(self) -> Optional[str]:
        return self.public_phone or self.public_email

    def all_evidence(self) -> list[TextEvidence]:
        items = list(self.evidence)
        if self.caption and not any(e.source is EvidenceSource.CAPTION for e in items):
            items.insert(0, TextEvidence(EvidenceSource.CAPTION, self.caption))
        return items
