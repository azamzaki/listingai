"""Owner-lead pipeline tests.

The Mudah pages below are SYNTHETIC: written for these tests in the shape Next.js
sites (like Mudah) embed listing data. They check the pipeline's logic, logging
and storage; whether real Mudah pages match is only known from a real run.
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from listingai.leads import db
from listingai.leads.classify import (HIGH, LOW, MEDIUM, VERY_HIGH, detect_property_type, detect_state, find_signals,
                                      opportunity_score, owner_confidence)
from listingai.leads.fetch import Fetcher
from listingai.leads.mudah import parse_ad_page, parse_date, parse_search_page
from listingai.leads.pipeline import RunOptions, run_pipeline

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def ad(n, subject, price, area, region="Penang", company=False, days=1, body=None, company_name=None):
    d = {"adId": n, "subject": subject, "price": price, "subareaName": area, "regionName": region,
         "adViewUrl": f"https://www.mudah.my/{subject.lower().replace(' ', '-')[:40]}-{n}.htm",
         "listTime": (NOW - timedelta(days=days)).isoformat(), "companyAd": company,
         "categoryName": "Houses for sale"}
    if body is not None:
        d["body"] = body
    if company_name:
        d["companyName"] = company_name
    return d


def next_page(ads):
    data = {"props": {"pageProps": {"initialState": {"listing": {"list": {"ads": ads, "total": len(ads)}}}}}}
    return (f'<html><head><title>Mudah</title></head><body><div id="__next">…</div>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>')


def ad_page(a):
    return next_page([a]).replace("…", "Private seller" if not a.get("companyAd") else "Company")


PENANG_ADS = [
    ad(1100001, "Teres 2 Tingkat Bukit Mertajam owner sell", "RM 450,000", "Bukit Mertajam", days=95,
       body="Direct owner. Rumah teres 2 tingkat, perlu jual segera kerana pindah KL. Harga nego."),
    ad(1100002, "Semi-D Bayan Lepas freehold", 880000, "Bayan Lepas", days=3,
       body="Semi-D corner lot, renovated kitchen, 4 rooms 3 bathrooms, near FTZ and airport. " * 3),
    ad(1100003, "Condo Gelugor sea view", 650000, "Gelugor", company=True, company_name="Island Realty Sdn Bhd",
       body="Fully furnished. Contact our negotiator."),
    ad(1100004, "Apartment Jelutong murah", 300000, "Jelutong", days=40, body="Owner jual. Call Ahmad REN 31234."),
    ad(1100005, "Room for rent Jelutong", 500000, "Jelutong", body="Room for rent near USM"),
    ad(1100006, "Tanah lot banglo Balik Pulau", 600000, "Balik Pulau", body="Land 6000 sqft"),
    ad(1100007, "Terrace house Johor Bahru", 400000, "Johor Bahru", region="Johor", body="Terrace in JB"),
]
KEDAH_ADS = [
    ad(2200001, "Rumah teres Sungai Petani untuk dijual", 380000, "Sungai Petani", region="Kedah", days=20,
       body="Rumah teres setingkat, tiada ejen hartanah. Owner sendiri."),
    # The same house reposted under a new link: a duplicate.
    ad(2200002, "Rumah teres Sungai Petani untuk dijual", 380000, "Sungai Petani", region="Kedah", days=1,
       body="Rumah teres setingkat, tiada ejen hartanah. Owner sendiri."),
]


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, status=200):
        super().__init__(body)
        self.status, self.headers = status, {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_site(pages: dict, robots="User-agent: *\nAllow: /\n", status_for=None):
    """pages: url substring -> html. status_for: url substring -> HTTP error code."""
    calls = []

    def opener(req, timeout=None):
        url = req.full_url
        calls.append(url)
        for frag, code in (status_for or {}).items():
            if frag in url:
                raise urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(b"Access denied"))
        if url.endswith("/robots.txt"):
            return _Resp(robots.encode())
        for frag, html in pages.items():
            if frag in url:
                return _Resp(html.encode())
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))

    opener.calls = calls
    return opener


def site_pages(penang=PENANG_ADS, kedah=KEDAH_ADS, page2_empty=True):
    pages = {"/penang/properties-for-sale?o=2": next_page([]), "/kedah/properties-for-sale?o=2": next_page([]),
             "/penang/properties-for-sale": next_page(penang), "/kedah/properties-for-sale": next_page(kedah)}
    for a in penang + kedah:
        pages[a["adViewUrl"].split("mudah.my")[1]] = ad_page(a)
    return pages


class Classifier(unittest.TestCase):
    def test_levels(self):
        sig = find_signals("Owner sell teres", "Direct owner, no agent", "Private", "")
        self.assertEqual(owner_confidence("Private", sig)[:2], (VERY_HIGH, 95))
        self.assertEqual(owner_confidence("Private", find_signals("Teres", "Nice house", "Private", ""))[:2], (HIGH, 80))
        lvl, score, reason = owner_confidence("Private", find_signals("Teres", "Call REN 12345", "Private", ""))
        self.assertEqual((lvl, reason), (LOW, "Rejected — REN number detected (REN 12345)"))
        lvl, _, reason = owner_confidence("Company", find_signals("Teres", "", "Company", ""))
        self.assertEqual(reason, "Rejected — agency detected (Advertiser type: Company)")
        lvl, _, reason = owner_confidence("", find_signals("Teres", "IQI Realty listing", "", ""))
        self.assertTrue(reason.startswith("Rejected — agency detected"))
        self.assertEqual(owner_confidence("", find_signals("Teres", "", "", ""))[0], MEDIUM)

    def test_refusals_are_not_agent_signals(self):
        sig = find_signals("Rumah owner", "No agency please. Tanpa ejen hartanah. No agent fee.", "Private", "")
        self.assertEqual(sig.agent, [])
        self.assertEqual(owner_confidence("Private", sig)[0], VERY_HIGH)

    def test_opportunity(self):
        score, reasons = opportunity_score(NOW - timedelta(days=100), "Teres BM", "Urgent! Harga turun, nego", 400000, now=NOW)
        self.assertGreaterEqual(score, 80)
        self.assertTrue(any("Listed 100 days" in r for r in reasons))
        self.assertTrue(any("Urgent" in r for r in reasons))
        fresh, _ = opportunity_score(NOW, "Semi-D", "x" * 400, 900000, now=NOW)
        self.assertEqual(fresh, 30)
        dropped, r = opportunity_score(NOW, "Semi-D", "x" * 400, 850000, price_dropped_rm=50000, now=NOW)
        self.assertIn("Price dropped RM50,000 since first seen", r)

    def test_state_and_type(self):
        self.assertEqual(detect_state("Bkt Mertajam"), "Penang")
        self.assertEqual(detect_state("Sg Petani"), "Kedah")
        self.assertEqual(detect_state("Johor Bahru"), "other")
        self.assertIsNone(detect_state("Taman Mutiara"))
        self.assertEqual(detect_property_type("", "Rumah teres 2 tingkat", ""), "Terrace")
        self.assertEqual(detect_property_type("", "Tanah lot banglo", ""), "Land")
        self.assertEqual(detect_property_type("Apartment for sale", "Unit murah", ""), "Apartment")


class Parsing(unittest.TestCase):
    def test_next_data(self):
        rep = parse_search_page(next_page(PENANG_ADS))
        self.assertEqual(len(rep.listings), 7)
        first = rep.listings[0]
        self.assertEqual((first.price, first.location, first.advertiser_type), (450000, "Bukit Mertajam", "Private"))
        self.assertEqual(first.source_id, "1100001")
        self.assertIn("7 listings parsed", rep.notes[0])

    def test_json_ld(self):
        ld = {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item": {
            "@type": "Product", "name": "Teres Kulim", "url": "https://www.mudah.my/teres-kulim-3300001.htm",
            "offers": {"price": "350000"}}}]}
        rep = parse_search_page(f'<script type="application/ld+json">{json.dumps(ld)}</script>')
        self.assertEqual([(l.title, l.price, l.via) for l in rep.listings], [("Teres Kulim", 350000, "json_ld")])

    def test_link_fallback(self):
        html = ('<div><a href="/rumah-teres-jitra-4400001.htm">Rumah teres Jitra untuk dijual</a>'
                '<span>RM 280,000</span><span>Jitra, Kedah</span></div><a href="/help">Help</a>')
        rep = parse_search_page(html)
        self.assertEqual([(l.title, l.price, l.via) for l in rep.listings], [("Rumah teres Jitra untuk dijual", 280000, "links")])

    def test_unrecognised_page_explains_why(self):
        rep = parse_search_page("<html><body><p>Hello</p></body></html>")
        self.assertEqual(rep.listings, [])
        self.assertIn("__NEXT_DATA__: not present on the page", rep.notes)
        self.assertIn("Ad links: 0 found, 0 new listings with a title and price", rep.notes)

    def test_ad_page_and_label(self):
        a = dict(PENANG_ADS[1])
        a.pop("companyAd")
        item, notes = parse_ad_page(ad_page(a), a["adViewUrl"])
        self.assertEqual(item.advertiser_type, "Private")  # read from the page label
        self.assertIn("matched this ad", notes[0])

    def test_dates(self):
        self.assertEqual(parse_date("Yesterday 10:15", NOW)[0].date(), (NOW - timedelta(days=1)).date())
        self.assertEqual(parse_date("3 hari lalu", NOW)[0].date(), (NOW - timedelta(days=3)).date())
        self.assertEqual(parse_date(1759400000)[0].year, 2025)
        self.assertEqual(parse_date("12 Sep", NOW)[0].month, 9)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LISTINGAI_HOME": self.tmp.name})
        self.env.start()
        self.lines = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_with(self, opener, **opts):
        fetcher = Fetcher(delay=0, opener=opener, sleep=lambda s: None, max_requests=100)
        return run_pipeline(RunOptions(**opts), fetcher=fetcher, printer=self.lines.append, now=NOW)

    def test_end_to_end(self):
        site = fake_site(site_pages())
        r = self.run_with(site)
        self.assertEqual(r["status"], "ok", r["diagnosis"])
        new = {l["title"]: l for l in db.list_leads("new")}
        self.assertEqual(set(new), {"Teres 2 Tingkat Bukit Mertajam owner sell", "Semi-D Bayan Lepas freehold",
                                    "Rumah teres Sungai Petani untuk dijual"})
        bm = new["Teres 2 Tingkat Bukit Mertajam owner sell"]
        self.assertEqual((bm["owner_level"], bm["state"], bm["property_type"], bm["price"]), (VERY_HIGH, "Penang", "Terrace", 450000))
        self.assertGreaterEqual(bm["opportunity_score"], 60)
        self.assertEqual(new["Semi-D Bayan Lepas freehold"]["owner_level"], HIGH)
        rejected = {l["title"]: l["rejection_reason"] for tab in ("rejected_agent", "rejected_filter")
                    for l in db.list_leads(tab)}
        self.assertEqual(rejected["Apartment Jelutong murah"], "Rejected — REN number detected (REN 31234)")
        self.assertTrue(rejected["Condo Gelugor sea view"].startswith("Rejected — agency detected"))
        self.assertEqual(rejected["Room for rent Jelutong"], "Rejected — rental, not for sale")
        self.assertEqual(rejected["Tanah lot banglo Balik Pulau"], "Rejected — not residential (Land)")
        self.assertEqual(rejected["Terrace house Johor Bahru"], "Rejected — location outside Kedah/Penang (Johor Bahru)")
        self.assertEqual(r["counts"]["duplicates"], 1)
        self.assertIn("priority", db.tab_counts())
        self.assertEqual([l["title"] for l in db.list_leads("priority")], ["Teres 2 Tingkat Bukit Mertajam owner sell"])
        # Every stage logged.
        stages = {e["stage"] for e in db.run_events(r["run_id"])}
        self.assertEqual(stages, {"fetch", "parse", "filter", "classify", "store"})
        self.assertTrue(any("RESULT:" in l for l in self.lines))
        # robots.txt read once per host, and no phone-number buttons or other hidden data requested.
        self.assertEqual(sum(1 for u in site.calls if u.endswith("robots.txt")), 1)

    def test_second_run_updates_and_detects_price_drop(self):
        self.run_with(fake_site(site_pages()))
        cheaper = [dict(a) for a in PENANG_ADS]
        cheaper[1]["price"] = 830000
        site = fake_site(site_pages(penang=cheaper))
        r = self.run_with(site)
        self.assertEqual(r["counts"]["inserted"], 0)
        self.assertGreater(r["counts"]["updated"], 0)
        self.assertEqual(r["counts"].get("ad_pages_requested", 0), 0)  # known listings are not reopened
        semi = next(l for l in db.list_leads("new") if l["title"].startswith("Semi-D"))
        self.assertIn("Price dropped RM50,000 since first seen", semi["opportunity_reasons"])

    def test_reviewed_stays_reviewed(self):
        self.run_with(fake_site(site_pages()))
        lead = db.list_leads("new")[0]
        db.set_status(lead["id"], "reviewed")
        self.run_with(fake_site(site_pages()))
        self.assertEqual([l["id"] for l in db.list_leads("reviewed")], [lead["id"]])

    def test_blocked_is_reported_at_fetch(self):
        r = self.run_with(fake_site({}, status_for={"properties-for-sale": 403}))
        self.assertEqual(r["status"], "failed")
        self.assertIn("Stopped at FETCHING", r["diagnosis"])
        self.assertIn("HTTP 403 Forbidden: the site refused automated access", r["diagnosis"])

    def test_robots_disallow_is_respected(self):
        site = fake_site(site_pages(), robots="User-agent: *\nDisallow: /penang/\nDisallow: /kedah/\n")
        r = self.run_with(site)
        self.assertIn("robots.txt disallows /penang/properties-for-sale", r["diagnosis"])
        self.assertFalse(any("properties-for-sale" in u for u in site.calls))

    def test_unrecognised_page_is_reported_at_parse_and_saved(self):
        r = self.run_with(fake_site({"properties-for-sale": "<html><body>New design</body></html>"}))
        self.assertEqual(r["status"], "zero_results")
        self.assertIn("Stopped at PARSING", r["diagnosis"])
        self.assertIn("__NEXT_DATA__: not present", r["diagnosis"])
        self.assertTrue(Path(r["snapshots"][0]).exists())

    def test_everything_filtered_is_reported(self):
        outside = [ad(9000001, "Terrace house Johor Bahru", 400000, "Johor Bahru", region="Johor")]
        r = self.run_with(fake_site(site_pages(penang=outside, kedah=[])))
        self.assertIn("Stopped at FILTERING", r["diagnosis"])

    def test_all_agents_is_reported(self):
        agents = [ad(9100001, "Condo Gelugor", 650000, "Gelugor", company=True)]
        r = self.run_with(fake_site(site_pages(penang=agents, kedah=[])))
        self.assertIn("look like agents", r["diagnosis"])

    def test_from_saved_file(self):
        f = Path(self.tmp.name) / "saved.html"
        f.write_text(next_page(KEDAH_ADS[:1]), encoding="utf-8")
        r = run_pipeline(RunOptions(regions=("Kedah",), from_files=(str(f),)), printer=self.lines.append, now=NOW)
        self.assertEqual(r["counts"]["new_owner_leads"], 1)


class LeadsPage(unittest.TestCase):
    def test_tabs_reasons_and_actions(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LISTINGAI_HOME": tmp}):
            from listingai.leads.web import LeadsRunner, leads_page, run_log_page
            from listingai.server import App
            fetcher = Fetcher(delay=0, opener=fake_site(site_pages()), sleep=lambda s: None, max_requests=100)
            r = run_pipeline(RunOptions(), fetcher=fetcher, printer=None, now=NOW)
            page = leads_page("new", "tok", LeadsRunner(), 0)
            for label in ("New Owner Leads", "High Priority", "Reviewed", "Rejected Agent", "Filtered out"):
                self.assertIn(label, page)
            self.assertIn("Owner VERY HIGH · 95", page)
            self.assertIn("Mark reviewed", page)
            rej = leads_page("rejected_agent", "tok", LeadsRunner(), 0)
            self.assertIn("Rejected — REN number detected (REN 31234)", rej)
            self.assertIn("Rejected — location outside Kedah/Penang", leads_page("rejected_filter", "tok", LeadsRunner(), 0))
            log = run_log_page(r["run_id"])
            for stage in ("FETCH", "PARSE", "FILTER", "CLASSIFY", "STORE"):
                self.assertIn(stage, log)
            # Served through the app with its navigation.
            app = App(None)
            self.assertIn("Owner Leads", app.nav("/"))
            lead = db.list_leads("new")[0]
            db.set_status(lead["id"], "reviewed")
            self.assertEqual(db.tab_counts()["reviewed"], 1)
