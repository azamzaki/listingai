"""Owner-lead discovery pipeline: FETCH → PARSE → FILTER → CLASSIFY → STORE.

Every stage writes events to the database, to logs/pipeline-YYYYMMDD.log and
(when verbose) to the console, so a run that ends with zero leads says where
it stopped: the site blocked the request, robots.txt disallowed it, the page
had no recognisable listings, every listing was filtered out, every listing
was an agent, or the database write failed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..settings import data_dir
from . import db
from .classify import (LOW, RESIDENTIAL, _RENT, _SALE, detect_property_type, detect_state, find_signals,
                       opportunity_score, owner_confidence)
from .fetch import Fetcher, save_snapshot
from .mudah import DEFAULT_SEARCHES, RawListing, parse_ad_page, parse_search_page, search_url

SOURCE = "mudah"
STAGES = ("fetch", "parse", "filter", "classify", "store")


def _logger() -> logging.Logger:
    log = logging.getLogger("listingai.pipeline")
    if not log.handlers:
        folder = data_dir() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(folder / f"pipeline-{datetime.now():%Y%m%d}.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


@dataclass
class RunOptions:
    regions: tuple[str, ...] = ("Penang", "Kedah")
    max_pages: int = 2                # search-result pages per region
    max_details: int = 30             # ad pages opened per run (only for listings not seen before)
    delay: float = 4.0                # seconds between requests
    save_html: bool = False           # keep every downloaded page in debug/ (pages with 0 listings are always kept)
    searches: dict = field(default_factory=lambda: dict(DEFAULT_SEARCHES))
    extra_query: str = ""             # appended to search URLs, e.g. a site filter
    from_files: tuple[str, ...] = ()  # parse saved HTML files instead of fetching (offline debugging)
    verbose: bool = False


class Run:
    def __init__(self, con: sqlite3.Connection, opts: RunOptions, printer: Optional[Callable[[str], None]]):
        self.con, self.opts, self.printer = con, opts, printer
        self.log = _logger()
        self.counts: Counter = Counter({k: 0 for k in (
            "requests", "pages_fetched", "listings_parsed", "ad_pages_requested", "ad_pages_parsed",
            "passed_filter", "filtered_out", "inserted", "updated", "duplicates", "new_owner_leads",
            "rejected_agent", "store_errors")})
        self.first_error: dict[str, str] = {}
        self.parse_notes: list[str] = []
        self.snapshots: list[str] = []
        params = {k: v for k, v in opts.__dict__.items() if k != "searches"} | {"searches": opts.searches}
        cur = con.execute("INSERT INTO runs (source, started_at, params, status) VALUES (?, ?, ?, 'running')",
                          (SOURCE, db.now_iso(), json.dumps(params, default=str)))
        self.id = int(cur.lastrowid)
        con.commit()

    def event(self, stage: str, level: str, message: str, **data) -> None:
        self.con.execute("INSERT INTO events (run_id, ts, stage, level, message, data) VALUES (?, ?, ?, ?, ?, ?)",
                         (self.id, db.now_iso(), stage, level, message, json.dumps(data, default=str) if data else None))
        line = f"[run {self.id}] {stage.upper():8} {level.upper():5} {message}"
        getattr(self.log, "error" if level == "error" else "warning" if level == "warn" else "info")(line)
        if self.printer and (self.opts.verbose or level != "debug"):
            self.printer(f"{stage.upper():8} {'!' if level == 'error' else '-'} {message}")
        if level == "error" and stage not in self.first_error:
            self.first_error[stage] = message


def _merge(base: RawListing, extra: RawListing) -> RawListing:
    for f in ("title", "price", "location", "posted_at", "posted_text", "advertiser_type", "advertiser_name",
              "description", "category", "photos"):
        if getattr(extra, f) not in (None, "") and getattr(base, f) in (None, ""):
            setattr(base, f, getattr(extra, f))
    if len(extra.description or "") > len(base.description or ""):
        base.description = extra.description
    return base


def run_pipeline(opts: Optional[RunOptions] = None, fetcher: Optional[Fetcher] = None,
                 printer: Optional[Callable[[str], None]] = print, db_file: Optional[Path] = None,
                 now: Optional[datetime] = None) -> dict:
    opts = opts or RunOptions()
    now = now or datetime.now(timezone.utc)
    fetcher = fetcher or Fetcher(delay=opts.delay, max_requests=opts.max_pages * len(opts.regions) + opts.max_details + 5)
    debug_dir = data_dir() / "debug"
    with db.connect(db_file) as con:
        run = Run(con, opts, printer)
        run.event("fetch", "info", f"Run {run.id} started: source=Mudah, regions={', '.join(opts.regions)}, "
                                   f"pages/region={opts.max_pages}, ad pages≤{opts.max_details}, delay={opts.delay}s")
        found: list[tuple[str, RawListing]] = []   # (search region, listing)

        # ---- FETCH + PARSE search pages -------------------------------------------
        pages: list[tuple[str, str, str, object]] = []   # (region, url or file, html, parse report)
        if opts.from_files:
            for f in opts.from_files:
                try:
                    html = Path(f).read_text(encoding="utf-8", errors="replace")
                    pages.append((opts.regions[0] if opts.regions else "", f, html, parse_search_page(html)))
                    run.counts["pages_fetched"] += 1
                    run.event("fetch", "info", f"Read saved page {f}")
                except OSError as e:
                    run.event("fetch", "error", f"Could not read {f}: {e}")
        for region in ([] if opts.from_files else opts.regions):
            path = opts.searches.get(region)
            if not path:
                run.event("fetch", "error", f"No search address configured for region {region}")
                continue
            for page in range(1, opts.max_pages + 1):
                url = search_url(path, page, opts.extra_query)
                res = fetcher.get(url)
                run.counts["requests"] += 1
                if not res.ok:
                    run.event("fetch", "error", f"{region} page {page}: {res.error}", url=url, status=res.status, kind=res.kind)
                    if res.html:
                        run.snapshots.append(str(save_snapshot(debug_dir, f"run{run.id}-{region}-p{page}-blocked.html", res.html)))
                    break  # blocked / robots / 404: the next pages will fail the same way
                run.counts["pages_fetched"] += 1
                run.event("fetch", "info", f"{region} page {page}: HTTP {res.status}, {len(res.html):,} bytes in {res.elapsed:.1f}s", url=url)
                rep = parse_search_page(res.html)
                pages.append((region, url, res.html, rep))
                if not rep.listings:
                    break  # parsed below; an empty page means no point fetching the next one
        for region, where, html, rep in pages:
            for note in rep.notes:
                run.event("parse", "debug", f"{where}: {note}")
            run.parse_notes.extend(rep.notes)
            run.counts["listings_parsed"] += len(rep.listings)
            level = "info" if rep.listings else "error"
            run.event("parse", level, f"{region or 'file'}: {len(rep.listings)} listings recognised on {where}"
                      + ("" if rep.listings else " — " + "; ".join(rep.notes)))
            if not rep.listings or opts.save_html:
                snap = save_snapshot(debug_dir, f"run{run.id}-{region or 'file'}-{len(run.snapshots) + 1}.html", html)
                run.snapshots.append(str(snap))
                run.event("parse", "info", f"Saved the page for inspection: {snap}")
            found.extend((region, l) for l in rep.listings)

        # ---- FETCH + PARSE ad pages for new listings ------------------------------
        opened = 0
        for region, item in found:
            if opts.from_files or opened >= opts.max_details:
                break
            if db.get_by_url(con, item.url) is not None:
                continue  # known listing: no need to open it again
            if item.description and item.advertiser_type and item.posted_at:
                continue
            if opened == 0:
                todo = min(opts.max_details, sum(1 for _, x in found if db.get_by_url(con, x.url) is None))
                run.event("fetch", "info", f"Opening up to {todo} new ad pages for details "
                                           f"(about {int(todo * opts.delay)}s with the {opts.delay}s wait between requests)")
            res = fetcher.get(item.url)
            opened += 1
            run.counts["ad_pages_requested"] += 1
            if run.printer:
                run.printer(f"FETCH    - ad page {opened}: {'ok' if res.ok else res.error} — {item.title[:60]}")
            if not res.ok:
                run.event("fetch", "warn", f"Ad page not opened ({res.error}): {item.url}")
                if res.kind in ("blocked", "robots"):
                    run.event("fetch", "error", f"Stopped opening ad pages: {res.error}")
                    break
                continue
            detail, notes = parse_ad_page(res.html, item.url)
            for n in notes:
                run.event("parse", "debug", f"{item.url}: {n}")
            if detail:
                _merge(item, detail)
                run.counts["ad_pages_parsed"] += 1
            else:
                run.event("parse", "warn", f"Ad page had no recognisable details: {item.url}")
                if run.counts["ad_pages_parse_failed"] == 0 or opts.save_html:
                    run.snapshots.append(str(save_snapshot(debug_dir, f"run{run.id}-ad-{item.source_id}.html", res.html)))
                run.counts["ad_pages_parse_failed"] += 1

        # ---- FILTER ---------------------------------------------------------------
        candidates: list[tuple[RawListing, dict]] = []
        reasons = Counter()
        for region, item in found:
            ptype = detect_property_type(item.category, item.title, item.description)
            state = detect_state(item.location, item.title)
            state_note = ""
            if state is None:
                state = detect_state(item.description) or region or None
                state_note = "inferred from search region" if state == region else ""
            text = f"{item.title} {item.category} {item.description[:300]}"
            reason = None
            if _RENT.search(text) and not _SALE.search(item.title):
                reason = "Rejected — rental, not for sale"
            elif ptype in ("Land", "Commercial"):
                reason = f"Rejected — not residential ({ptype})"
            elif state == "other":
                reason = f"Rejected — location outside Kedah/Penang ({item.location or 'from text'})"
            info = {"property_type": ptype or "", "state": state if state in ("Penang", "Kedah") else "",
                    "state_note": state_note, "reason": reason}
            if reason:
                reasons[reason.split(" (")[0]] += 1
                run.event("filter", "debug", f"{reason}: {item.title}", url=item.url)
            candidates.append((item, info))
        kept = sum(1 for _, i in candidates if not i["reason"])
        run.counts["filtered_out"] = len(candidates) - kept
        run.counts["passed_filter"] = kept
        run.event("filter", "info" if kept or not candidates else "error",
                  f"{kept} of {len(candidates)} listings passed the filters"
                  + (f"; rejected: {dict(reasons)}" if reasons else ""))

        # ---- CLASSIFY + STORE ---------------------------------------------------------
        levels = Counter()
        for item, info in candidates:
            sig = find_signals(item.title, item.description, item.advertiser_type, item.advertiser_name)
            level, oscore, agent_reason = owner_confidence(item.advertiser_type, sig)
            existing = db.get_by_url(con, item.url)
            drop = 0
            if existing and existing["first_price"] and item.price and item.price < existing["first_price"]:
                drop = existing["first_price"] - item.price
            if info["reason"]:
                status, reason = "rejected_filter", info["reason"]
            elif agent_reason:
                status, reason = "rejected_agent", agent_reason
            else:
                status, reason = "new", None
            opp, opp_reasons = (opportunity_score(item.posted_at, item.title, item.description, item.price, drop, now)
                                if status == "new" else (0, []))
            if not info["reason"]:
                levels[level] += 1
                run.event("classify", "debug", f"{level} owner ({oscore}), opportunity {opp}: {item.title}"
                          + (f" — {agent_reason}" if agent_reason else ""), signals=sig.agent + sig.owner)
            record = {
                "source": SOURCE, "source_id": item.source_id, "url": item.url,
                "fingerprint": db.fingerprint(item.title, item.price, item.location),
                "title": item.title, "property_type": info["property_type"], "price": item.price,
                "location": item.location, "state": info["state"],
                "posted_at": item.posted_at.isoformat() if item.posted_at else None, "posted_text": item.posted_text,
                "advertiser_type": item.advertiser_type, "advertiser_name": item.advertiser_name,
                "description": item.description, "agent_indicators": sig.agent, "owner_wording": sig.owner,
                "owner_level": level, "owner_score": oscore, "opportunity_score": opp, "opportunity_reasons": opp_reasons,
                "rejection_reason": reason,
            }
            try:
                if existing:
                    changes = {k: v for k, v in record.items() if k not in ("source", "url")}
                    changes["last_seen"], changes["last_run"] = db.now_iso(), run.id
                    if existing["status"] == "reviewed":
                        changes.pop("rejection_reason", None)  # keep the user's decision
                    else:
                        changes["status"] = status
                    db.update_lead(con, existing["id"], changes)
                    run.counts["updated"] += 1
                    continue
                twin = db.get_by_fingerprint(con, record["fingerprint"])
                record.update({"status": status, "first_seen": db.now_iso(), "last_seen": db.now_iso(),
                               "last_run": run.id, "first_price": item.price})
                if twin is not None:
                    record["duplicate_of"] = twin["id"]
                    run.counts["duplicates"] += 1
                    run.event("store", "debug", f"Duplicate of lead #{twin['id']} (same title, price and area): {item.url}")
                db.insert_lead(con, record)
                run.counts["inserted"] += 1
                if status == "new" and twin is None:
                    run.counts["new_owner_leads"] += 1
                elif status == "rejected_agent":
                    run.counts["rejected_agent"] += 1
            except sqlite3.Error as e:
                run.counts["store_errors"] += 1
                run.event("store", "error", f"Database write failed for {item.url}: {e}")
        con.commit()
        run.event("classify", "info", f"Owner confidence: {dict(levels) or 'nothing to classify'}")
        run.event("store", "info" if not run.counts["store_errors"] else "error",
                  f"Database: {run.counts['inserted']} inserted, {run.counts['updated']} updated, "
                  f"{run.counts['duplicates']} duplicates, {run.counts['store_errors']} errors")

        status, diagnosis = diagnose(run, levels)
        run.event("store", "info" if status == "ok" else "error", f"RESULT: {diagnosis}")
        con.execute("UPDATE runs SET finished_at = ?, status = ?, counts = ?, diagnosis = ? WHERE id = ?",
                    (db.now_iso(), status, json.dumps(dict(run.counts)), diagnosis, run.id))
        return {"run_id": run.id, "status": status, "diagnosis": diagnosis, "counts": dict(run.counts),
                "snapshots": run.snapshots}


def diagnose(run: Run, levels: Counter) -> tuple[str, str]:
    c = run.counts
    snaps = f" Saved page: {run.snapshots[-1]}" if run.snapshots else ""
    if c["pages_fetched"] == 0:
        why = run.first_error.get("fetch", "no request was made")
        return "failed", f"Stopped at FETCHING — no search page was downloaded: {why}.{snaps}"
    if c["listings_parsed"] == 0:
        notes = "; ".join(dict.fromkeys(run.parse_notes)) or "no details"
        return "zero_results", (f"Stopped at PARSING — {c['pages_fetched']} page(s) downloaded but no listings were "
                                f"recognised ({notes}). The site's page format has probably changed.{snaps}")
    if c["passed_filter"] == 0:
        return "zero_results", (f"Stopped at FILTERING — all {c['listings_parsed']} listings were rejected by the "
                                f"location/type/rental filters (see Rejected tab for each reason).")
    if c["store_errors"] and not c["inserted"] and not c["updated"]:
        return "failed", f"Stopped at DATABASE — {run.first_error.get('store', 'write failed')}."
    owner = sum(n for lvl, n in levels.items() if lvl != LOW)
    if owner == 0:
        return "zero_results", (f"CLASSIFICATION — all {c['passed_filter']} listings that passed the filters look "
                                f"like agents (REN/agency detected; see Rejected Agent tab).")
    return "ok", (f"{c['new_owner_leads']} new owner leads, {c['updated']} already known and refreshed, "
                  f"{c['rejected_agent']} new agent listings rejected, {c['filtered_out']} filtered out, "
                  f"{c['duplicates']} duplicates.")
