import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock
from urllib.parse import parse_qs, urlparse

from listingai.websearch import SearchError, build_queries, looks_like_listing, run_search


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


RESULTS = {
    "Sungai Petani": [
        {"title": "Rumah Teres 2 Tingkat Taman Ria Jaya <strong>Sungai Petani</strong> untuk dijual",
         "url": "https://www.mudah.my/rumah-teres-taman-ria-jaya-110022.htm",
         "description": "Owner jual. RM 385,000. Freehold, 4 bilik. Hubungi 012-3456789"},
        {"title": "Bilik untuk disewa Sungai Petani", "url": "https://www.mudah.my/bilik-1.htm", "description": "Sewa RM300 sebulan"},
        {"title": "Sungai Petani - Wikipedia", "url": "https://en.wikipedia.org/wiki/Sungai_Petani", "description": "Town in Kedah"},
    ],
    "Kulim": [
        {"title": "Single Storey Terrace House for Sale in Kulim", "url": "https://www.propertyguru.com.my/property-listing/kulim-123",
         "description": "RM 260,000 · 3 beds · Kulim, Kedah"},
    ],
}


def fake_brave(status=200):
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(b"{}"))
        q = parse_qs(urlparse(req.full_url).query)["q"][0]
        place = next((p for p in RESULTS if p in q), None)
        # The same results come back for every template of a place.
        return _Resp(json.dumps({"web": {"results": RESULTS.get(place, [])}}).encode())

    opener.calls = calls
    return opener


class Search(unittest.TestCase):
    def test_queries(self):
        self.assertEqual(build_queries(["Kulim", "kulim", " "]),
                         ['"Kulim" rumah dijual owner', '"Kulim" house for sale owner'])

    def test_filter(self):
        self.assertTrue(looks_like_listing("Rumah teres dijual", "", "https://mudah.my/x"))
        self.assertFalse(looks_like_listing("Bilik untuk disewa", "", "https://mudah.my/x"))
        self.assertFalse(looks_like_listing("Kulim house for sale", "", "https://www.youtube.com/watch?v=1"))
        self.assertFalse(looks_like_listing("Kulim weather today", "", "https://x.com"))

    def test_run_keeps_listings_and_sends_key_in_header(self):
        opener = fake_brave()
        run = run_search(["Sungai Petani", "Kulim"], "BSA-key", opener=opener, pause=0)
        self.assertEqual(sorted(r.url for r in run.results), [
            "https://www.mudah.my/rumah-teres-taman-ria-jaya-110022.htm",
            "https://www.propertyguru.com.my/property-listing/kulim-123",
        ])
        sp = next(r for r in run.results if "mudah" in r.url)
        self.assertEqual((sp.price, sp.location), (385_000, "Sungai Petani"))
        self.assertNotIn("<strong>", sp.title)
        self.assertEqual(opener.calls[0].get_header("X-subscription-token"), "BSA-key")
        self.assertNotIn("BSA-key", opener.calls[0].full_url)
        self.assertEqual(parse_qs(urlparse(opener.calls[0].full_url).query)["freshness"], ["pw"])

    def test_cursor_rotates_through_places(self):
        run = run_search(["Sungai Petani", "Kulim"], "k", cursor=0, max_queries=2, opener=fake_brave(), pause=0)
        self.assertEqual([q for q, _ in run.queries], ['"Sungai Petani" rumah dijual owner', '"Sungai Petani" house for sale owner'])
        run2 = run_search(["Sungai Petani", "Kulim"], "k", cursor=run.next_cursor, max_queries=2, opener=fake_brave(), pause=0)
        self.assertTrue(all("Kulim" in q for q, _ in run2.queries))

    def test_errors(self):
        with self.assertRaisesRegex(SearchError, "API key"):
            run_search(["Kulim"], "", opener=fake_brave())
        with self.assertRaisesRegex(SearchError, "campaign"):
            run_search([], "k", opener=fake_brave())
        with self.assertRaisesRegex(SearchError, "rejected"):
            run_search(["Kulim"], "bad", opener=fake_brave(401), pause=0)


def fake_openai_search(listings, cited, status=200, reject_tool=False):
    calls = []

    def opener(req, timeout=None):
        body = json.loads(req.data)
        calls.append(body)
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(b'{"error":{"message":"x"}}'))
        if reject_tool and body["tools"][0]["type"] == "web_search":
            raise urllib.error.HTTPError(req.full_url, 400, "err", {},
                                         io.BytesIO(b'{"error":{"message":"Unsupported tool type: web_search"}}'))
        text = "Here you go:\n```json\n" + json.dumps({"listings": listings}) + "\n```"
        return _Resp(json.dumps({"output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": text,
             "annotations": [{"type": "url_citation", "url": u, "title": "t"} for u in cited]}]},
        ]}).encode())

    opener.calls = calls
    return opener


