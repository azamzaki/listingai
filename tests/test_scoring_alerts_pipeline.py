import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from listingai.alerts import (
    EXCLUSIVE_TAG,
    build_alert_email,
    build_subject,
    exclusive_opportunity_decision,
    immediate_alert_decision,
)
from listingai.config import ExclusiveAgentConfig
from listingai.dashboard import badges_for, filter_by_status, render_dashboard_html, sort_for_dashboard
from listingai.exclusive_agent import apply_exclusive_agent_detection
from listingai.models import ExclusiveAgentStatus as S, Listing
from listingai.pipeline import (
    TO_CONTACT,
    can_enter_to_contact,
    move_to_contact,
    override_contact_block,
    owner_instruction,
    review_queue,
)
from listingai.scoring import compute_opportunity_score

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
CFG = ExclusiveAgentConfig(dashboard_base_url="https://dash.test")


def make(caption, **kw):
    defaults = dict(
        id="L1", post_url="https://facebook.com/post/1", location="Bangi", price=650_000, caption=caption,
        is_direct_owner=True, public_phone="012-3456789", has_eligible_contact=True,
        matches_target_location=True, posted_at=NOW - timedelta(days=1), scam_risk_score=10,
        base_opportunity_score=77,
    )
    defaults.update(kw)
    l = Listing(**defaults)
    apply_exclusive_agent_detection(l, CFG, NOW)
    return l


class Scoring(unittest.TestCase):
    def test_adjustments(self):
        self.assertEqual(compute_opportunity_score(make("Looking for sole agent"), CFG, NOW).score, 92)
        self.assertEqual(compute_opportunity_score(make("Agents welcome"), CFG, NOW).score, 82)
        self.assertEqual(compute_opportunity_score(make("Exclusive listing"), CFG, NOW).score, 57)
        self.assertEqual(compute_opportunity_score(make("Rumah Bangi"), CFG, NOW).score, 77)
        self.assertEqual(compute_opportunity_score(make("No agent"), CFG, NOW).score, 77)

    def test_bonus_configurable(self):
        cfg = replace(CFG, seeking_exclusive_bonus=3)
        l = make("Looking for sole agent")
        self.assertEqual(compute_opportunity_score(l, cfg, NOW).score, 80)

    def test_bonus_does_not_override_hard_gates(self):
        for kw in [dict(scam_risk_score=90), dict(privacy_blocked=True), dict(has_eligible_contact=False),
                   dict(matches_target_location=False)]:
            with self.subTest(**kw):
                s = compute_opportunity_score(make("Looking for sole agent", **kw), CFG, NOW)
                self.assertEqual(s.exclusive_agent_adjustment, 0)
                self.assertTrue(s.hard_gate_failures)

    def test_uncertain_held_and_rejects_excluded(self):
        self.assertTrue(compute_opportunity_score(make("Need exclusive agent? haha"), CFG, NOW).held_for_review)
        self.assertTrue(compute_opportunity_score(make("No agent"), CFG, NOW).excluded_from_immediate_alerts)
        self.assertFalse(compute_opportunity_score(make("No agent"), replace(CFG, alert_on_rejects_agents=True), NOW).excluded_from_immediate_alerts)


