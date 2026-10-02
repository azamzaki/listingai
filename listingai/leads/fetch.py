"""Polite HTTP fetching of public pages.

* Obeys the site's robots.txt; a disallowed page is never requested.
* Waits between requests (default 4 s) and caps requests per run.
* Identifies itself honestly in the User-Agent. It does not log in, solve
  CAPTCHAs or try to get around blocking: a blocked request is reported and
  the run stops for that site.
"""

from __future__ import annotations

import gzip
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

USER_AGENT = "ListingAI/0.2 (personal property lead research; low volume; respects robots.txt)"

_BLOCK_MARKERS = re.compile(
    r"cf-chl|challenge-platform|captcha|attention required|access denied|just a moment\.\.\.|"
    r"request blocked|are you a robot|unusual traffic|perimeterx|px-captcha|datadome", re.I)


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int
    html: str
    error: str = ""          # human-readable reason when not ok
    kind: str = ""           # "", "blocked", "robots", "http", "network", "limit"
    elapsed: float = 0.0


Opener = Callable[..., object]


class Fetcher:
    def __init__(self, delay: float = 4.0, max_requests: int = 40, timeout: float = 25.0,
                 opener: Optional[Opener] = None, sleep: Callable[[float], None] = time.sleep,
                 respect_robots: bool = True):
        self.delay = max(2.0, delay) if sleep is time.sleep else delay
        self.max_requests = max_requests
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.respect_robots = respect_robots
        self.requests = 0
        self._last = 0.0
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self.blocked_hosts: set[str] = set()

    # robots.txt ---------------------------------------------------------------
    def robots_allows(self, url: str) -> tuple[bool, str]:
        if not self.respect_robots:
            return True, "robots.txt check turned off"
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            res = self._raw_get(f"{base}/robots.txt", count=False)
            if res.ok:
                rp.parse(res.html.splitlines())
                self._robots[base] = rp
            elif res.status in (401, 403):
                # robots.txt itself refused: treat as "do not crawl" (the convention).
                self._robots[base] = None
            else:
                # 404 or unreachable robots.txt: no rules published.
                empty = urllib.robotparser.RobotFileParser()
                empty.parse([])
                self._robots[base] = empty
        rp = self._robots[base]
        if rp is None:
            return False, f"{base}/robots.txt could not be read (HTTP 403), so crawling is treated as not allowed"
        if not rp.can_fetch(USER_AGENT, url):
            return False, f"{base}/robots.txt disallows {p.path or '/'}"
        return True, ""

    # fetching -------------------------------------------------------------------
    def get(self, url: str) -> FetchResult:
        host = urlparse(url).netloc
        if host in self.blocked_hosts:
            return FetchResult(url, False, 0, "", f"{host} blocked an earlier request in this run; not retrying", "blocked")
        if self.requests >= self.max_requests:
            return FetchResult(url, False, 0, "", f"request limit for this run reached ({self.max_requests})", "limit")
        allowed, why = self.robots_allows(url)
        if not allowed:
            return FetchResult(url, False, 0, "", why, "robots")
        res = self._raw_get(url)
        if res.kind == "blocked":
            self.blocked_hosts.add(host)
        return res

    def _raw_get(self, url: str, count: bool = True) -> FetchResult:
        wait = self.delay - (time.monotonic() - self._last)
        if self._last and wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()
        if count:
            self.requests += 1
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-MY,en;q=0.9,ms;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        })
        start = time.monotonic()
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower() if getattr(resp, "headers", None) else ""
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:5000]
            except Exception:
                pass
            kind = "blocked" if e.code in (403, 429) or _BLOCK_MARKERS.search(body) else "http"
            reason = {403: "HTTP 403 Forbidden: the site refused automated access",
                      429: "HTTP 429 Too Many Requests: the site is rate-limiting this computer",
                      404: "HTTP 404 Not Found: the search address may have changed"}.get(e.code, f"HTTP {e.code} {e.reason}")
            if kind == "blocked" and e.code not in (403, 429):
                reason += " (block/CAPTCHA page detected)"
            return FetchResult(url, False, e.code, body, reason, kind, time.monotonic() - start)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            return FetchResult(url, False, 0, "", f"network error: {reason}", "network", time.monotonic() - start)
        try:
            if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw)
        except Exception:
            pass
        html = raw.decode("utf-8", "replace")
        elapsed = time.monotonic() - start
        if _BLOCK_MARKERS.search(html[:20000]) and len(html) < 60000:
            return FetchResult(url, False, status, html, "the site returned a block/CAPTCHA page instead of listings",
                               "blocked", elapsed)
        return FetchResult(url, True, status, html, "", "", elapsed)


def save_snapshot(folder: Path, name: str, html: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / re.sub(r"[^\w.-]+", "_", name)[:120]
    path.write_text(html, encoding="utf-8")
    return path
