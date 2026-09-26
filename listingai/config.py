"""Configurable thresholds and score adjustments for exclusive-agent intent."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping


@dataclass(frozen=True)
class ExclusiveAgentConfig:
    # Classification ---------------------------------------------------------
    # Minimum confidence (0-1) for a definite status. Anything below becomes
    # `uncertain` and is sent to the Review Queue.
    confidence_threshold: float = 0.75
    # OCR / transcription results with a source confidence below this value
    # can never produce a definite status on their own.
    min_source_confidence: float = 0.60
    # Probability that a listing with no evidence is seeking an exclusive agent.
    no_evidence_prior: float = 0.05

    # Scoring (points on a 0-100 opportunity score) ----------------------------
    seeking_exclusive_bonus: int = 15
    open_to_appointment_bonus: int = 5
    already_has_agent_penalty: int = -20

    # Alerts -------------------------------------------------------------------
    alert_on_rejects_agents: bool = False  # rejects_agents excluded by default
    max_listing_age_days: int = 14
    max_scam_risk_score: int = 40  # 0-100, alert only when strictly below
    dashboard_base_url: str = "https://listingai.example/dashboard"

    # Per-source reliability multipliers applied to pattern weights.
    source_reliability: Mapping[str, float] = field(
        default_factory=lambda: {
            "manually_verified": 1.0,
            "caption": 1.0,
            "profile": 0.9,
            "video_transcript": 0.95,
            "image_ocr": 0.95,
            "comment": 0.85,
        }
    )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExclusiveAgentConfig":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        return cls(**dict(data))


DEFAULT_CONFIG = ExclusiveAgentConfig()