class Alerts(unittest.TestCase):
    def test_subject_matches_spec_example(self):
        l = make("Looking for an exclusive agent")
        self.assertEqual(build_subject(l, CFG, NOW), "[EXCLUSIVE OPPORTUNITY][92/100] Direct Owner – Bangi RM650,000")

    def test_subject_without_tag_when_not_seeking(self):
        self.assertFalse(build_subject(make("Agents welcome"), CFG, NOW).startswith(EXCLUSIVE_TAG))

    def test_email_body_contents(self):
        l = make("Owner jual. Nak lantik seorang ejen sahaja!", public_email="owner@example.com")
        msg = build_alert_email(l, CFG, NOW)
        body = msg.get_body(("plain",)).get_content()
        for expected in ["Mencari Ejen Eksklusif", "seeking_exclusive_agent", "%", "Nak lantik seorang ejen sahaja",
                         "caption", "012-3456789", "owner@example.com", "https://facebook.com/post/1",
                         "https://dash.test/listings/L1"]:
            self.assertIn(expected, body)
        self.assertIn("https://dash.test/listings/L1", msg.get_body(("html",)).get_content())

    def test_exclusive_decision_all_rules(self):
        self.assertTrue(exclusive_opportunity_decision(make("Looking for sole agent"), CFG, NOW).send)
        failing = {
            "no_eligible_contact": dict(has_eligible_contact=False),
            "outside_target_location": dict(matches_target_location=False),
            "confirmed_duplicate": dict(is_confirmed_duplicate=True),
            "too_old": dict(posted_at=NOW - timedelta(days=60)),
            "scam_risk_too_high": dict(scam_risk_score=40),
        }
        for reason, kw in failing.items():
            with self.subTest(reason=reason):
                d = exclusive_opportunity_decision(make("Looking for sole agent", **kw), CFG, NOW)
                self.assertFalse(d.send)
                self.assertIn(reason, d.reasons)

    def test_confidence_threshold(self):
        l = make("Perlukan ejen untuk uruskan jualan")  # confidence 0.85
        self.assertTrue(exclusive_opportunity_decision(l, CFG, NOW).send)
        strict = replace(CFG, confidence_threshold=0.9)
        l2 = make("Perlukan ejen untuk uruskan jualan")
        apply_exclusive_agent_detection(l2, strict, NOW)
        self.assertEqual(l2.exclusive_agent.exclusive_agent_status, S.UNCERTAIN)
        self.assertFalse(exclusive_opportunity_decision(l2, strict, NOW).send)

    def test_no_exclusive_alert_for_other_statuses(self):
        for caption in ["No agent", "Agents welcome", "Exclusive listing", "Rumah Bangi", "Need exclusive agent? haha"]:
            with self.subTest(caption=caption):
                self.assertFalse(exclusive_opportunity_decision(make(caption), CFG, NOW).send)

    def test_immediate_alert_excludes_rejects_and_uncertain(self):
        self.assertIn("owner_rejects_agents", immediate_alert_decision(make("No agent"), CFG, NOW).reasons)
        self.assertIn("pending_review", immediate_alert_decision(make("Need exclusive agent? haha"), CFG, NOW).reasons)
        self.assertTrue(immediate_alert_decision(make("Rumah Bangi"), CFG, NOW).send)


class Pipeline(unittest.TestCase):
    def test_rejects_blocked_from_to_contact(self):
        l = make("Ejen jangan hubungi")
        self.assertEqual(can_enter_to_contact(l), (False, "owner_rejects_agents"))
        with self.assertRaises(PermissionError):
            move_to_contact(l)
        self.assertIn("Ejen jangan hubungi", owner_instruction(l))

    def test_manual_override(self):
        l = make("No agent")
        with self.assertRaises(ValueError):
            override_contact_block(l, "aina", "")
        override_contact_block(l, "aina", "Owner called us first", NOW)
        move_to_contact(l)
        self.assertEqual(l.pipeline_stage, TO_CONTACT)

    def test_seeking_enters_pipeline(self):
        l = make("Looking for sole agent")
        move_to_contact(l)
        self.assertEqual(l.pipeline_stage, TO_CONTACT)

    def test_review_queue(self):
        items = [make("Looking for sole agent"), make("Need exclusive agent? haha", id="L2")]
        self.assertEqual([l.id for l in review_queue(items)], ["L2"])


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.items = [
            make("No agent", id="rej"),
            make("Exclusive listing", id="has"),
            make("Rumah Bangi", id="none"),
            make("Agents welcome", id="open"),
            make("Need exclusive agent? haha", id="unc"),
            make("Looking for sole agent", id="seek"),
        ]

    def test_badges(self):
        labels = {l.id: badges_for(l)[0].label for l in self.items}
        self.assertEqual(labels, {
            "seek": "Mencari Ejen Eksklusif", "open": "Terbuka Melantik Ejen", "has": "Sudah Ada Ejen Eksklusif",
            "rej": "Tidak Mahu Ejen", "none": "Tiada Bukti", "unc": "Perlu Semakan",
        })

    def test_seeking_sorted_first(self):
        self.assertEqual(sort_for_dashboard(self.items)[0].id, "seek")

    def test_filters_for_every_status(self):
        for status in S:
            with self.subTest(status=status):
                self.assertEqual(len(filter_by_status(self.items, [status.value])), 1)
        self.assertEqual(len(filter_by_status(self.items)), 6)

    def test_html_shows_owner_instruction_and_filters(self):
        page = render_dashboard_html(self.items, CFG)
        self.assertIn("Pemilik tidak mahu dihubungi oleh ejen", page)
        for status in S:
            self.assertIn(f"value='{status.value}'", page)
        self.assertLess(page.index("data-status='seeking_exclusive_agent'"), page.index("data-status='rejects_agents'"))


if __name__ == "__main__":
    unittest.main()
