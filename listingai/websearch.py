"""Find listings through the Brave Search API (an official, paid search API).

ListingAI sends search queries built from campaign places ("Sungai Petani rumah
dijual owner") and keeps results that look like property listings. It uses
only the title, description and link that the search API returns; it does not
open or crawl the listing pages.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .extract import find_location, find_price

API_URL = "https://api.search.brave.com/res/v1/web/search"

QUERY_TEMPLATES = [
    '"{place}" rumah dijual owner',
    '"{place}" house for sale owner',
]

_PROPERTY = re.compile(
    r"\b(?:rumah|house|teres|terrace|semi-?d|banglo|bungalow|condo(?:minium)?|apartment|pangsapuri|flat|townhouse|"
    r"tanah|land|lot|shop\s?lot|kedai|for\s+sale|dijual|jual|untuk\s+dijual)\b", re.I)
_RENT = re.compile(r"\b(?:for\s+rent|disewa|sewa|rental|room\s+for\s+rent|bilik)\b", re.I)
_SKIP_HOSTS = re.compile(r"(?:youtube|tiktok|wikipedia|pinterest|linkedin|jobstreet|indeed)\.", re.I)

Opener = Callable[..., object]


class SearchError(Exception):
    pass


@dataclass
class WebResult:
    title: str
    url: str
    description: str
    price: Optional[int]
    location: Optional[str]
    query: str


@dataclass
class SearchRun:
    results: list[WebResult] = field(default_factory=list)
    queries: list[tuple[str, int]] = field(default_factory=list)  # (query, results kept)
    next_cursor: int = 0


def _clean(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", text or "")).split())


def build_queries(places: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for place in places:
        place = " ".join(place.split())
        if place and place.lower() not in seen:
            seen.add(place.lower())
            out.extend(t.format(place=place) for t in QUERY_TEMPLATES)
    return out


def search(query: str, api_key: str, freshness: str = "pw", opener: Optional[Opener] = None) -> list[dict]:
    opener = opener or urllib.request.urlopen
    params = urllib.parse.urlencode({"q": query, "count": 20, "freshness": freshness, "safesearch": "moderate"})
    req = urllib.request.Request(f"{API_URL}?{params}", headers={
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    })
    try:
        with opener(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SearchError("Brave Search rejected the API key. Check it in Settings.") from None
        if e.code == 429:
            raise SearchError("Brave Search limit reached for now. Try again later or check your plan.") from None
        raise SearchError(f"Brave Search error {e.code}.") from None
    except urllib.error.URLError as e:
        raise SearchError(f"Could not reach Brave Search: {e.reason}") from None
    return (data.get("web") or {}).get("results") or []


def looks_like_listing(title: str, description: str, url: str) -> bool:
    text = f"{title} {description}"
    if _SKIP_HOSTS.search(urllib.parse.urlparse(url).netloc):
        return False
    if _RENT.search(text) and not re.search(r"\b(?:for\s+sale|dijual|jual)\b", text, re.I):
        return False
    return bool(_PROPERTY.search(text))


def run_search(places: Iterable[str], api_key: str, cursor: int = 0, max_queries: int = 15,
               opener: Optional[Opener] = None, pause: float = 1.1) -> SearchRun:
    """Run up to `max_queries` queries, continuing from `cursor` so every place is covered over several runs."""
    if not api_key:
        raise SearchError("Add a Brave Search API key in Settings first.")
    queries = build_queries(places)
    if not queries:
        raise SearchError("Create a campaign with at least one place first.")
    run = SearchRun()
    start = cursor % len(queries)
    batch = [queries[(start + i) % len(queries)] for i in range(min(max_queries, len(queries)))]
    seen_urls: set[str] = set()
    for n, q in enumerate(batch):
        if n and pause:
            time.sleep(pause)  # stay within the API's per-second limit
        kept = 0
        for r in search(q, api_key, opener=opener):
            url = r.get("url") or ""
            title, desc = _clean(r.get("title", "")), _clean(r.get("description", ""))
            if not url or url in seen_urls or not looks_like_listing(title, desc, url):
                continue
            seen_urls.add(url)
            text = f"{title}. {desc}"
            run.results.append(WebResult(title, url, desc, find_price(text), find_location(text, places), q))
            kept += 1
        run.queries.append((q, kept))
    run.next_cursor = (start + len(batch)) % len(queries)
    return run
