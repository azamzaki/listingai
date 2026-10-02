"""Mudah.my: search URLs and parsers for search-result and ad pages.

Parsing tries, in order:
  1. the Next.js data blob (<script id="__NEXT_DATA__">) that Mudah pages carry,
  2. JSON-LD structured data (<script type="application/ld+json">),
  3. a fallback over ad links (…-<id>.htm) with a price nearby.
Each strategy reports what it found so a zero result can be traced to the
exact reason (no data blob, unknown field names, no ad links, …).
"""

from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from ..extract import find_price

BASE = "https://www.mudah.my"

# Region slugs and category paths. Configurable because sites rename paths;
# a wrong path shows up in the run log as "HTTP 404 … the search address may have changed".
DEFAULT_SEARCHES = {
    "Penang": "/penang/properties-for-sale",
    "Kedah": "/kedah/properties-for-sale",
}
PAGE_PARAM = "o"  # Mudah paginates with ?o=2, ?o=3 …


def search_url(path: str, page: int = 1, extra_query: str = "") -> str:
    url = urljoin(BASE, path)
    params = [p for p in [extra_query.lstrip("?&")] if p]
    if page > 1:
        params.append(f"{PAGE_PARAM}={page}")
    return url + (("&" if "?" in url else "?") + "&".join(params) if params else "")


@dataclass
class RawListing:
    url: str
    title: str = ""
    price: Optional[int] = None
    location: str = ""
    posted_at: Optional[datetime] = None
    posted_text: str = ""
    advertiser_type: str = ""     # "Private", "Company" or ""
    advertiser_name: str = ""     # shop / company name shown on the ad, never a person's phone
    description: str = ""
    category: str = ""
    source_id: str = ""
    photos: Optional[int] = None
    via: str = ""                 # which parse strategy produced it


@dataclass
class ParseReport:
    listings: list[RawListing] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # what each strategy found, for the run log

    def note(self, text: str) -> None:
        self.notes.append(text)


# --- helpers ------------------------------------------------------------------------

_AD_URL = re.compile(r"(?:https?://(?:www\.)?mudah\.my)?/[^\s\"'<>?#]*?-(\d{6,})\.htm", re.I)

_KEYS = {
    "title": ("subject", "title", "name", "adTitle", "ad_title"),
    "price": ("price", "priceValue", "price_value", "rawPrice", "listPrice", "amount"),
    "url": ("adViewUrl", "adUrl", "ad_url", "url", "link", "permalink", "shareUrl", "share_url", "href"),
    "id": ("adId", "ad_id", "listId", "list_id", "id"),
    "location": ("subareaName", "subarea_name", "areaName", "area_name", "subarea", "area", "location",
                 "regionName", "region_name", "region", "address", "state"),
    "date": ("listTime", "list_time", "date", "postedDate", "posted_date", "createdAt", "created_at",
             "publishedAt", "published_at", "updatedAt", "updated_at", "time", "datePosted"),
    "description": ("body", "description", "adBody", "ad_body", "content"),
    "category": ("categoryName", "category_name", "category", "subCategoryName", "propertyType", "property_type"),
    "advertiser": ("companyAd", "company_ad", "isCompany", "is_company", "adType", "ad_type", "posterType",
                   "poster_type", "sellerType", "seller_type", "accountType", "account_type", "type"),
    "advertiser_name": ("companyName", "company_name", "storeName", "store_name", "shopName", "shop_name",
                        "agencyName", "agency_name"),
    "photos": ("imageCount", "image_count", "photoCount", "photo_count", "images", "photos"),
}


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _text(v: Any) -> str:
    if isinstance(v, dict):
        v = _first(v, ("name", "label", "text", "value", "title")) or ""
    if isinstance(v, list):
        v = ", ".join(_text(x) for x in v if x)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", str(v or ""))).split())


def _price(v: Any) -> Optional[int]:
    if isinstance(v, dict):
        v = _first(v, ("value", "amount", "price", "raw"))
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if v >= 1000 else None
    if isinstance(v, str):
        if re.search(r"rm|k\b|juta|mil", v, re.I):
            return find_price(v if "RM" in v.upper() else f"RM{v}")
        digits = v.replace(",", "").strip()
        try:
            n = float(digits)
        except ValueError:
            return None
        return int(n) if n >= 1000 else None
    return None


