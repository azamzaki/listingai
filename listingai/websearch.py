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


# --- OpenAI web search (uses the OpenAI key saved for post checking) -------------

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
_OPENAI_PROMPT = """Search the web for residential properties FOR SALE in Malaysia in these places: {places}.
Prefer listings posted by the owner ("owner", "pemilik", "tuan rumah", "direct owner"), posted in the last 7 days,
on sites such as mudah.my, propertyguru.com.my, iproperty.com.my, or public Facebook posts.
Only include listings you actually found in the search results. Never invent listings, prices or links.
Reply with JSON only, no other text:
{{"listings": [{{"title": "...", "url": "<exact URL of the listing page>", "price_rm": <number or null>,
"location": "<place>", "description": "<one sentence from the listing>"}}]}}"""


def _openai_call(body: dict, api_key: str, opener: Optional[Opener]) -> dict:
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(OPENAI_RESPONSES_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with opener(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        if e.code == 401:
            raise SearchError("OpenAI rejected the API key. Check it in Settings.") from None
        if e.code == 429:
            raise SearchError("OpenAI rate limit or credit reached. Check billing on platform.openai.com.") from None
        raise SearchError(f"OpenAI web search error {e.code}: {detail or e.reason}") from None
    except urllib.error.URLError as e:
        raise SearchError(f"Could not reach OpenAI: {e.reason}") from None


def _openai_search(places: list[str], api_key: str, model: str, opener: Optional[Opener]) -> tuple[str, set[str]]:
    """Return (answer text, URLs the search actually cited)."""
    body = {"model": model, "input": _OPENAI_PROMPT.format(places=", ".join(places)), "tools": [{"type": "web_search"}]}
    try:
        data = _openai_call(body, api_key, opener)
    except SearchError as e:
        if "web_search" not in str(e) and "tool" not in str(e).lower():
            raise
        body["tools"] = [{"type": "web_search_preview"}]  # older name of the same tool
        data = _openai_call(body, api_key, opener)
    texts, cited = [], set()
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                texts.append(part.get("text") or "")
                for ann in part.get("annotations") or []:
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        cited.add(ann["url"])
    return "\n".join(texts), cited


def _norm_url(url: str) -> str:
    p = urllib.parse.urlparse(url.strip())
    query = "&".join(q for q in p.query.split("&") if q and not q.startswith("utm_"))
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}" + (f"?{query}" if query else "")


def run_openai_search(places: Iterable[str], api_key: str, model: str, cursor: int = 0, places_per_call: int = 3,
                      max_calls: int = 4, opener: Optional[Opener] = None) -> SearchRun:
    """Search with OpenAI's web search tool. Only listings whose link the search cited are kept."""
    if not api_key:
        raise SearchError("Add your OpenAI API key in Settings first.")
    unique = list(dict.fromkeys(" ".join(p.split()) for p in places if p.strip()))
    if not unique:
        raise SearchError("Create a campaign with at least one place first.")
    groups = [unique[i:i + places_per_call] for i in range(0, len(unique), places_per_call)]
    start = cursor % len(groups)
    batch = [groups[(start + i) % len(groups)] for i in range(min(max_calls, len(groups)))]
    run = SearchRun()
    seen: set[str] = set()
    for group in batch:
        text, cited = _openai_search(group, api_key, model, opener)
        cited_norm = {_norm_url(u) for u in cited}
        match = re.search(r"\{.*\}", text, re.S)
        try:
            listings = json.loads(match.group(0)).get("listings", []) if match else []
        except (json.JSONDecodeError, AttributeError):
            listings = []
        kept = 0
        for item in listings if isinstance(listings, list) else []:
            url = str(item.get("url") or "")
            if not url.startswith("http") or _norm_url(url) not in cited_norm or url in seen:
                continue  # a link the search did not return may be made up
            title, desc = _clean(str(item.get("title") or "")), _clean(str(item.get("description") or ""))
            if not looks_like_listing(title, desc, url):
                continue
            seen.add(url)
            price = item.get("price_rm")
            price = int(price) if isinstance(price, (int, float)) and 1_000 <= price <= 100_000_000 else find_price(f"{title} {desc}")
            loc = find_location(f"{item.get('location') or ''} {title} {desc}", group)
            run.results.append(WebResult(title, url, desc, price, loc, "OpenAI: " + ", ".join(group)))
            kept += 1
        run.queries.append(("OpenAI web search: " + ", ".join(group), kept))
    run.next_cursor = (start + len(batch)) % len(groups)
    return run
