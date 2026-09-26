"""Optional OpenAI second opinion for exclusive-agent intent.

The phrase rules always run first. The AI is used to catch wording the rules
miss, under the same constraints:

* Its supporting phrase must appear word-for-word in the listing text, or its
  answer is discarded. Truncated text is never completed.
* It cannot overturn a manual verification or an `uncertain` rule result
  (those stay in the Review Queue; the AI's view is added as a note).
* When the rules and the AI disagree on a definite status, the listing
  becomes `uncertain` and goes to review.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import DEFAULT_CONFIG, ExclusiveAgentConfig
from .exclusive_agent import _PREFERENCE, _probability, _source_factor, classify_exclusive_agent
from .models import EvidenceSource, ExclusiveAgentResult, ExclusiveAgentStatus as S, TextEvidence

API_BASE = "https://api.openai.com/v1"

SYSTEM_PROMPT = """You classify Malaysian property listings (English, Bahasa Malaysia or mixed) by whether the OWNER wants to appoint an exclusive real estate agent.

Statuses:
- seeking_exclusive_agent: owner asks for an exclusive/sole/single agent, or asks for an agent to handle or sell the property ("perlukan ejen untuk uruskan jualan", "nak lantik seorang ejen sahaja", "need one agent to handle everything").
- open_to_agent_appointment: agents are welcome or may contact ("agents welcome", "boleh co-broke", "agent boleh PM", "ejen dialu-alukan").
- already_has_exclusive_agent: an agent is already appointed ("exclusive listing", "sole agent appointed", "sudah lantik ejen", "under exclusive agency").
- rejects_agents: owner does not want agents ("no agent", "direct buyer only", "no co-broke", "owner deal only", "ejen jangan hubungi"). "No agent fee" is NOT a rejection.
- no_evidence: nothing about appointing, accepting or rejecting agents.
- uncertain: ambiguous, sarcastic, a question, or text that is cut off.

Rules:
- Being a direct owner is NOT evidence of wanting an agent.
- Only comments written by the owner count.
- "evidence" must be a short phrase copied EXACTLY, character for character, from one numbered text. Never complete cut-off words. Use null for no_evidence.
- Only give appointed agent name / REN number if they appear in the text.

