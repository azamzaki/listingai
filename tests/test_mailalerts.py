import os
import tempfile
import unittest
from email.message import EmailMessage
from unittest import mock

from listingai.mailalerts import SITES, MailError, fetch_alert_items, parse_alert, site_for_sender

PG_HTML = """<html><head><style>.x{}</style></head><body>
<a href="https://www.propertyguru.com.my/"><img src="logo.png"></a>
<table>
<tr><td><a href="https://www.propertyguru.com.my/property-listing/taman-ria-jaya-for-sale-by-owner-500123"><img src="a.jpg"></a></td>
<td><a href="https://www.propertyguru.com.my/property-listing/taman-ria-jaya-for-sale-by-owner-500123">2 Storey Terrace, Taman Ria Jaya</a>
<br>Sungai Petani, Kedah<br>RM 385,000<br>3 Beds 2 Baths 1,400 sqft
<a href="https://www.propertyguru.com.my/property-listing/taman-ria-jaya-for-sale-by-owner-500123">View listing</a></td></tr>
<tr><td><a href="https://click.propertyguru.com.my/ls/click?upn=abc">Single Storey Terrace Bukit Mertajam</a>
<br>Bukit Mertajam, Penang<br>RM 260,000</td></tr>
</table>
<a href="https://www.propertyguru.com.my/unsubscribe?x=1">Unsubscribe</a>
<a href="https://www.facebook.com/propertyguru">Facebook</a>
</body></html>"""

MUDAH_TEXT = """New ads matching your saved search "Rumah Kulim"

Rumah teres setingkat Kulim, owner jual
RM 230,000 - Kulim, Kedah
https://www.mudah.my/rumah-teres-setingkat-kulim-owner-jual-104455667.htm

Manage your saved searches: https://www.mudah.my/account/searches
"""


def make_email(sender, subject, body, html=True, mid="<1@x>"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Message-ID"] = mid
    if html:
        msg.set_content("plain version")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


class FakeImap:
    def __init__(self, messages, password="goodpass"):
        self.messages = messages  # {id: (from_domain, bytes)}
        self.password = password
        self.fetch_specs = []
        self.selected_readonly = None

    def __call__(self, host, *args, **kwargs):
        return self

    def login(self, user, pw):
        import imaplib
        if pw != self.password:
            raise imaplib.IMAP4.error("auth failed")

    def select(self, box, readonly=False):
        self.selected_readonly = readonly
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        domain = criteria[-1].strip('"')
        ids = [i for i, (d, _) in self.messages.items() if d == domain]
        return "OK", [b" ".join(ids)]

    def fetch(self, msg_id, spec):
        self.fetch_specs.append(spec)
        return "OK", [(b"1", self.messages[msg_id][1])]

    def logout(self):
        pass


class Parsing(unittest.TestCase):
    def test_propertyguru_html(self):
        items = parse_alert(SITES["propertyguru"], PG_HTML)
        self.assertEqual([(i.title, i.price, i.location) for i in items], [
            ("2 Storey Terrace, Taman Ria Jaya", 385_000, "Taman Ria Jaya"),
            ("Single Storey Terrace Bukit Mertajam", 260_000, "Bukit Mertajam"),
        ])
        self.assertIn("500123", items[0].url)

    def test_mudah_plain_text(self):
        items = parse_alert(SITES["mudah"], MUDAH_TEXT, is_html=False)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].price, items[0].location), (230_000, "Kulim"))
        self.assertTrue(items[0].url.endswith("104455667.htm"))

    def test_sender_matching(self):
        self.assertEqual(site_for_sender("PropertyGuru <alerts@mailer.propertyguru.com.my>", ["propertyguru"]).key, "propertyguru")
        self.assertIsNone(site_for_sender("PropertyGuru <alerts@propertyguru.com.my>", ["mudah"]))
        self.assertIsNone(site_for_sender("Scammer <x@propertyguru.com.my.evil.com>", ["propertyguru"]))


class Fetching(unittest.TestCase):
    def setUp(self):
        self.imap = FakeImap({
            b"1": ("propertyguru.com.my", make_email("PG <alerts@propertyguru.com.my>", "New listings", PG_HTML, mid="<pg1@x>")),
            b"2": ("mudah.my", make_email("Mudah <noreply@mudah.my>", "Saved search", MUDAH_TEXT, html=False, mid="<m1@x>")),
        })

    def test_reads_new_alerts_without_marking_read(self):
        r = fetch_alert_items("me@gmail.com", "goodpass", "imap.gmail.com", ["mudah", "propertyguru"], [], imap_factory=self.imap)
        self.assertEqual(r.emails_read, 2)
        self.assertEqual(len(r.items), 3)
        self.assertTrue(self.imap.selected_readonly)
        self.assertTrue(all("PEEK" in s for s in self.imap.fetch_specs))
        again = fetch_alert_items("me@gmail.com", "goodpass", "h", ["mudah", "propertyguru"], r.new_seen, imap_factory=self.imap)
        self.assertEqual((again.emails_read, again.items), (0, []))

    def test_errors(self):
        with self.assertRaises(MailError):
            fetch_alert_items("", "", "h", ["mudah"], [], imap_factory=self.imap)
        with self.assertRaisesRegex(MailError, "app password"):
            fetch_alert_items("me@gmail.com", "wrong", "h", ["mudah"], [], imap_factory=self.imap)


class AppFlow(unittest.TestCase):
    def test_check_adds_listings_matches_campaigns_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LISTINGAI_HOME": tmp}):
            from listingai.campaigns import new_campaign
            from listingai.server import App
            app = App(None)
            app.campaigns = [new_campaign("Kedah", ["Sungai Petani", "Kulim"])]
            app.settings.email_address, app.settings.email_app_password = "me@gmail.com", "goodpass"
            imap = FakeImap({
                b"1": ("propertyguru.com.my", make_email("PG <a@propertyguru.com.my>", "s", PG_HTML, mid="<pg1@x>")),
                b"2": ("mudah.my", make_email("M <n@mudah.my>", "s", MUDAH_TEXT, html=False, mid="<m1@x>")),
            })
            with mock.patch("listingai.mailalerts.imaplib.IMAP4_SSL", imap):
                msg = app.check_email_now()
            self.assertEqual(msg, "Read 2 alert emails: 3 new listings added, 2 in your campaigns.")
            page = app.settings_page()
            self.assertIn("Last check", page)
            self.assertIn("PropertyGuru: <b>1</b> email", page)
            self.assertIn("Bukit Mertajam (RM260,000)", page)  # added but outside the Kedah campaign
            self.assertEqual(sorted(l.source for l in app.listings),
                             ["Mudah email alert", "PropertyGuru email alert", "PropertyGuru email alert"])
            in_campaign = sorted(l.location for l in app.listings if l.campaign_ids)
            self.assertEqual(in_campaign, ["Kulim", "Taman Ria Jaya"])  # Taman Ria Jaya post names Sungai Petani
            with mock.patch("listingai.mailalerts.imaplib.IMAP4_SSL", imap):
                self.assertEqual(app.check_email_now(), "No new alert emails since the last check.")
                self.assertIn("already read", app.settings_page())
                self.assertIn("3 already in ListingAI", app.check_email_now(reread=True))
            app.post("/listings/delete", {"id": app.listings[0].id})
            self.assertEqual(len(app.listings), 2)
            self.assertNotIn("goodpass", app.settings_page())
            self.assertIn('data-csrf="', app.dashboard())