def parse_date(v: Any, now: Optional[datetime] = None) -> tuple[Optional[datetime], str]:
    """ISO strings, epoch seconds/ms, or Mudah-style 'Today 10:15', 'Yesterday', '3 days ago', '12 Sep'."""
    now = now or datetime.now(timezone.utc)
    if v is None or v == "":
        return None, ""
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        if n > 1e12:
            n /= 1000
        if 1e9 < n < 4e9:
            return datetime.fromtimestamp(n, timezone.utc), str(v)
        return None, str(v)
    s = _text(v)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)), s
    except ValueError:
        pass
    low = s.lower()
    if low.startswith(("today", "hari ini")):
        return now, s
    if low.startswith(("yesterday", "semalam")):
        return now - timedelta(days=1), s
    m = re.match(r"(\d+)\s*(minute|min|hour|jam|day|hari|week|minggu|month|bulan)s?\s*(?:ago|lalu|yang lalu)?", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"minute": timedelta(minutes=n), "min": timedelta(minutes=n), "hour": timedelta(hours=n),
                 "jam": timedelta(hours=n), "day": timedelta(days=n), "hari": timedelta(days=n),
                 "week": timedelta(weeks=n), "minggu": timedelta(weeks=n), "month": timedelta(days=30 * n),
                 "bulan": timedelta(days=30 * n)}[unit]
        return now - delta, s
    for fmt in ("%d %b %Y", "%d %B %Y", "%d %b", "%d %B", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:20].strip(), fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=now.year)
                if dt.replace(tzinfo=timezone.utc) > now + timedelta(days=1):
                    dt = dt.replace(year=now.year - 1)
            return dt.replace(tzinfo=timezone.utc), s
        except ValueError:
            continue
    return None, s


def _advertiser(v: Any) -> str:
    if isinstance(v, bool):
        return "Company" if v else "Private"
    s = _text(v).lower()
    if s in ("p", "private", "individual", "personal", "owner", "persendirian", "peribadi"):
        return "Private"
    if s in ("c", "company", "business", "dealer", "agent", "agency", "syarikat", "pro", "professional"):
        return "Company"
    return ""


def canonical_url(url: str) -> str:
    """Same ad, same key: https, www host, no query string or fragment."""
    p = urlparse(urljoin(BASE, url))
    host = "www.mudah.my" if p.netloc.lower().endswith("mudah.my") else p.netloc.lower()
    return urlunparse(("https", host, p.path, "", "", ""))


def _from_dict(d: dict, via: str) -> Optional[RawListing]:
    title = _text(_first(d, _KEYS["title"]))
    raw_url = _first(d, _KEYS["url"])
    url = raw_url if isinstance(raw_url, str) else ""
    if url and not _AD_URL.search(url):
        url = ""
    if not title or not url:
        return None
    posted, posted_text = parse_date(_first(d, _KEYS["date"]))
    m = _AD_URL.search(url)
    photos = _first(d, _KEYS["photos"])
    return RawListing(
        url=canonical_url(url), title=title[:300], price=_price(_first(d, _KEYS["price"])),
        location=_text(_first(d, _KEYS["location"]))[:200], posted_at=posted, posted_text=posted_text,
        advertiser_type=_advertiser(_first(d, _KEYS["advertiser"])),
        advertiser_name=_text(_first(d, _KEYS["advertiser_name"]))[:120],
        description=_text(_first(d, _KEYS["description"]))[:4000], category=_text(_first(d, _KEYS["category"]))[:120],
        source_id=str(_first(d, _KEYS["id"]) or (m.group(1) if m else "")),
        photos=len(photos) if isinstance(photos, list) else (photos if isinstance(photos, int) else None),
        via=via,
    )


def _walk(obj: Any, found: list[dict], depth: int = 0) -> None:
    if depth > 40:
        return
    if isinstance(obj, dict):
        keys = set(obj)
        has_title = keys & set(_KEYS["title"])
        has_url = any(isinstance(obj.get(k), str) and _AD_URL.search(obj.get(k)) for k in _KEYS["url"] if k in obj)
        if has_title and has_url:
            found.append(obj)
        for v in obj.values():
            _walk(v, found, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, found, depth + 1)


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[dict, str]] = []
        self.links: list[tuple[str, int]] = []   # (href, token index)
        self.tokens: list[str] = []
        self._cur: Optional[dict] = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self._cur, self._buf = a, []
        elif tag == "a" and a.get("href"):
            self.links.append((a["href"], len(self.tokens)))

    def handle_endtag(self, tag):
        if tag == "script" and self._cur is not None:
            self.scripts.append((self._cur, "".join(self._buf)))
            self._cur = None

    def handle_data(self, data):
        if self._cur is not None:
            self._buf.append(data)
        elif data.strip():
            self.tokens.append(data.strip())


