"""Extract listing details (phone, email, price, location, owner) from post text.

Rules run first. With an OpenAI key, the AI fills in what the rules could not
find, and its answers are checked against the text: a phone number or email
must appear in the post, and a location must be named in it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .llm import Opener, OpenAIError, _request

# Common Malaysian areas, used when no campaign place matches. Longer names first.
KNOWN_PLACES = sorted([
    "Bandar Baru Bangi", "Bangi", "Kajang", "Semenyih", "Cyberjaya", "Putrajaya", "Seri Kembangan", "Serdang",
    "Puchong", "Shah Alam", "Klang", "Subang Jaya", "Petaling Jaya", "Damansara", "Kota Damansara", "Mont Kiara",
    "Cheras", "Ampang", "Setapak", "Wangsa Maju", "Gombak", "Rawang", "Selayang", "Kepong", "Sungai Buloh",
    "Bukit Jalil", "Sri Petaling", "Kuala Lumpur", "Seremban", "Nilai", "Sepang", "Dengkil", "Kota Kemuning",
    "Bukit Jelutong", "Setia Alam", "Kapar", "Banting", "Bukit Beruntung", "Kuala Selangor", "Hulu Langat",
    "Balakong", "Bandar Mahkota Cheras", "Johor Bahru", "Iskandar Puteri", "Skudai", "Kulai", "Pasir Gudang",
    "Melaka", "Ipoh", "Penang", "George Town", "Bayan Lepas", "Butterworth", "Bukit Mertajam", "Alor Setar",
    "Sungai Petani", "Kota Bharu", "Kuala Terengganu", "Kuantan", "Kota Kinabalu", "Kuching", "Miri",
], key=len, reverse=True)

_PHONE = re.compile(r"(?<!\d)(?:\+?6)?0(?:1\d[\s-]?\d{3,4}[\s-]?\d{4}|[3-9][\s-]?\d{3,4}[\s-]?\d{4})(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PRICE = re.compile(
    r"\bRM\s?(\d{1,3}(?:[,.]\d{3})+|\d+(?:\.\d+)?)\s?(k|K|mil|million|juta|j|m|M)?\b"
    r"|\b(\d+(?:\.\d+)?)\s?(k|K|juta)\b(?!\s*(?:sqft|kaki|km))",
)
_OWNER = re.compile(r"\b(?:direct\s+owner|owner\s+(?:sell|jual|sale)|dari\s+owner|tuan\s+rumah|pemilik|owner\s+sendiri|saya\s+owner|i'?m\s+the\s+owner)\b", re.I)
_AGENT_POSTER = re.compile(r"\b(?:REN|PEA|E)\s*\d{3,6}\b|\bnegotiator\b|\brealty\b|\bproperties\s+sdn\b", re.I)


@dataclass
class Extracted:
    phone: Optional[str] = None
    email: Optional[str] = None
    price: Optional[int] = None
    location: Optional[str] = None
    is_direct_owner: Optional[bool] = None
    extracted_by: str = "rules"


def _price_value(num: str, unit: Optional[str]) -> Optional[int]:
    unit = (unit or "").lower()
    if re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", num):
        value = float(num.replace(",", "").replace(".", ""))
    else:
        value = float(num)
    if unit == "k":
        value *= 1_000
    elif unit in ("mil", "million", "juta", "j", "m"):
        value *= 1_000_000
    return int(value) if value > 0 else None


def find_price(text: str) -> Optional[int]:
    """The asking price: the first amount of at least RM50,000, else the first amount."""
    values = []
    for m in _PRICE.finditer(text or ""):
        v = _price_value(m.group(1), m.group(2)) if m.group(1) else _price_value(m.group(3), m.group(4))
        if v:
            values.append(v)
    big = [v for v in values if v >= 50_000]
    return big[0] if big else (values[0] if values else None)


def find_location(text: str, places: Iterable[str] = ()) -> Optional[str]:
    for place in sorted({p.strip() for p in places if p.strip()}, key=len, reverse=True) + KNOWN_PLACES:
        words = r"\s+".join(re.escape(w) for w in place.split())
        if re.search(rf"(?<!\w){words}(?!\w)", text or "", re.I):
            return place
    return None


def extract_with_rules(text: str, places: Iterable[str] = ()) -> Extracted:
    phone = _PHONE.search(text or "")
    email = _EMAIL.search(text or "")
    owner = None
    if _OWNER.search(text or ""):
        owner = not _AGENT_POSTER.search(text or "")
    return Extracted(
        phone=phone.group(0).strip() if phone else None,
        email=email.group(0) if email else None,
        price=find_price(text),
        location=find_location(text, places),
        is_direct_owner=owner,
    )


_PROMPT = """Extract the property listing details from this Malaysian social media post.
Only use information written in the post. Use null when something is not stated.
Reply with JSON only:
{"phone": "<as written or null>", "email": "<or null>", "price_rm": <number or null>, "location": "<area/town as written or null>", "posted_by_owner": <true, false or null>}"""


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def extract_listing(text: str, places: Iterable[str] = (), api_key: str = "", model: str = "",
                    opener: Optional[Opener] = None) -> Extracted:
    places = list(places)
    found = extract_with_rules(text, places)
    missing = (found.phone is None and found.email is None) or found.price is None or found.location is None
    if not api_key or not missing:
        return found
    try:
        data = _request("POST", "/chat/completions", api_key, {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": _PROMPT}, {"role": "user", "content": text}],
        }, opener=opener)
        ai = json.loads(data["choices"][0]["message"]["content"])
    except (OpenAIError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return found

    used = False
    phone = ai.get("phone")
    if found.phone is None and isinstance(phone, str) and len(_digits(phone)) >= 9 and _digits(phone) in _digits(text):
        found.phone, used = phone.strip(), True
    email = ai.get("email")
    if found.email is None and isinstance(email, str) and email.strip() and email.strip().lower() in text.lower():
        found.email, used = email.strip(), True
    loc = ai.get("location")
    if found.location is None and isinstance(loc, str) and loc.strip() and loc.strip().lower() in text.lower():
        found.location, used = loc.strip(), True
    price = ai.get("price_rm")
    if found.price is None and isinstance(price, (int, float)) and 1_000 <= price <= 100_000_000:
        found.price, used = int(price), True
    if found.is_direct_owner is None and isinstance(ai.get("posted_by_owner"), bool):
        found.is_direct_owner, used = ai["posted_by_owner"], True
    if used:
        found.extracted_by = "rules+openai"
    return found


def split_posts(blob: str) -> list[str]:
    """Posts separated by a line containing only --- (three or more dashes)."""
    parts = re.split(r"^\s*-{3,}\s*$", blob or "", flags=re.M)
    return [p.strip() for p in parts if p.strip()]