class OpenAISearch(unittest.TestCase):
    LISTINGS = [
        {"title": "Rumah teres Sungai Petani untuk dijual owner", "url": "https://www.mudah.my/rumah-sp-777.htm",
         "price_rm": 380000, "location": "Sungai Petani", "description": "Owner jual, 4 bilik, 012-9999999"},
        {"title": "Made-up house for sale Kulim", "url": "https://www.mudah.my/does-not-exist-1.htm",
         "price_rm": 250000, "location": "Kulim", "description": "Not a real result"},
        {"title": "Made-up house for sale Kulim", "url": "https://random-blog.example/house-kulim",
         "price_rm": 250000, "location": "Kulim", "description": "Site the search never visited"},
    ]

    def test_only_cited_links_are_kept(self):
        from listingai.websearch import run_openai_search
        opener = fake_openai_search(self.LISTINGS, ["https://mudah.my/rumah-sp-777.htm?utm_source=openai"])
        run = run_openai_search(["Sungai Petani", "Kulim"], "sk-x", "gpt-test", opener=opener)
        # Cited exactly: confirmed. Same property site as a cited link: kept, not confirmed. Other site: dropped.
        self.assertEqual([(r.url, r.confirmed) for r in run.results], [
            ("https://www.mudah.my/rumah-sp-777.htm", True), ("https://www.mudah.my/does-not-exist-1.htm", False)])
        self.assertIn("1 dropped (link not from the search)", run.notes[0])
        self.assertEqual((run.results[0].price, run.results[0].location), (380_000, "Sungai Petani"))
        self.assertEqual(opener.calls[0]["tools"], [{"type": "web_search"}])

    def test_falls_back_to_older_tool_name(self):
        from listingai.websearch import run_openai_search
        opener = fake_openai_search(self.LISTINGS, ["https://www.mudah.my/rumah-sp-777.htm"], reject_tool=True)
        run = run_openai_search(["Sungai Petani"], "sk-x", "m", opener=opener)
        self.assertEqual(len(run.results), 2)
        self.assertEqual(opener.calls[-1]["tools"], [{"type": "web_search_preview"}])

    def test_errors(self):
        from listingai.websearch import run_openai_search
        with self.assertRaisesRegex(SearchError, "OpenAI API key"):
            run_openai_search(["Kulim"], "", "m")
        with self.assertRaisesRegex(SearchError, "rejected"):
            run_openai_search(["Kulim"], "sk-bad", "m", opener=fake_openai_search([], [], status=401))

    def test_app_never_uses_ai_search_phone_numbers(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LISTINGAI_HOME": tmp, "OPENAI_API_KEY": ""}):
            from listingai.campaigns import new_campaign
            from listingai.server import App
            app = App(None)
            app.campaigns = [new_campaign("Kedah", ["Sungai Petani"])]
            app.settings.openai_api_key = "sk-x"
            self.assertTrue(app.settings.web_search_ready)
            opener = fake_openai_search(self.LISTINGS, ["https://www.mudah.my/rumah-sp-777.htm"])
            with mock.patch("listingai.websearch.urllib.request.urlopen", opener):
                self.assertEqual(app.search_web_now(), "Ran 1 search: 2 new listings added, 1 in your campaigns.")
            self.assertIn("link not confirmed", app.listings[1].source)
            l = app.listings[0]
            self.assertIsNone(l.public_phone)
            self.assertFalse(l.has_eligible_contact)
            self.assertIn("Automatic", app.settings_page())
            self.assertIn("AI returned 3", app.settings_page())

    def test_prose_answer_is_explained(self):
        from listingai.websearch import run_openai_search

        def opener(req, timeout=None):
            return _Resp(json.dumps({"output": [{"type": "message", "content": [
                {"type": "output_text", "text": "I could not find any recent listings.", "annotations": []}]}]}).encode())
        run = run_openai_search(["Kulim"], "sk", "m", opener=opener)
        self.assertIn("without a list: I could not find", run.notes[0])


class AppFlow(unittest.TestCase):
    def test_search_now_adds_listings_once(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LISTINGAI_HOME": tmp}):
            from listingai.campaigns import new_campaign
            from listingai.server import App
            app = App(None)
            app.campaigns = [new_campaign("Kedah", ["Sungai Petani", "Kulim"], max_price=300_000)]
            app.settings.brave_api_key = "BSA-key"
            app.settings.web_provider = "brave"
            with mock.patch("listingai.websearch.urllib.request.urlopen", fake_brave()), \
                 mock.patch("listingai.websearch.time.sleep"):
                msg = app.search_web_now()
                self.assertEqual(msg, "Ran 4 searches: 2 new listings added, 1 in your campaigns.")
                sp = next(l for l in app.listings if "mudah" in l.post_url)
                self.assertEqual(sp.public_phone, "012-3456789")
                self.assertIsNotNone(sp.exclusive_agent)  # has a phone, so agent intent was checked
                self.assertTrue(sp.source.startswith("Web search (mudah.my)"))
                self.assertIn("2 already in ListingAI", app.search_web_now())
            page = app.settings_page()
            self.assertIn("Last search", page)
            self.assertNotIn("BSA-key", page)
