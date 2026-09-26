"""Location campaigns.

A campaign targets one or more areas (e.g. "Bangi", "Bandar Baru Bangi",
"Kajang") with an optional price range. A listing is in a target location
when it matches at least one active campaign. With no campaigns defined, each
listing keeps its own `matches_target_location` value (e.g. from the CSV).
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from .models import Listing
from .settings import _write_private, data_dir


@dataclass
class Campaign:
    id: str
    name: str
    locations: list[str]
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    active: bool = True
    alert_email: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def matches(self, listing: Listing) -> bool:
        if not self.active or not self.locations:
            return False
        if listing.price is not None:
            if self.min_price is not None and listing.price < self.min_price:
                return False
            if self.max_price is not None and listing.price > self.max_price:
                return False
        # Match the listing's location field first, then the post text.
        haystacks = [listing.location or "", listing.caption or ""]
        return any(_contains_place(h, place) for place in self.locations for h in haystacks)


def _contains_place(text: str, place: str) -> bool:
    place = place.strip()
    if not place:
        return False
    words = r"\s+".join(re.escape(w) for w in place.split())
    return re.search(rf"(?<![\w]){words}(?![\w])", text, re.IGNORECASE) is not None


def parse_locations(text: str) -> list[str]:
    """Comma- or newline-separated place names, deduplicated, order kept."""
    out: list[str] = []
    for part in re.split(r"[,\n;]+", text or ""):
        part = " ".join(part.split())
        if part and part.lower() not in {p.lower() for p in out}:
            out.append(part)
    return out


def new_campaign(name: str, locations: Iterable[str], min_price: Optional[int] = None,
                 max_price: Optional[int] = None, alert_email: str = "", active: bool = True) -> Campaign:
    name = " ".join((name or "").split())
    locations = [l for l in locations if l.strip()]
    if not name:
        raise ValueError("Give the campaign a name.")
    if not locations:
        raise ValueError("Add at least one location.")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValueError("Minimum price is higher than maximum price.")
    return Campaign(secrets.token_hex(4), name, list(locations), min_price, max_price, active, alert_email.strip())


def apply_campaigns(listings: Iterable[Listing], campaigns: list[Campaign]) -> None:
    """Tag each listing with the campaigns it matches and set its target-location flag."""
    has_active = any(c.active for c in campaigns)
    for listing in listings:
        listing.campaign_ids = [c.id for c in campaigns if c.matches(listing)]
        if has_active:
            listing.matches_target_location = bool(listing.campaign_ids)


def campaigns_path():
    return data_dir() / "campaigns.json"


def load_campaigns() -> list[Campaign]:
    path = campaigns_path()
    if not path.exists():
        return []
    return [Campaign(**c) for c in json.loads(path.read_text(encoding="utf-8") or "[]")]


def save_campaigns(campaigns: list[Campaign]) -> None:
    _write_private(campaigns_path(), json.dumps([asdict(c) for c in campaigns], ensure_ascii=False, indent=2))
