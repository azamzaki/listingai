"""Read saved-search alert emails from Mudah, PropertyGuru and iProperty.

ListingAI never visits these sites. It reads the alert emails the sites send
to the user's own mailbox (IMAP, read-only; messages stay unread) and turns
each listing in them into a dashboard entry with its title, price, location
and link.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from html.parser import HTMLParser
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from .extract import find_location, find_price


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    sender_domains: tuple[str, ...]
    link_domains: tuple[str, ...]


SITES: dict[str, Site] = {
    "mudah": Site("mudah", "Mudah", ("mudah.my",), ("mudah.my",)),
    "propertyguru": Site("propertyguru", "PropertyGuru", ("propertyguru.com.my", "propertyguru.com"),
                         ("propertyguru.com.my", "propertyguru.com", "pgimgs.com")),
    "iproperty": Site("iproperty", "iProperty", ("iproperty.com.my",), ("iproperty.com.my",)),
}

# Anchor texts that are buttons, not listing titles.
_GENERIC = re.compile(r"^(?:view|lihat|see|more|details?|butiran|click here|klik|open|unsubscribe|manage|"
                      r"view (?:listing|property|details|more|all)|lihat (?:iklan|lagi|semua))\b", re.I)
_SKIP_URL = re.compile(r"unsubscribe|preferences|privacy|terms|help|support|facebook\.com|instagram|twitter|youtube|"
                       r"apps?\.apple|play\.google|mailto:", re.I)


@dataclass
class AlertItem:
    site: str
    title: str
    url: str
    price: Optional[int]
    location: Optional[str]
    text: str


class _Flatten(HTMLParser):
    """HTML → sequence of ("text", str) and ("link", href, anchor_text) tokens."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple] = []
        self._href: Optional[str] = None
        self._anchor: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._anchor = []
        elif tag in ("br", "p", "div", "tr", "td", "li", "h1", "h2", "h3", "table"):
            self.tokens.append(("text", "\n"))

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href is not None:
            self.tokens.append(("link", self._href, " ".join("".join(self._anchor).split())))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._href is not None:
            self._anchor.append(data)  # kept with its link token, not as loose text
        else:
            self.tokens.append(("text", data))


def _is_site_link(href: str, site: Site) -> bool:
    if not href.startswith("http") or _SKIP_URL.search(href):
        return False
    host = urlparse(href).netloc.lower()
    # Direct links, or the site's click-tracking links (which usually carry the site name).
    return any(host == d or host.endswith("." + d) for d in site.link_domains) or site.key in href.lower()


def _parse_text(site: Site, body: str) -> list[AlertItem]:
    """Plain-text alerts: one listing per paragraph that holds a listing link and a price."""
    items = []
    for block in re.split(r"\n\s*\n", body):
        urls = [u for u in re.findall(r"https?://\S+", block) if _is_site_link(u, site)]
        text = " ".join(re.sub(r"https?://\S+", " ", block).split())
        price = find_price(text)
        lines = [l.strip() for l in block.splitlines() if l.strip() and not l.strip().startswith("http")]
        if urls and price is not None and lines:
            items.append(AlertItem(site.key, lines[0][:200], urls[0], price, find_location(text), text[:600]))
    return items


def parse_alert(site: Site, body: str, is_html: bool = True) -> list[AlertItem]:
    if not is_html:
        return _parse_text(site, body)
    parser = _Flatten()
    parser.feed(body)
    tokens = parser.tokens

    # Each listing link owns the text that follows it, up to the next listing link.
    link_idx = [i for i, t in enumerate(tokens) if t[0] == "link" and _is_site_link(t[1], site)]
    items: dict[str, AlertItem] = {}
    for n, i in enumerate(link_idx):
        end = link_idx[n + 1] if n + 1 < len(link_idx) else min(len(tokens), i + 60)
        after = "".join(t[1] for t in tokens[i + 1:end] if t[0] == "text")
        anchor = tokens[i][2]
        text = " ".join((anchor + "\n" + after).split())[:600]
        price = find_price(text)
        title = anchor if anchor and not _GENERIC.match(anchor) and len(anchor) >= 12 and not anchor.startswith("http") else ""
        if not title:
            lines = [l.strip() for l in after.split("\n") if len(l.strip()) >= 12 and not _GENERIC.match(l.strip())]
            title = lines[0] if lines else ""
        if not title or price is None:
            continue  # logos, buttons and footer links carry no title or price
        key = re.sub(r"\W+", " ", title.lower()).strip() + f"|{price}"
        if key in items:
            continue  # the same listing linked from its photo, title and button
        items[key] = AlertItem(site.key, title[:200], tokens[i][1], price, find_location(text), text)
    return list(items.values())


