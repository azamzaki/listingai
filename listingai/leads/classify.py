"""Filtering, Owner Confidence and Opportunity Score for discovered listings.

Everything here works only on information shown publicly on the listing:
title, description, price, location, date, category and the advertiser label
(e.g. Mudah's "Private" / "Company"). Phone numbers are never extracted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..regions import REGIONS, STATES, mentions

# --- location -------------------------------------------------------------------

PENANG_PLACES = REGIONS["Penang Island"] + REGIONS["Seberang Perai (Penang mainland)"] + ["Penang", "Pulau Pinang"]
KEDAH_PLACES = REGIONS["Kedah"] + ["Kedah"]
_OTHER_PLACES = ([p for k, v in REGIONS.items() if k not in ("Penang Island", "Seberang Perai (Penang mainland)", "Kedah")
                  for p in v] + [s for s in STATES if s not in ("Penang", "Pulau Pinang", "Kedah")])


def detect_state(*texts: str) -> Optional[str]:
    """'Penang', 'Kedah', 'other' or None when no place is named."""
    joined = " ".join(t for t in texts if t)
    # Longest names first so "Kepala Batas" or "Bandar Baharu" win over shorter overlaps.
    for state, places in (("Penang", PENANG_PLACES), ("Kedah", KEDAH_PLACES)):
        if any(mentions(joined, p) for p in places):
            return state
    if any(mentions(joined, p) for p in _OTHER_PLACES):
        return "other"
    return None


# --- property type ----------------------------------------------------------------

_TYPES = [
    # Land and commercial first: "tanah lot banglo" is land, "rumah kedai" is a shop house.
    ("Land", r"\btanah\b|\bland\b|\blot\s+(?:banglo|tanah)|acre|ekar"),
    ("Commercial", r"shop\s?lot|shop\s?house|rumah\s+kedai|\bkedai\b|office|pejabat|factory|kilang|warehouse|gudang|commercial|komersial"),
    ("Semi-D", r"semi[\s-]?d(?:etached)?|berkembar"),
    ("Bungalow", r"bungalow|banglo"),
    ("Townhouse", r"town\s?house"),
    ("Cluster", r"cluster|kluster"),
    ("Terrace", r"terrace|teres|link\s?house|rumah\s+(?:satu|dua|1|2)\s+tingkat|single\s+storey|double\s+storey|\d\s*(?:storey|sty|tingkat)"),
    ("Condominium", r"condo(?:minium)?|serviced\s+residence|service\s+apartment"),
    ("Apartment", r"apartment|apartmen|pangsapuri|flat|rumah\s+pangsa"),
]
_TYPE_RE = [(name, re.compile(rf"\b(?:{p})", re.I)) for name, p in _TYPES]
RESIDENTIAL = {"Terrace", "Semi-D", "Bungalow", "Townhouse", "Cluster", "Condominium", "Apartment", "House"}


def detect_property_type(category: str, title: str, description: str) -> Optional[str]:
    for text in (category, title, description):
        for name, rx in _TYPE_RE:
            if text and rx.search(text):
                return name
    if re.search(r"\b(?:house|rumah|home)\b", f"{category} {title}", re.I):
        return "House"
    return None


_RENT = re.compile(r"\b(?:for\s+rent|to\s+let|disewakan|untuk\s+disewa|sewa\s+bulanan|room\s+(?:for\s+)?rent|bilik\s+sewa)\b", re.I)
_SALE = re.compile(r"\b(?:for\s+sale|dijual|untuk\s+dijual|jual|sale)\b", re.I)

# --- agent / owner indicators -----------------------------------------------------

_REN = re.compile(r"\b(?:REN|REA|PEA)\s*(?:no\.?)?\s*[:.#-]?\s*\d{3,6}\b", re.I)
_AGENCY = re.compile(
    r"\b(?:realty|realtors?|real\s+estate|properties\s+sdn\.?\s*bhd|property\s+(?:consultants?|agency|agents?|negotiators?)|"
    r"estate\s+agen(?:cy|ts?)|real\s+estate\s+negotiator|negotiator|ejen\s+hartanah|perunding\s+hartanah|"
    r"registered\s+estate\s+agent|agency)\b", re.I)
_BRANDS = re.compile(
    r"\b(?:IQI|PropNex|Reapfield|Hartamas|Gather\s+Properties|Huttons|Kith\s*(?:&|and)\s*Kin|Zeta\s+Realty|"
    r"MIEA|Chester\s+Properties|Rina\s+Properties|Ivory\s+Properties)\b", re.I)
_AGENT_JARGON = re.compile(r"\b(?:co-?broke|cobroke|exclusive\s+listing|sole\s+agent|listing\s+id)\b", re.I)
_OWNER = re.compile(
    r"\b(?:direct\s+owner|owner\s+(?:sell(?:ing)?|jual|sale|sendiri)|by\s+owner|from\s+owner|dari\s+owner|"
    r"pemilik\s+(?:sendiri|asal)|dijual\s+oleh\s+pemilik|tuan\s+rumah|saya\s+owner|i\s*'?\s*a?m\s+the\s+owner|"
    r"no\s+agents?(?!\s*(?:fee|commission))|tanpa\s+ejen|tiada\s+ejen|owner\s+occupied\s+selling)\b", re.I)


_NEGATION = re.compile(r"\b(?:no|not|tanpa|bukan|tiada|tak|x|non)\s+(?:\w+\s+)?$", re.I)


def _negated(text: str, start: int) -> bool:
    """'no agency please', 'tanpa ejen hartanah': the word is being refused, not claimed."""
    return _NEGATION.search(text[max(0, start - 20):start]) is not None


@dataclass
class Signals:
    agent: list[str] = field(default_factory=list)  # human-readable evidence, e.g. "REN number: REN 12345"
    owner: list[str] = field(default_factory=list)


def find_signals(title: str, description: str, advertiser_type: str, advertiser_name: str) -> Signals:
    sig = Signals()
    text = f"{title}\n{description}"
    who = advertiser_name or ""
    for rx, label in ((_REN, "REN number"),):
        for m in rx.finditer(f"{text}\n{who}"):
            sig.agent.append(f"{label}: {m.group(0).strip()}")
    full = f"{text}\n{who}"
    for rx, label in ((_AGENCY, "Agency wording"), (_BRANDS, "Agency brand")):
        for m in rx.finditer(full):
            if not _negated(full, m.start()):
                sig.agent.append(f"{label}: {m.group(0).strip()}")
    for m in _AGENT_JARGON.finditer(text):
        if not _negated(text, m.start()):
            sig.agent.append(f"Agent wording: {m.group(0).strip()}")
    if (advertiser_type or "").lower() in ("company", "agent", "agency", "dealer", "business"):
        sig.agent.append(f"Advertiser type: {advertiser_type}")
    for m in _OWNER.finditer(text):
        sig.owner.append(m.group(0).strip())
    sig.agent = list(dict.fromkeys(sig.agent))
    sig.owner = list(dict.fromkeys(sig.owner))
    return sig


# --- owner confidence ---------------------------------------------------------------

VERY_HIGH, HIGH, MEDIUM, LOW = "VERY HIGH", "HIGH", "MEDIUM", "LOW"


def owner_confidence(advertiser_type: str, sig: Signals) -> tuple[str, int, Optional[str]]:
    """Return (level, score 0-100, rejection reason or None)."""
    private = (advertiser_type or "").lower() == "private"
    ren = [s for s in sig.agent if s.startswith("REN number")]
    if ren:
        return LOW, 5, f"Rejected — REN number detected ({ren[0].split(': ', 1)[1]})"
    agency = [s for s in sig.agent if s.startswith(("Agency", "Advertiser type"))]
    if agency:
        return LOW, 10, f"Rejected — agency detected ({agency[0]})"
    jargon = [s for s in sig.agent if s.startswith("Agent wording")]
    if jargon:
        return LOW, 20, f"Rejected — agent wording detected ({jargon[0].split(': ', 1)[1]})"
    if private and sig.owner:
        return VERY_HIGH, 95, None
    if private:
        return HIGH, 80, None
    if sig.owner:
        return MEDIUM, 60, None  # owner wording but the site gave no advertiser label
    return MEDIUM, 45, None


# --- opportunity score ----------------------------------------------------------------

_URGENT = re.compile(
    r"\b(?:urgent(?:ly)?|cepat|segera|nak\s+cepat|need\s+(?:to\s+)?(?:sell|cash)|quick\s+sale|fast\s+sale|must\s+sell|"
    r"below\s+(?:market|valuation|bank\s+value)|bawah\s+(?:harga\s+)?(?:pasaran|valuation|nilai)|fire\s+sale|"
    r"pindah|berpindah|relocat\w*|migrat\w*|harga\s+runtuh|rugi)\b", re.I)
_REDUCED = re.compile(r"\b(?:reduced|price\s+(?:drop|reduced|cut)|harga\s+(?:turun|baru|terbaru)|turun\s+harga|new\s+price|"
                      r"discount|diskaun)\b", re.I)
_NEGO = re.compile(r"\b(?:nego(?:tiable)?|boleh\s+runding|runding)\b", re.I)


def opportunity_score(posted_at: Optional[datetime], title: str, description: str, price: Optional[int],
                      price_dropped_rm: int = 0, now: Optional[datetime] = None) -> tuple[int, list[str]]:
    """How much an owner may benefit from professional marketing. Returns (score, reasons)."""
    now = now or datetime.now(timezone.utc)
    score, reasons = 30, []
    text = f"{title}\n{description}"
    if posted_at:
        days = (now - posted_at).days
        if days >= 90:
            score += 25; reasons.append(f"Listed {days} days ago")
        elif days >= 30:
            score += 15; reasons.append(f"Listed {days} days ago")
        elif days >= 14:
            score += 8; reasons.append(f"Listed {days} days ago")
    m = _URGENT.search(text)
    if m:
        score += 20; reasons.append(f"Urgent wording: “{m.group(0)}”")
    if price_dropped_rm > 0:
        score += 25; reasons.append(f"Price dropped RM{price_dropped_rm:,} since first seen")
    else:
        m = _REDUCED.search(text)
        if m:
            score += 15; reasons.append(f"Price-reduction wording: “{m.group(0)}”")
    desc_len = len((description or "").strip())
    if desc_len == 0:
        score += 12; reasons.append("No description")
    elif desc_len < 150:
        score += 8; reasons.append(f"Short description ({desc_len} characters)")
    if price is None:
        score += 5; reasons.append("No asking price shown")
    if _NEGO.search(text):
        score += 5; reasons.append("Price negotiable")
    return min(score, 100), reasons