def parse_search_page(html: str) -> ParseReport:
    rep = ParseReport()
    p = _Scripts()
    p.feed(html)
    seen: set[str] = set()

    def add(item: Optional[RawListing]) -> None:
        if item and item.url not in seen:
            seen.add(item.url)
            rep.listings.append(item)

    # 1. Next.js data
    nd = next((body for attrs, body in p.scripts if attrs.get("id") == "__NEXT_DATA__"), None)
    if nd is None:
        rep.note("__NEXT_DATA__: not present on the page")
    else:
        try:
            data = json.loads(nd)
            dicts: list[dict] = []
            _walk(data, dicts)
            before = len(rep.listings)
            for d in dicts:
                add(_from_dict(d, "next_data"))
            rep.note(f"__NEXT_DATA__: {len(dicts)} ad-like objects, {len(rep.listings) - before} listings parsed")
            if dicts and len(rep.listings) == before:
                rep.note(f"__NEXT_DATA__ field names seen: {sorted(dicts[0].keys())[:40]}")
        except json.JSONDecodeError as e:
            rep.note(f"__NEXT_DATA__: present but not valid JSON ({e})")

    # 2. JSON-LD
    ld_blocks = [body for attrs, body in p.scripts if (attrs.get("type") or "").lower() == "application/ld+json"]
    before = len(rep.listings)
    for body in ld_blocks:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        for item in _ld_items(data):
            add(item)
    rep.note(f"JSON-LD: {len(ld_blocks)} blocks, {len(rep.listings) - before} new listings")

    # 3. Ad links with a price nearby
    before = len(rep.listings)
    ad_links = [(h, i) for h, i in p.links if _AD_URL.search(h)]
    for href, i in ad_links:
        url = canonical_url(_AD_URL.search(href).group(0))
        if url in seen:
            continue
        near = " ".join(p.tokens[i:i + 12])
        price = find_price(near)
        title = next((t for t in p.tokens[i:i + 6] if len(t) >= 12 and not re.match(r"^RM", t, re.I)), "")
        if title and price:
            add(RawListing(url=url, title=title[:300], price=price, via="links",
                           source_id=_AD_URL.search(href).group(1)))
    rep.note(f"Ad links: {len(ad_links)} found, {len(rep.listings) - before} new listings with a title and price")
    return rep


def _ld_items(data: Any) -> list[RawListing]:
    out: list[RawListing] = []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, dict):
            continue
        if "@graph" in cur:
            stack.append(cur["@graph"])
        if "itemListElement" in cur:
            stack.append(cur["itemListElement"])
        item = cur.get("item") if isinstance(cur.get("item"), dict) else cur
        url = item.get("url") or item.get("@id") or ""
        if isinstance(url, str) and _AD_URL.search(url) and (item.get("name") or item.get("headline")):
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            posted, posted_text = parse_date(item.get("datePosted") or item.get("datePublished") or offers.get("validFrom"))
            out.append(RawListing(
                url=canonical_url(url), title=_text(item.get("name") or item.get("headline"))[:300],
                price=_price(offers.get("price") if isinstance(offers, dict) else None),
                location=_text((item.get("address") or {}).get("addressLocality") if isinstance(item.get("address"), dict) else item.get("address")),
                posted_at=posted, posted_text=posted_text, description=_text(item.get("description"))[:4000],
                source_id=_AD_URL.search(url).group(1), via="json_ld"))
    return out


_LABEL_PRIVATE = re.compile(r"\b(?:Private\s+(?:seller|advertiser|ad)|Persendirian|Iklan\s+peribadi)\b", re.I)
_LABEL_COMPANY = re.compile(r"\b(?:Company\s+(?:seller|advertiser|ad)|Syarikat|Pro\s+seller|Verified\s+(?:agent|dealer))\b", re.I)


def parse_ad_page(html: str, url: str) -> tuple[Optional[RawListing], list[str]]:
    """Details from a single ad page. Returns (listing or None, notes)."""
    notes: list[str] = []
    p = _Scripts()
    p.feed(html)
    want = canonical_url(url)
    best: Optional[RawListing] = None
    nd = next((body for attrs, body in p.scripts if attrs.get("id") == "__NEXT_DATA__"), None)
    if nd:
        try:
            dicts: list[dict] = []
            _walk(json.loads(nd), dicts)
            # Prefer the object for this ad; fall back to the one with the most details.
            cands = [x for x in (_from_dict(d, "next_data") for d in dicts) if x]
            same = [c for c in cands if c.url == want]
            pool = same or cands
            if pool:
                best = max(pool, key=lambda c: len(c.description) + (50 if c.advertiser_type else 0))
            notes.append(f"ad page __NEXT_DATA__: {len(dicts)} ad-like objects, {'matched this ad' if same else 'no exact match'}")
        except json.JSONDecodeError:
            notes.append("ad page __NEXT_DATA__: not valid JSON")
    else:
        notes.append("ad page __NEXT_DATA__: not present")
    if best is None:
        for body in (b for a, b in p.scripts if (a.get("type") or "").lower() == "application/ld+json"):
            try:
                items = _ld_items(json.loads(body))
            except json.JSONDecodeError:
                continue
            if items:
                best = next((i for i in items if i.url == want), items[0])
                notes.append("ad page: details from JSON-LD")
                break
    meta = dict(re.findall(r'<meta[^>]+(?:property|name)=["\'](og:title|og:description|description)["\'][^>]+content=["\']([^"\']*)', html, re.I))
    if best is None and (meta.get("og:title")):
        best = RawListing(url=want, title=_text(meta["og:title"]), via="meta")
        notes.append("ad page: details from meta tags only")
    if best is None:
        return None, notes + ["ad page: no listing details found"]
    if not best.description:
        best.description = _text(meta.get("og:description") or meta.get("description") or "")[:4000]
    if not best.advertiser_type:
        page_text = " ".join(p.tokens)
        if _LABEL_PRIVATE.search(page_text):
            best.advertiser_type = "Private"
        elif _LABEL_COMPANY.search(page_text):
            best.advertiser_type = "Company"
    return best, notes
