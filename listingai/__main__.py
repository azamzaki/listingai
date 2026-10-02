"""Command line entry point.

    python -m listingai serve                 # the full app: dashboard, campaigns, OpenAI settings
    python -m listingai serve --port 8080

Or build a one-off static dashboard from a CSV:

    python -m listingai                       # uses examples/sample_listings.csv
    python -m listingai my_listings.csv       # your own data
    python -m listingai my.csv -o out.html --no-open

CSV columns (only id and caption are required):
id, post_url, location, price, caption, public_phone, public_email,
is_direct_owner, in_target_location, posted_days_ago, scam_risk_score, base_score
"""

from __future__ import annotations

import argparse
import sys
import csv
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .alerts import build_subject, exclusive_opportunity_decision
from .config import DEFAULT_CONFIG
from .dashboard import render_dashboard_html
from .exclusive_agent import apply_exclusive_agent_detection
from .models import Listing


def _yes(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "y", "yes", "true", "ya"}


def _int(value: str | None, default: int | None = None) -> int | None:
    value = (value or "").replace(",", "").replace("RM", "").strip()
    return int(float(value)) if value else default


def load_listings(path: Path) -> list[Listing]:
    now = datetime.now(timezone.utc)
    listings = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            phone = (row.get("public_phone") or "").strip() or None
            email = (row.get("public_email") or "").strip() or None
            days = _int(row.get("posted_days_ago"))
            listing = Listing(
                id=row["id"].strip(),
                post_url=(row.get("post_url") or "").strip(),
                location=(row.get("location") or "").strip(),
                price=_int(row.get("price")),
                caption=row.get("caption") or "",
                is_direct_owner=_yes(row.get("is_direct_owner")),
                public_phone=phone,
                public_email=email,
                has_eligible_contact=bool(phone or email),
                matches_target_location=_yes(row.get("in_target_location") or "yes"),
                posted_at=now - timedelta(days=days) if days is not None else None,
                scam_risk_score=_int(row.get("scam_risk_score"), 0),
                base_opportunity_score=_int(row.get("base_score"), 0),
            )
            apply_exclusive_agent_detection(listing, DEFAULT_CONFIG, now)
            listings.append(listing)
    return listings


def serve_main(argv: list[str]) -> None:
    from .server import serve

    parser = argparse.ArgumentParser(prog="python -m listingai serve", description="Run the ListingAI app on this computer.")
    parser.add_argument("--port", type=int, default=8321)
    parser.add_argument("--csv", help="also import the listings in this CSV file (duplicates are skipped)")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)
    serve(args.port, Path(args.csv) if args.csv else None, not args.no_open)


def fetch_main(argv: list[str]) -> None:
    from .leads.pipeline import RunOptions, run_pipeline

    parser = argparse.ArgumentParser(
        prog="python -m listingai fetch",
        description="Find owner listings in Penang and Kedah on Mudah and store them, logging every stage.")
    parser.add_argument("--region", action="append", choices=["Penang", "Kedah"],
                        help="region to search (repeat for both; default: both)")
    parser.add_argument("--pages", type=int, default=2, help="search-result pages per region (default 2)")
    parser.add_argument("--details", type=int, default=30, help="ad pages to open per run, new listings only (default 30)")
    parser.add_argument("--delay", type=float, default=4.0, help="seconds between requests (minimum 2, default 4)")
    parser.add_argument("--save-html", action="store_true", help="keep every downloaded page in the debug folder")
    parser.add_argument("--from-file", action="append", default=[], metavar="PAGE.html",
                        help="process a Mudah search page you saved from your browser instead of downloading")
    parser.add_argument("--search", action="append", default=[], metavar="REGION=/path",
                        help="override a search address, e.g. Penang=/penang/houses-for-sale")
    parser.add_argument("-v", "--verbose", action="store_true", help="also print per-listing details")
    args = parser.parse_args(argv)
    opts = RunOptions(regions=tuple(args.region or ("Penang", "Kedah")), max_pages=args.pages,
                      max_details=args.details, delay=args.delay, save_html=args.save_html,
                      from_files=tuple(args.from_file), verbose=args.verbose)
    for item in args.search:
        region, _, path = item.partition("=")
        opts.searches[region.strip()] = path.strip()
    result = run_pipeline(opts)
    print(f"\nRESULT ({result['status']}): {result['diagnosis']}")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:
        return serve_main(argv[1:])
    if argv[:1] == ["fetch"]:
        return fetch_main(argv[1:])
    parser = argparse.ArgumentParser(prog="python -m listingai", description="Build the ListingAI dashboard.")
    parser.add_argument("csv", nargs="?", default="examples/sample_listings.csv", help="listings CSV (default: examples/sample_listings.csv)")
    parser.add_argument("-o", "--output", default="dashboard.html", help="output HTML file (default: dashboard.html)")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)

    listings = load_listings(Path(args.csv))
    out = Path(args.output).resolve()
    out.write_text(render_dashboard_html(listings, DEFAULT_CONFIG, source_name=Path(args.csv).name), encoding="utf-8")

    for l in listings:
        ea = l.exclusive_agent
        status = ea.exclusive_agent_status.value if ea else "no_public_contact"
        email = "EMAIL" if exclusive_opportunity_decision(l).send else "     "
        print(f"{l.id:6} {status:28} {email}  {build_subject(l)}")
    print(f"\nDashboard written to {out}")
    if not args.no_open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
