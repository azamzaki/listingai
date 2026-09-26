import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import urlencode

from listingai.campaigns import apply_campaigns, new_campaign, parse_locations
from listingai.exclusive_agent import classify_exclusive_agent
from listingai.llm import OpenAIError, classify_with_ai, test_api_key as check_key
from listingai.models import EvidenceSource as Src, ExclusiveAgentStatus as S, Listing, TextEvidence

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_openai(answer=None, status=200):
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(b'{"error":{"message":"bad"}}'))
        if req.get_method() == "GET":
            return _Resp(b'{"id":"m"}')
        return _Resp(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode())

    opener.calls = calls
    return opener


def ev(text, source=Src.CAPTION, **kw):
    return [TextEvidence(source, text, **kw)]


class Campaigns(unittest.TestCase):
    def listing(self, location, caption="", price=500_000):
        return Listing(id=location, post_url="", location=location, caption=caption, price=price, matches_target_location=False)

    def test_parse_locations(self):
        self.assertEqual(parse_locations("Bangi, Kajang\nSemenyih; bangi ,  "), ["Bangi", "Kajang", "Semenyih"])

    def test_matching_by_location_caption_and_price(self):
        c = new_campaign("Bangi", ["Bangi", "Bandar Baru Bangi"], min_price=400_000, max_price=700_000)
        a = self.listing("Bangi")
        b = self.listing("Selangor", caption="Rumah di Bandar Baru Bangi seksyen 9")
        too_dear = self.listing("Bangi", price=900_000)
        other = self.listing("Kajang")
        partial = self.listing("Bangika")  # whole words only
        items = [a, b, too_dear, other, partial]
        apply_campaigns(items, [c])
        self.assertEqual([l.matches_target_location for l in items], [True, True, False, False, False])
        self.assertEqual(a.campaign_ids, [c.id])

    def test_paused_or_no_campaigns_keep_listing_flag(self):
        l = self.listing("Kajang")
        l.matches_target_location = True
        c = new_campaign("Bangi", ["Bangi"], active=False)
        apply_campaigns([l], [c])
        self.assertTrue(l.matches_target_location)
        apply_campaigns([l], [])
        self.assertTrue(l.matches_target_location)

    def test_validation(self):
        for args in [("", ["Bangi"]), ("X", []), ("X", ["Bangi"], 5, 1)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                new_campaign(*args)


class OpenAI(unittest.TestCase):
    def run_ai(self, text, answer, **kw):
        return classify_with_ai(ev(text, **kw), "sk-test", "gpt-test", now=NOW, opener=fake_openai(answer))

    def test_ai_finds_what_rules_miss(self):
        text = "Rumah Bangi. Saya mahu seorang wakil hartanah sahaja uruskan semua"
        self.assertEqual(classify_exclusive_agent(ev(text), now=NOW).exclusive_agent_status, S.NO_EVIDENCE)
        r = self.run_ai(text, {"status": "seeking_exclusive_agent", "confidence": 0.9, "evidence_item": 1,
                               "evidence": "mahu seorang wakil hartanah sahaja"})
        self.assertEqual(r.exclusive_agent_status, S.SEEKING_EXCLUSIVE_AGENT)
        self.assertEqual(r.exclusive_agent_evidence, "mahu seorang wakil hartanah sahaja")
        self.assertEqual(r.classified_by, "openai")

    def test_ai_quote_not_in_post_is_rejected(self):
        r = self.run_ai("Rumah Bangi RM650k", {"status": "seeking_exclusive_agent", "confidence": 0.99,
                                              "evidence_item": 1, "evidence": "looking for exclusive agent"})
        self.assertEqual(r.exclusive_agent_status, S.NO_EVIDENCE)
        self.assertIn("AI quote not found", r.review_reason)

    def test_disagreement_goes_to_review(self):
        r = self.run_ai("Agents welcome", {"status": "rejects_agents", "confidence": 0.9, "evidence_item": 1, "evidence": "Agents welcome"})
        self.assertEqual(r.exclusive_agent_status, S.UNCERTAIN)
        self.assertTrue(r.exclusive_agent_review_required)

    def test_agreement_keeps_status(self):
        r = self.run_ai("No agent", {"status": "rejects_agents", "confidence": 0.99, "evidence_item": 1, "evidence": "No agent"})
        self.assertEqual(r.exclusive_agent_status, S.REJECTS_AGENTS)
        self.assertEqual(r.classified_by, "rules+openai")

    def test_ai_cannot_resolve_uncertain_rule(self):
        r = self.run_ai("Looking for exclusive ag...", {"status": "seeking_exclusive_agent", "confidence": 0.95,
                                                       "evidence_item": 1, "evidence": "Looking for exclusive ag"})
        self.assertEqual(r.exclusive_agent_status, S.UNCERTAIN)
        self.assertIn("AI suggests", r.review_reason)

    def test_low_ai_confidence_is_uncertain(self):
        r = self.run_ai("Saya mahu wakil hartanah", {"status": "seeking_exclusive_agent", "confidence": 0.5,
                                                    "evidence_item": 1, "evidence": "mahu wakil hartanah"})
        self.assertEqual(r.exclusive_agent_status, S.UNCERTAIN)

    def test_non_owner_comment_cannot_be_evidence(self):
        evidence = ev("Rumah Bangi") + [TextEvidence(Src.COMMENT, "mahu wakil hartanah sahaja", author_is_owner=False)]
        r = classify_with_ai(evidence, "sk-test", "m", now=NOW, opener=fake_openai(
            {"status": "seeking_exclusive_agent", "confidence": 0.95, "evidence_item": 2, "evidence": "mahu wakil hartanah sahaja"}))
        self.assertEqual(r.exclusive_agent_status, S.NO_EVIDENCE)

    def test_api_failure_keeps_rules(self):
        r = classify_with_ai(ev("Looking for sole agent"), "sk-bad", "m", now=NOW, opener=fake_openai(status=401))
        self.assertEqual(r.exclusive_agent_status, S.SEEKING_EXCLUSIVE_AGENT)
        self.assertIn("rejected the API key", r.review_reason)

    def test_key_check(self):
        self.assertIn("Connected", check_key("sk-x", "m", opener=fake_openai()))
        with self.assertRaises(OpenAIError):
            check_key("sk-x", "m", opener=fake_openai(status=401))
        with self.assertRaises(OpenAIError):
            check_key("", "m")

    def test_request_carries_key_only_in_header(self):
        opener = fake_openai({"status": "no_evidence", "confidence": 0.9})
        classify_with_ai(ev("Rumah Bangi"), "sk-secret", "m", now=NOW, opener=opener)
        req = opener.calls[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-secret")
        self.assertNotIn(b"sk-secret", req.data)


class WebApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LISTINGAI_HOME": self.tmp.name, "OPENAI_API_KEY": ""})
        self.env.start()
        from listingai.server import App, make_handler

        self.app = App(None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        self.opener = urllib.request.build_opener(NoRedirect)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def get(self, path):
        return urllib.request.urlopen(self.base + path).read().decode()

    def post(self, path, **form):
        form.setdefault("csrf", self.app.csrf)
        req = urllib.request.Request(self.base + path, data=urlencode(form).encode())
        try:
            self.opener.open(req)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location")

    def test_pages_render(self):
        for path in ["/", "/settings", "/campaigns", "/add"]:
            with self.subTest(path=path):
                self.assertIn("ListingAI", self.get(path))

    def test_csrf_required(self):
        self.assertEqual(self.post("/settings/remove", csrf="wrong")[0], 403)

    def test_save_key_never_shown(self):
        with mock.patch("listingai.server.test_api_key", return_value="Connected."):
            code, loc = self.post("/settings", api_key="sk-abcdefghijklmnop1234", model="gpt-test")
        self.assertEqual((code, loc), (303, "/settings"))
        page = self.get("/settings")
        self.assertNotIn("sk-abcdefghijklmnop1234", page)
        self.assertIn("1234", page)  # masked hint only
        self.assertNotIn("abcdefghijklmnop", self.get("/"))
        with open(os.path.join(self.tmp.name, "settings.json")) as f:
            saved = json.load(f)
        self.assertEqual(saved["openai_api_key"], "sk-abcdefghijklmnop1234")

    def test_reject_non_openai_key(self):
        self.post("/settings", api_key="hello", model="m")
        self.assertIn("Keys start with sk-", self.get("/settings"))
        self.assertEqual(self.app.settings.openai_api_key, "")

    def test_campaign_and_add_listing_flow(self):
        self.post("/campaigns/save", name="Bangi landed", locations="Bangi, Kajang", min_price="", max_price="800000", active="on")
        self.assertIn("Bangi landed", self.get("/campaigns"))
        camp = self.app.campaigns[0]

        today = datetime.now().strftime("%Y-%m-%d")
        self.post("/add", caption="Owner jual. Nak lantik seorang ejen sahaja", location="Bangi", price="650000",
                  phone="012-3456789", posted=today, base_score="77", scam_risk="0", direct_owner="on")
        self.post("/add", caption="Rumah Ipoh, agents welcome", location="Ipoh", price="300000",
                  phone="012-3456780", posted=today, base_score="70", scam_risk="0")
        bangi, ipoh = self.app.listings
        self.assertEqual(bangi.exclusive_agent.exclusive_agent_status, S.SEEKING_EXCLUSIVE_AGENT)
        self.assertEqual(bangi.campaign_ids, [camp.id])
        self.assertTrue(bangi.matches_target_location)
        self.assertFalse(ipoh.matches_target_location)
        page = self.get("/")
        self.assertIn("Bangi landed", page)
        self.assertIn("Nak lantik seorang ejen sahaja", page)

        self.post("/campaigns/toggle", id=camp.id)
        self.assertFalse(self.app.campaigns[0].active)
        self.post("/campaigns/delete", id=camp.id)
        self.assertEqual(self.app.campaigns, [])

        # Data survives a restart.
        from listingai.server import App
        again = App(None)
        self.assertEqual(len(again.listings), 2)

    def test_add_validation(self):
        self.post("/add", caption="x", location="Bangi", phone="12", posted="")
        self.assertIn("at least 9 digits", self.get("/add"))
        self.assertEqual(self.app.listings, [])

    def test_busy_port_falls_back_to_next(self):
        from listingai.server import bind_server
        busy = self.httpd.server_address[1]
        other = bind_server(self.app, busy)
        try:
            self.assertNotEqual(other.server_address[1], busy)
        finally:
            other.server_close()

    def test_foreign_host_blocked(self):
        req = urllib.request.Request(self.base + "/settings", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