def _decode(value: Optional[str]) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _body(msg: email.message.Message) -> tuple[str, bool]:
    html_part = text_part = None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype == "text/html" and html_part is None:
            html_part = part
        elif ctype == "text/plain" and text_part is None:
            text_part = part
    part = html_part or text_part
    if part is None:
        return "", False
    payload = part.get_payload(decode=True) or b""
    return payload.decode(part.get_content_charset() or "utf-8", errors="replace"), part is html_part


def site_for_sender(sender: str, enabled: Iterable[str]) -> Optional[Site]:
    addr = email.utils.parseaddr(sender)[1].lower()
    domain = addr.split("@")[-1]
    for key in enabled:
        site = SITES.get(key)
        if site and any(domain == d or domain.endswith("." + d) for d in site.sender_domains):
            return site
    return None


@dataclass
class FetchResult:
    items: list[AlertItem]
    emails_read: int
    new_seen: list[str]
    # What the check saw, for the Settings page: one row per alert email.
    report: list[dict] = field(default_factory=list)
    found_per_site: dict = field(default_factory=dict)


class MailError(Exception):
    pass


ImapFactory = Callable[[str], imaplib.IMAP4]


def fetch_alert_items(address: str, app_password: str, host: str, sites: Iterable[str], seen: Iterable[str],
                      days: int = 14, imap_factory: Optional[ImapFactory] = None, reread: bool = False) -> FetchResult:
    """Read alert emails from the last `days` days that have not been imported yet."""
    if not address or not app_password:
        raise MailError("Add your email address and app password in Settings first.")
    sites = [s for s in sites if s in SITES]
    if not sites:
        raise MailError("Choose at least one site to read alerts from.")
    seen = set(seen)
    factory = imap_factory or (lambda h: imaplib.IMAP4_SSL(h, 993, timeout=30))
    try:
        imap = factory(host)
    except OSError as e:
        raise MailError(f"Could not reach {host}: {e}") from None
    try:
        try:
            imap.login(address, app_password.replace(" ", ""))
        except imaplib.IMAP4.error:
            raise MailError("The mailbox refused the login. For Gmail, use an app password, not your normal password.") from None
        # Gmail's All Mail also covers alerts filtered out of the inbox.
        status, _ = imap.select('"[Gmail]/All Mail"', readonly=True)
        if status != "OK":
            imap.select("INBOX", readonly=True)
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        ids: list[bytes] = []
        found_per_site: dict[str, int] = {}
        for key in sites:
            site_ids: list[bytes] = []
            for domain in SITES[key].sender_domains:
                status, data = imap.search(None, "SINCE", since, "FROM", f'"{domain}"')
                if status == "OK" and data and data[0]:
                    site_ids.extend(data[0].split())
            found_per_site[SITES[key].name] = len(set(site_ids))
            ids.extend(site_ids)
        items: list[AlertItem] = []
        new_seen: list[str] = []
        report: list[dict] = []
        read = 0
        for msg_id in dict.fromkeys(ids):  # unique, order kept
            status, data = imap.fetch(msg_id, "(BODY.PEEK[])")  # PEEK keeps the email unread
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email.message_from_bytes(data[0][1])
            mid = msg.get("Message-ID") or f"{msg.get('Date')}|{msg.get('Subject')}"
            row = {"from": _decode(msg.get("From")), "subject": _decode(msg.get("Subject"))[:120],
                   "date": msg.get("Date", ""), "listings": None, "note": ""}
            if mid in new_seen:
                continue
            if mid in seen and not reread:
                row["note"] = "already read"
                report.append(row)
                continue
            site = site_for_sender(row["from"], sites)
            if site is None:
                row["note"] = "not from a property site"
                report.append(row)
                continue
            body, is_html = _body(msg)
            found = parse_alert(site, body, is_html)
            items.extend(found)
            row["listings"] = len(found)
            report.append(row)
            if mid not in seen:
                new_seen.append(mid)
            read += 1
        return FetchResult(items, read, new_seen, report, found_per_site)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
