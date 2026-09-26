"""Local settings (OpenAI key, model) and listing storage.

Everything lives in a folder on the user's own computer: `~/.listingai`, or
the folder named by the LISTINGAI_HOME environment variable. The API key is
never written into the dashboard HTML or the repository.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import (
    AgentContactPreference,
    ContactOverride,
    EvidenceSource,
    ExclusiveAgentResult,
    ExclusiveAgentStatus,
    Listing,
    TextEvidence,
)

DEFAULT_MODEL = "gpt-4.1-mini"


def data_dir() -> Path:
    path = Path(os.environ.get("LISTINGAI_HOME") or Path.home() / ".listingai")
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Settings:
    openai_api_key: str = ""
    openai_model: str = DEFAULT_MODEL
    # Saved-search email alerts (read over IMAP, e.g. Gmail with an app password).
    email_address: str = ""
    email_app_password: str = ""
    email_imap_host: str = "imap.gmail.com"
    email_sites: list = field(default_factory=lambda: ["mudah", "propertyguru", "iproperty"])
    email_check_minutes: int = 30
    email_seen: list = field(default_factory=list)  # Message-IDs already imported
    email_last_check: str = ""

    @property
    def effective_key(self) -> str:
        """A saved key wins; otherwise fall back to the OPENAI_API_KEY variable."""
        return self.openai_api_key or os.environ.get("OPENAI_API_KEY", "")

    @property
    def key_hint(self) -> str:
        key = self.effective_key
        if not key:
            return ""
        return f"{key[:3]}…{key[-4:]}" if len(key) > 10 else "saved"


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # owner-only; a no-op on Windows
    except OSError:
        pass
    tmp.replace(path)


def load_settings() -> Settings:
    path = data_dir() / "settings.json"
    if not path.exists():
        return Settings()
    raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    defaults = Settings()
    return Settings(
        openai_api_key=raw.get("openai_api_key", ""),
        openai_model=raw.get("openai_model") or DEFAULT_MODEL,
        email_address=raw.get("email_address", ""),
        email_app_password=raw.get("email_app_password", ""),
        email_imap_host=raw.get("email_imap_host") or defaults.email_imap_host,
        email_sites=list(raw.get("email_sites", defaults.email_sites)),
        email_check_minutes=int(raw.get("email_check_minutes", defaults.email_check_minutes)),
        email_seen=list(raw.get("email_seen", [])),
        email_last_check=raw.get("email_last_check", ""),
    )


def save_settings(settings: Settings) -> None:
    _write_private(data_dir() / "settings.json", json.dumps(asdict(settings), indent=2))


# --- listing storage ----------------------------------------------------------

def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def result_to_dict(r: ExclusiveAgentResult) -> dict:
    return r.to_dict()


def result_from_dict(d: dict) -> ExclusiveAgentResult:
    return ExclusiveAgentResult(
        exclusive_agent_status=ExclusiveAgentStatus(d["exclusive_agent_status"]),
        exclusive_agent_probability=d["exclusive_agent_probability"],
        exclusive_agent_confidence=d["exclusive_agent_confidence"],
        exclusive_agent_evidence=d.get("exclusive_agent_evidence"),
        exclusive_agent_evidence_source=EvidenceSource(d["exclusive_agent_evidence_source"]) if d.get("exclusive_agent_evidence_source") else None,
        exclusive_agent_review_required=d["exclusive_agent_review_required"],
        agent_contact_preference=AgentContactPreference(d["agent_contact_preference"]),
        already_appointed_agent_name=d.get("already_appointed_agent_name"),
        already_appointed_agent_ren=d.get("already_appointed_agent_ren"),
        exclusive_agent_detected_at=_dt(d["exclusive_agent_detected_at"]),
        review_reason=d.get("review_reason"),
        classified_by=d.get("classified_by", "rules"),
    )


def listing_to_dict(l: Listing) -> dict:
    return {
        "id": l.id, "post_url": l.post_url, "location": l.location, "price": l.price, "caption": l.caption,
        "is_direct_owner": l.is_direct_owner, "public_phone": l.public_phone, "public_email": l.public_email,
        "has_eligible_contact": l.has_eligible_contact, "matches_target_location": l.matches_target_location,
        "is_confirmed_duplicate": l.is_confirmed_duplicate,
        "posted_at": l.posted_at.isoformat() if l.posted_at else None,
        "scam_risk_score": l.scam_risk_score, "privacy_blocked": l.privacy_blocked,
        "base_opportunity_score": l.base_opportunity_score,
        "evidence": [
            {"source": e.source.value, "text": e.text, "source_confidence": e.source_confidence,
             "author_is_owner": e.author_is_owner,
             "verified_status": e.verified_status.value if e.verified_status else None, "verified_by": e.verified_by}
            for e in l.evidence
        ],
        "exclusive_agent": result_to_dict(l.exclusive_agent) if l.exclusive_agent else None,
        "pipeline_stage": l.pipeline_stage,
        "contact_override": (
            {"user": l.contact_override.user, "reason": l.contact_override.reason, "at": l.contact_override.at.isoformat()}
            if l.contact_override else None
        ),
        "campaign_ids": l.campaign_ids,
        "source": l.source,
    }


def listing_from_dict(d: dict) -> Listing:
    co = d.get("contact_override")
    return Listing(
        id=d["id"], post_url=d.get("post_url", ""), location=d.get("location", ""), price=d.get("price"),
        caption=d.get("caption", ""), is_direct_owner=d.get("is_direct_owner", False),
        public_phone=d.get("public_phone"), public_email=d.get("public_email"),
        has_eligible_contact=d.get("has_eligible_contact", False),
        matches_target_location=d.get("matches_target_location", False),
        is_confirmed_duplicate=d.get("is_confirmed_duplicate", False), posted_at=_dt(d.get("posted_at")),
        scam_risk_score=d.get("scam_risk_score", 0), privacy_blocked=d.get("privacy_blocked", False),
        base_opportunity_score=d.get("base_opportunity_score", 0),
        evidence=[
            TextEvidence(EvidenceSource(e["source"]), e["text"], e.get("source_confidence", 1.0), e.get("author_is_owner", True),
                         ExclusiveAgentStatus(e["verified_status"]) if e.get("verified_status") else None, e.get("verified_by"))
            for e in d.get("evidence", [])
        ],
        exclusive_agent=result_from_dict(d["exclusive_agent"]) if d.get("exclusive_agent") else None,
        pipeline_stage=d.get("pipeline_stage"),
        contact_override=ContactOverride(co["user"], co["reason"], _dt(co["at"])) if co else None,
        campaign_ids=list(d.get("campaign_ids", [])),
        source=d.get("source", ""),
    )


def listings_path() -> Path:
    return data_dir() / "listings.json"


def load_store() -> Optional[list[Listing]]:
    path = listings_path()
    if not path.exists():
        return None
    return [listing_from_dict(d) for d in json.loads(path.read_text(encoding="utf-8") or "[]")]


def save_store(listings: list[Listing]) -> None:
    _write_private(listings_path(), json.dumps([listing_to_dict(l) for l in listings], ensure_ascii=False, indent=1))