Reply with JSON only:
{"status": "...", "confidence": 0.0-1.0, "evidence_item": <number or null>, "evidence": "<exact phrase or null>", "appointed_agent_name": null, "appointed_agent_ren": null, "reason": "<max 15 words>"}"""

Opener = Callable[..., object]


class OpenAIError(Exception):
    pass


def _request(method: str, path: str, api_key: str, payload: Optional[dict] = None,
             timeout: float = 30, opener: Optional[Opener] = None) -> dict:
    opener = opener or urllib.request.urlopen
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        with opener(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        if e.code == 401:
            raise OpenAIError("OpenAI rejected the API key. Check it was copied in full.") from None
        if e.code == 429:
            raise OpenAIError("OpenAI rate limit or quota reached. Check billing on platform.openai.com.") from None
        raise OpenAIError(f"OpenAI error {e.code}: {detail or e.reason}") from None
    except urllib.error.URLError as e:
        raise OpenAIError(f"Could not reach OpenAI: {e.reason}") from None


def test_api_key(api_key: str, model: str, opener: Optional[Opener] = None) -> str:
    """Raise OpenAIError if the key or model does not work; return a success message."""
    if not api_key:
        raise OpenAIError("No API key saved.")
    _request("GET", f"/models/{model}", api_key, opener=opener, timeout=15)
    return f"Connected. Model {model} is available."


def ask_openai(evidence: list[TextEvidence], api_key: str, model: str, opener: Optional[Opener] = None) -> dict:
    lines = []
    for i, item in enumerate(evidence, 1):
        note = ""
        if item.source is EvidenceSource.COMMENT:
            note = " (by owner)" if item.author_is_owner else " (NOT by owner - ignore)"
        if item.source_confidence < 1:
            note += f" (read quality {round(item.source_confidence * 100)}%)"
        lines.append(f"[{i}] {item.source.value}{note}:\n{item.text}")
    body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(lines)},
        ],
    }
    data = _request("POST", "/chat/completions", api_key, body, opener=opener)
    try:
        return json.loads(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise OpenAIError("OpenAI returned an answer that was not valid JSON.") from None


def _exact(text: str, phrase: Optional[str]) -> Optional[str]:
    """The phrase as it appears in text (case-insensitive find), or None."""
    if not phrase or not text:
        return None
    i = text.lower().find(phrase.strip().lower())
    return text[i:i + len(phrase.strip())] if i >= 0 else None


def combine(rule: ExclusiveAgentResult, ai: dict, evidence: list[TextEvidence],
            config: ExclusiveAgentConfig = DEFAULT_CONFIG) -> ExclusiveAgentResult:
    try:
        ai_status = S(ai.get("status"))
        ai_conf = max(0.0, min(1.0, float(ai.get("confidence") or 0)))
    except (ValueError, TypeError):
        return replace(rule, review_reason=_note(rule.review_reason, "AI answer unreadable"))

    usable = [e for e in evidence if not (e.source is EvidenceSource.COMMENT and not e.author_is_owner)]
    item = None
    idx = ai.get("evidence_item")
    if isinstance(idx, int) and 1 <= idx <= len(evidence) and evidence[idx - 1] in usable:
        item = evidence[idx - 1]
    phrase = _exact(item.text, ai.get("evidence")) if item else None
    if ai_status not in (S.NO_EVIDENCE, S.UNCERTAIN) and phrase is None:
        # The AI quoted something that is not in the post: never trust it.
        return replace(rule, review_reason=_note(rule.review_reason, "AI quote not found in post"))

    if rule.classified_by == "manual":
        return rule
    if rule.exclusive_agent_status is S.UNCERTAIN:
        return replace(rule, classified_by="rules+openai",
                       review_reason=_note(rule.review_reason, f"AI suggests {ai_status.value}"))
    if ai_status is S.UNCERTAIN:
        if rule.exclusive_agent_status is S.NO_EVIDENCE:
            return replace(rule, exclusive_agent_status=S.UNCERTAIN, exclusive_agent_review_required=True,
                           agent_contact_preference=_PREFERENCE[S.UNCERTAIN], classified_by="rules+openai",
                           exclusive_agent_evidence=phrase, exclusive_agent_evidence_source=item.source if phrase else None,
                           review_reason="AI found ambiguous wording")
        return replace(rule, classified_by="rules+openai")
    if ai_status is S.NO_EVIDENCE:
        return replace(rule, classified_by="rules+openai")

    source_conf = ai_conf * _source_factor(item, config)
    rule_status = rule.exclusive_agent_status
    if rule_status is ai_status:
        conf = max(rule.exclusive_agent_confidence, source_conf)
        return replace(rule, exclusive_agent_confidence=round(conf, 3),
                       exclusive_agent_probability=_probability(ai_status, conf, {}, config), classified_by="rules+openai")
    if rule_status is not S.NO_EVIDENCE:
        return replace(rule, exclusive_agent_status=S.UNCERTAIN, exclusive_agent_review_required=True,
                       agent_contact_preference=_PREFERENCE[S.UNCERTAIN], classified_by="rules+openai",
                       exclusive_agent_probability=_probability(S.UNCERTAIN, 0, {}, config),
                       review_reason=f"Rules say {rule_status.value}, AI says {ai_status.value}")

    # Rules found nothing; the AI found a quoted statement.
    status = ai_status if source_conf >= config.confidence_threshold else S.UNCERTAIN
    has_agent = status is S.ALREADY_HAS_EXCLUSIVE_AGENT
    return replace(
        rule,
        exclusive_agent_status=status,
        exclusive_agent_confidence=round(source_conf, 3),
        exclusive_agent_probability=_probability(status, source_conf, {}, config),
        exclusive_agent_evidence=phrase,
        exclusive_agent_evidence_source=item.source,
        exclusive_agent_review_required=status is S.UNCERTAIN,
        agent_contact_preference=_PREFERENCE[status],
        already_appointed_agent_name=_exact(item.text, ai.get("appointed_agent_name")) if has_agent else None,
        already_appointed_agent_ren=_exact(item.text, ai.get("appointed_agent_ren")) if has_agent else None,
        review_reason=None if status is not S.UNCERTAIN else "AI confidence below threshold",
        classified_by="openai",
    )


def _note(existing: Optional[str], note: str) -> str:
    return f"{existing}; {note}" if existing else note


def classify_with_ai(evidence: list[TextEvidence], api_key: str, model: str,
                     config: ExclusiveAgentConfig = DEFAULT_CONFIG, now: Optional[datetime] = None,
                     opener: Optional[Opener] = None) -> ExclusiveAgentResult:
    """Rules first, then OpenAI. On any OpenAI failure the rule result is kept."""
    rule = classify_exclusive_agent(evidence, config, now or datetime.now(timezone.utc))
    if rule.classified_by == "manual" or not api_key:
        return rule
    try:
        ai = ask_openai(evidence, api_key, model, opener)
    except OpenAIError as e:
        return replace(rule, review_reason=_note(rule.review_reason, f"AI unavailable: {e}"))
    return combine(rule, ai, evidence, config)
