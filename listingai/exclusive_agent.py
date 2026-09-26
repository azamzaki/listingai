"""Exclusive-agent intent detection (English + Bahasa Malaysia).

Rules of the classifier:

* Evidence is always an exact substring of the text it came from. Truncated
  OCR words are never completed; a truncated agent keyword yields `uncertain`.
* Direct-owner status is never an input. "Owner sell" is not a request for an
  exclusive agent.
* Anything below `confidence_threshold` becomes `uncertain` and requires review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from .config import DEFAULT_CONFIG, ExclusiveAgentConfig
from .models import (
    AgentContactPreference,
    EvidenceSource,
    ExclusiveAgentResult,
    ExclusiveAgentStatus as S,
    Listing,
    TextEvidence,
)

_AGENT = r"(?:agents?|ejen|agen)"
_ANY_AGENT = r"(?:(?:property|real\s+estate|hartanah)\s+)?" + _AGENT
_EXCL = r"(?:exclusive|sole|eksklusif|tunggal)"
_NEG = r"(?:not|no|don'?t|do\s+not|never|tak|tidak|x|bukan|tiada|tak\s+nak|tidak\s+mahu)"

# (status, weight, pattern). Weight is the confidence of a clean match in a
# fully reliable source.
_RULES: list[tuple[S, float, str]] = [
    # --- rejects_agents -----------------------------------------------------
    (S.REJECTS_AGENTS, 0.95, rf"\b(?:strictly\s+)?no\s+{_ANY_AGENT}\b(?!\s*(?:fees?|commission|komisen|yuran|charges?))(?:\s+(?:please|pls|plz))?"),
    (S.REJECTS_AGENTS, 0.95, rf"\b{_AGENT}\s+(?:please\s+|pls\s+|plz\s+)?(?:do\s+not|don'?t|dont|no\s+need\s+to|need\s+not)\s+(?:contact|call|disturb|pm|whatsapp|message|text)\b"),
    (S.REJECTS_AGENTS, 0.95, rf"\b{_AGENT}\s+(?:not\s+welcome|are\s+not\s+welcome|tidak\s+dialu-?alukan)"),
    (S.REJECTS_AGENTS, 0.95, rf"\b{_AGENT}\s+(?:sila\s+)?(?:jangan|tak\s+payah|x\s+payah|usah|tidak\s+perlu)\s+(?:hubungi|call|kacau|ganggu|pm|contact|whatsapp|wasap)\b"),
    (S.REJECTS_AGENTS, 0.9, rf"\b{_AGENT}\s+(?:x|tak|tidak)\s+layan\b"),
    (S.REJECTS_AGENTS, 0.9, rf"\b(?:tak|tidak|x)\s+(?:nak|mahu|perlu|perlukan|terima|layan)\s+{_ANY_AGENT}\b"),
    (S.REJECTS_AGENTS, 0.9, rf"\b(?:not\s+looking\s+for|don'?t\s+need|do\s+not\s+need|no\s+need\s+for)\s+(?:an?\s+)?(?:{_EXCL}\s+)?{_ANY_AGENT}\b"),
    (S.REJECTS_AGENTS, 0.95, r"\bdirect\s+buyers?\s+only\b"),
    (S.REJECTS_AGENTS, 0.95, r"\b(?:no|tiada|x|tak\s+nak)\s+co-?\s?broke?\b"),
    (S.REJECTS_AGENTS, 0.9, r"\bco-?\s?broke?\s+(?:not\s+(?:allowed|welcome)|tidak\s+dibenarkan)\b"),
    (S.REJECTS_AGENTS, 0.9, r"\b(?:owner|direct)\s+deal\s+only\b"),
    (S.REJECTS_AGENTS, 0.85, r"\bprincipals?\s+only\b"),
    (S.REJECTS_AGENTS, 0.9, r"\bpembeli\s+(?:terus\s+)?sahaja\b"),

    # --- already_has_exclusive_agent -----------------------------------------
    (S.ALREADY_HAS_EXCLUSIVE_AGENT, 0.9, r"\bexclusive\s+listing\b"),
    (S.ALREADY_HAS_EXCLUSIVE_AGENT, 0.95, rf"\b{_EXCL}\s+{_AGENT}\s+(?:has\s+been\s+|already\s+)?appointed\b"),
    (S.ALREADY_HAS_EXCLUSIVE_AGENT, 0.9, rf"\b(?:already\s+)?appointed\s+(?:an?\s+)?(?:{_EXCL}\s+)?{_AGENT}\b"),
    (S.ALREADY_HAS_EXCLUSIVE_AGENT, 0.95, rf"\b(?:sudah|dah|telah)\s+(?:pun\s+)?lantik\s+(?:seorang\s+)?{_AGENT}\b"),
    (S.ALREADY_HAS_EXCLUSIVE_AGENT, 0.95, rf"\bcontact\s+(?:the\s+|our\s+|my\s+)?appointed\s+{_AGENT}\b"),
    (S.ALREADY_HAS_EXCLUSIVE_AGENT, 0.95, r"\bunder\s+(?:an?\s+)?exclusive\s+(?:agency|agent|listing|appointment|mandate)\b"),
    (S.ALREADY_HAS_EXCLUSIVE_AGENT, 0.9, r"\bexclusively\s+(?:marketed|listed|handled)\s+by\b"),
    (S.ALREADY_HAS_EXCLUSIVE_AGENT, 0.9, rf"\b(?:hubungi\s+)?{_AGENT}\s+(?:yang\s+)?(?:telah\s+)?dilantik\b"),

    # --- seeking_exclusive_agent ---------------------------------------------
    (S.SEEKING_EXCLUSIVE_AGENT, 0.95, rf"\b(?:looking\s+for|seeking|searching\s+for|need|needs|want|wants|prefer|require|mencari|cari|perlukan|perlu|nak)\s+(?:an?\s+|seorang\s+)?{_EXCL}\s+{_AGENT}\b"),
    (S.SEEKING_EXCLUSIVE_AGENT, 0.95, rf"\b{_AGENT}\s+{_EXCL}\s+(?:dicari|diperlukan)\b"),
    (S.SEEKING_EXCLUSIVE_AGENT, 0.95, rf"\b(?:mencari|cari|perlukan|perlu|nak|ingin)\s+(?:seorang\s+)?{_AGENT}\s+{_EXCL}\b"),
    (S.SEEKING_EXCLUSIVE_AGENT, 0.9, rf"\b(?:nak|ingin|mahu|hendak|mau|akan|nk)\s+lantik\s+(?:seorang\s+|satu\s+|1\s+)?{_AGENT}(?:\s+(?:sahaja|saja|je|eksklusif|tunggal))?\b"),
    (S.SEEKING_EXCLUSIVE_AGENT, 0.9, rf"\blantik\s+(?:seorang\s+|satu\s+|1\s+)?{_AGENT}\s+(?:sahaja|saja|je|eksklusif|tunggal)\b"),
    (S.SEEKING_EXCLUSIVE_AGENT, 0.9, rf"\b(?:need|needs|looking\s+for|want|seeking)\s+(?:one|1|a\s+single|single)\s+{_AGENT}\s+to\s+(?:handle|manage|settle|take\s+care\s+of|sell)\b"),
    (S.SEEKING_EXCLUSIVE_AGENT, 0.85, rf"\b(?:perlukan|perlu|mencari|cari|nak)\s+(?:seorang\s+)?{_AGENT}\s+(?:untuk\s+|utk\s+)?(?:uruskan|urus|jualkan|jual|sewakan|handle|pasarkan)\b"),
    (S.SEEKING_EXCLUSIVE_AGENT, 0.8, rf"\b(?:need|needs|looking\s+for|want)\s+(?:an?\s+)?{_AGENT}\s+to\s+(?:handle|manage|sell|market)\b"),

    # --- open_to_agent_appointment -------------------------------------------
    (S.OPEN_TO_AGENT_APPOINTMENT, 0.9, rf"\b{_ANY_AGENT}\s+(?:are\s+|is\s+)?(?:welcome|welcomed)\b"),
    (S.OPEN_TO_AGENT_APPOINTMENT, 0.9, rf"\b{_AGENT}\s+(?:adalah\s+)?dialu-?alukan\b"),
    (S.OPEN_TO_AGENT_APPOINTMENT, 0.9, r"\b(?:boleh|can|ok|okay|welcome\s+to)\s+co-?\s?broke?\b"),
    (S.OPEN_TO_AGENT_APPOINTMENT, 0.9, r"\bco-?\s?broke?\s+(?:ok|okay|welcome|allowed|boleh|dialu-?alukan)\b"),
    (S.OPEN_TO_AGENT_APPOINTMENT, 0.85, rf"\b{_AGENT}\s+(?:boleh|can|may|pun\s+boleh|also\s+can)\s+(?:pm|whatsapp|wasap|ws|contact|hubungi|call|message)\b"),
    (S.OPEN_TO_AGENT_APPOINTMENT, 0.85, rf"\b(?:looking\s+for|seeking|mencari|cari)\s+(?:an?\s+|seorang\s+)?(?:property\s+|real\s+estate\s+|hartanah\s+)?{_AGENT}\b"),
    (S.OPEN_TO_AGENT_APPOINTMENT, 0.8, rf"\b{_AGENT}\s+(?:ok|okay|friendly)\b"),
]

_COMPILED = [(status, weight, re.compile(p, re.IGNORECASE)) for status, weight, p in _RULES]

# Signals that the matched phrase may not be a sincere, complete statement.
_HEDGE = re.compile(r"\b(?:maybe|perhaps|might|mungkin|considering|tengok\s+dulu|tgk\s+dulu|if\s+any|belum\s+pasti)\b", re.I)
_SARCASM = re.compile(r"\b(?:lol|lmao|haha+|hehe+|kononnya|konon)\b|🙄|😂|🤣|/s\b", re.I)
_NEGATION_BEFORE = re.compile(rf"\b{_NEG}\s+(?:\w+\s+){{0,1}}$", re.I)
# An agent / exclusive keyword cut off by OCR or a "see more" truncation.
_TRUNCATED = re.compile(
    r"\b(?:ag|age|agen|agnt|eje|ej|excl|exclu|exclus|exclusi|exclusiv|eksk|ekskl|ekskl[ui]s?)(?:\.{2,}|…|-\s*$)",
    re.I,
)
_AGENT_WORD = re.compile(rf"\b{_AGENT}\b|\bco-?\s?broke?\b", re.I)

_REN = re.compile(r"\b(?:REN|PEA|E)\s*(?:no\.?|number)?\s*[:#.]?\s*(\d{3,6})\b")
_AGENT_NAME = re.compile(
    r"(?i:appointed\s+agent|sole\s+agent|exclusive\s+agent|ejen\s+(?:yang\s+)?dilantik|ejen\s+eksklusif|agent|ejen)"
    r"\s*[:\-–]\s*([A-Z][a-z'.]+(?:\s+(?:bin|binti|bt|bte|a/l|a/p|[A-Z][a-z'.]+)){0,4})"
)


@dataclass
class _Match:
    status: S
    confidence: float
    phrase: str
    source: EvidenceSource
    start: int
    end: int
    item_index: int
    flags: tuple[str, ...] = ()


def _source_factor(item: TextEvidence, config: ExclusiveAgentConfig) -> float:
    reliability = config.source_reliability.get(item.source.value, 0.8)
    return reliability * max(0.0, min(1.0, item.source_confidence))


def _find_matches(item: TextEvidence, index: int, config: ExclusiveAgentConfig) -> list[_Match]:
    text = item.text or ""
    factor = _source_factor(item, config)
    raw: list[_Match] = []
    for status, weight, pattern in _COMPILED:
        for m in pattern.finditer(text):
            raw.append(_Match(status, weight * factor, m.group(0), item.source, m.start(), m.end(), index))

    # Resolve overlaps: a rejection wins ("tak perlukan ejen untuk uruskan"),
    # then the longer span, then the higher weight.
    raw.sort(key=lambda m: (m.status is not S.REJECTS_AGENTS, -(m.end - m.start), -m.confidence))
    kept: list[_Match] = []
    for m in raw:
        if any(m.start < k.end and k.start < m.end for k in kept):
            continue
        kept.append(m)

    for m in kept:
        flags = []
        window_after = text[m.end:m.end + 3]
        window = text[max(0, m.start - 40): m.end + 40]
        if "?" in window_after:
            flags.append("question")
        if _HEDGE.search(window):
            flags.append("hedged")
        if _SARCASM.search(window):
            flags.append("sarcasm")
        if m.status in (S.SEEKING_EXCLUSIVE_AGENT, S.OPEN_TO_AGENT_APPOINTMENT) and _NEGATION_BEFORE.search(text[max(0, m.start - 20): m.start]):
            flags.append("negated")
        penalty = 1.0
        for f in flags:
            penalty *= 0.3 if f == "negated" else 0.6
        m.confidence *= penalty
        m.flags = tuple(flags)
    return kept


def _truncated_fragment(item: TextEvidence) -> Optional[str]:
    m = _TRUNCATED.search(item.text or "")
    if not m:
        return None
    start = max(0, m.start() - 25)
    return (item.text[start:m.end()]).strip()


def _noisy_or(values: Iterable[float]) -> float:
    p = 1.0
    for v in values:
        p *= 1.0 - v
    return 1.0 - p


def _extract_appointed_agent(evidence: list[TextEvidence]) -> tuple[Optional[str], Optional[str]]:
    name = ren = None
    for item in evidence:
        text = item.text or ""
        if ren is None:
            m = _REN.search(text)
            if m and re.search(r"\bREN\b", m.group(0), re.I):
                ren = f"REN {m.group(1)}"
            elif m and re.search(rf"\b{_AGENT}\b|appointed|dilantik", text, re.I):
                ren = m.group(0).strip()
        if name is None:
            m = _AGENT_NAME.search(text)
            if m:
                name = m.group(1).strip()
    return name, ren


_PREFERENCE = {
    S.SEEKING_EXCLUSIVE_AGENT: AgentContactPreference.EXCLUSIVE_AGENT_WANTED,
    S.OPEN_TO_AGENT_APPOINTMENT: AgentContactPreference.AGENTS_WELCOME,
    S.ALREADY_HAS_EXCLUSIVE_AGENT: AgentContactPreference.APPOINTED_AGENT_ONLY,
    S.REJECTS_AGENTS: AgentContactPreference.NO_AGENTS,
    S.NO_EVIDENCE: AgentContactPreference.UNKNOWN,
    S.UNCERTAIN: AgentContactPreference.UNKNOWN,
}


def _probability(status: S, confidence: float, per_status: dict[S, float], config: ExclusiveAgentConfig) -> float:
    """Probability the owner is looking to appoint an exclusive agent."""
    if status is S.SEEKING_EXCLUSIVE_AGENT:
        p = confidence
    elif status is S.OPEN_TO_AGENT_APPOINTMENT:
        p = 0.35 * confidence + config.no_evidence_prior
    elif status in (S.ALREADY_HAS_EXCLUSIVE_AGENT, S.REJECTS_AGENTS):
        p = 0.0
    elif status is S.NO_EVIDENCE:
        p = config.no_evidence_prior
    else:  # uncertain: blend whatever weak signals exist
        p = max(config.no_evidence_prior,
                per_status.get(S.SEEKING_EXCLUSIVE_AGENT, 0.0) * 0.8,
                per_status.get(S.OPEN_TO_AGENT_APPOINTMENT, 0.0) * 0.35)
        p *= 1.0 - max(per_status.get(S.REJECTS_AGENTS, 0.0), per_status.get(S.ALREADY_HAS_EXCLUSIVE_AGENT, 0.0))
    return round(max(0.0, min(1.0, p)), 3)


def classify_exclusive_agent(
    evidence: list[TextEvidence],
    config: ExclusiveAgentConfig = DEFAULT_CONFIG,
    now: Optional[datetime] = None,
) -> ExclusiveAgentResult:
    now = now or datetime.now(timezone.utc)
    name, ren = _extract_appointed_agent(evidence)

    def result(status, confidence, phrase, source, review, reason=None, per_status=None):
        has_agent = status is S.ALREADY_HAS_EXCLUSIVE_AGENT
        return ExclusiveAgentResult(
            exclusive_agent_status=status,
            exclusive_agent_probability=_probability(status, confidence, per_status or {}, config),
            exclusive_agent_confidence=round(confidence, 3),
            exclusive_agent_evidence=phrase,
            exclusive_agent_evidence_source=source,
            exclusive_agent_review_required=review,
            agent_contact_preference=_PREFERENCE[status],
            already_appointed_agent_name=name if has_agent else None,
            already_appointed_agent_ren=ren if has_agent else None,
            exclusive_agent_detected_at=now,
            review_reason=reason,
        )

    # 1. A reviewer's decision supersedes every automated signal.
    for item in evidence:
        if item.source is EvidenceSource.MANUALLY_VERIFIED and item.verified_status is not None:
            return result(item.verified_status, 1.0, item.text or None, EvidenceSource.MANUALLY_VERIFIED, False)

    # Comments by anyone other than the owner say nothing about owner intent.
    usable = [e for e in evidence if not (e.source is EvidenceSource.COMMENT and not e.author_is_owner)]

    matches: list[_Match] = []
    for i, item in enumerate(usable):
        matches.extend(_find_matches(item, i, config))

    if not matches:
        # Truncated agent keyword (e.g. OCR "Looking for exclusive ag...") -> review.
        for item in usable:
            fragment = _truncated_fragment(item)
            if fragment:
                return result(S.UNCERTAIN, 0.0, fragment, item.source, True, "truncated_text")
        # Low-confidence OCR/transcript mentioning agents at all -> review.
        for item in usable:
            if item.source_confidence < config.min_source_confidence and _AGENT_WORD.search(item.text or ""):
                m = _AGENT_WORD.search(item.text)
                return result(S.UNCERTAIN, 0.0, m.group(0), item.source, True, "low_source_confidence")
        return result(S.NO_EVIDENCE, 0.0, None, None, False)

    per_status: dict[S, float] = {}
    best: dict[S, _Match] = {}
    for m in matches:
        per_status.setdefault(m.status, 0.0)
        per_status[m.status] = _noisy_or([per_status[m.status], m.confidence])
        if m.status not in best or m.confidence > best[m.status].confidence:
            best[m.status] = m

    def strong(status: S) -> bool:
        return per_status.get(status, 0.0) >= config.confidence_threshold

    present = {s for s in per_status}
    top = max(present, key=lambda s: per_status[s])
    reason = None

    # 2. Resolve combinations of statuses.
    if S.ALREADY_HAS_EXCLUSIVE_AGENT in present and S.SEEKING_EXCLUSIVE_AGENT in present:
        m = best[top]
        return result(S.UNCERTAIN, per_status[top], m.phrase, m.source, True, "conflicting_signals", per_status)
    if S.ALREADY_HAS_EXCLUSIVE_AGENT in present and strong(S.ALREADY_HAS_EXCLUSIVE_AGENT):
        # "Exclusive listing, co-broke welcome" / "Exclusive listing, no co-broke".
        status = S.ALREADY_HAS_EXCLUSIVE_AGENT
    elif S.REJECTS_AGENTS in present and (S.SEEKING_EXCLUSIVE_AGENT in present or S.OPEN_TO_AGENT_APPOINTMENT in present):
        # Contradictory. Honour the rejection (avoid unwanted contact) but review.
        status, reason = S.REJECTS_AGENTS, "conflicting_signals"
    elif S.SEEKING_EXCLUSIVE_AGENT in present and S.OPEN_TO_AGENT_APPOINTMENT in present:
        status = S.SEEKING_EXCLUSIVE_AGENT if strong(S.SEEKING_EXCLUSIVE_AGENT) else S.OPEN_TO_AGENT_APPOINTMENT
    else:
        status = top

    m = best[status]
    confidence = per_status[status]
    if confidence < config.confidence_threshold:
        if status is S.REJECTS_AGENTS and reason:
            pass  # keep the rejection; it is already flagged for review
        else:
            flag_reason = ",".join(m.flags) if m.flags else "below_threshold"
            return result(S.UNCERTAIN, confidence, m.phrase, m.source, True, flag_reason, per_status)
    return result(status, confidence, m.phrase, m.source, reason is not None, reason, per_status)


def apply_exclusive_agent_detection(
    listing: Listing,
    config: ExclusiveAgentConfig = DEFAULT_CONFIG,
    now: Optional[datetime] = None,
) -> Optional[ExclusiveAgentResult]:
    """Classify a listing. Only listings with a public phone or email are eligible."""
    if not listing.public_contact:
        listing.exclusive_agent = None
        return None
    listing.exclusive_agent = classify_exclusive_agent(listing.all_evidence(), config, now)
    return listing.exclusive_agent
