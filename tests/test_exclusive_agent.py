import unittest

from listingai.exclusive_agent import apply_exclusive_agent_detection, classify_exclusive_agent
from listingai.models import (
    AgentContactPreference,
    EvidenceSource as Src,
    ExclusiveAgentStatus as S,
    Listing,
    TextEvidence,
)


def classify(text, source=Src.CAPTION, **kw):
    return classify_exclusive_agent([TextEvidence(source, text, **kw)])


class SpecExamples(unittest.TestCase):
    CASES = {
        S.SEEKING_EXCLUSIVE_AGENT: [
            "Looking for an exclusive agent",
            "Perlukan ejen untuk uruskan jualan",
            "Nak lantik seorang ejen sahaja",
            "Need one agent to handle everything",
            "Looking for sole agent",
            "Prefer exclusive agent",
            "Saya owner dan perlukan agent jualkan rumah",
            "Mencari ejen eksklusif",
        ],
        S.OPEN_TO_AGENT_APPOINTMENT: [
            "Agents are welcome",
            "Boleh co-broke",
            "Agent boleh PM",
            "Looking for property agent",
            "Ejen dialu-alukan",
        ],
        S.ALREADY_HAS_EXCLUSIVE_AGENT: [
            "Exclusive listing",
            "Sole agent appointed",
            "Sudah lantik ejen",
            "Contact appointed agent",
            "Under exclusive agency",
        ],
        S.REJECTS_AGENTS: [
            "No agent",
            "Agents please do not contact",
            "Direct buyer only",
            "No co-broke",
            "Owner deal only",
            "Ejen jangan hubungi",
        ],
    }

    def test_examples(self):
        for expected, phrases in self.CASES.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    r = classify(f"Rumah teres Bangi RM650k. {phrase}. Call 012-3456789")
                    self.assertEqual(r.exclusive_agent_status, expected)
                    self.assertFalse(r.exclusive_agent_review_required)
                    # Evidence is an exact substring of the source text.
                    self.assertIn(r.exclusive_agent_evidence.lower(), phrase.lower())
                    self.assertEqual(r.exclusive_agent_evidence_source, Src.CAPTION)

    def test_no_evidence(self):
        r = classify("Rumah teres 2 tingkat, Bangi. RM650,000. Freehold.")
        self.assertEqual(r.exclusive_agent_status, S.NO_EVIDENCE)
        self.assertIsNone(r.exclusive_agent_evidence)
        self.assertFalse(r.exclusive_agent_review_required)
        self.assertEqual(r.agent_contact_preference, AgentContactPreference.UNKNOWN)


class Rules(unittest.TestCase):
    def test_direct_owner_alone_is_not_seeking(self):
        for text in ["Direct owner. Owner sell. Rumah Bangi RM650k", "Saya owner, jual sendiri"]:
            self.assertEqual(classify(text).exclusive_agent_status, S.NO_EVIDENCE)

    def test_listing_direct_owner_flag_is_ignored(self):
        l = Listing(id="1", post_url="https://x/1", location="Bangi", caption="Owner sell",
                    is_direct_owner=True, public_phone="0123456789")
        self.assertEqual(apply_exclusive_agent_detection(l).exclusive_agent_status, S.NO_EVIDENCE)

    def test_listing_without_public_contact_is_not_eligible(self):
        l = Listing(id="1", post_url="https://x/1", location="Bangi", caption="Looking for exclusive agent")
        self.assertIsNone(apply_exclusive_agent_detection(l))

    def test_no_agent_fee_is_not_a_rejection(self):
        self.assertEqual(classify("No agent fee, direct from owner").exclusive_agent_status, S.NO_EVIDENCE)

    def test_negated_seeking_is_rejection(self):
        for text in ["Not looking for an exclusive agent", "Tak perlukan ejen untuk uruskan", "Tidak mahu ejen"]:
            with self.subTest(text=text):
                self.assertEqual(classify(text).exclusive_agent_status, S.REJECTS_AGENTS)

    def test_low_ocr_confidence_is_uncertain(self):
        r = classify("Looking for exclusive agent", Src.IMAGE_OCR, source_confidence=0.45)
        self.assertEqual(r.exclusive_agent_status, S.UNCERTAIN)
        self.assertTrue(r.exclusive_agent_review_required)
        self.assertEqual(r.exclusive_agent_evidence_source, Src.IMAGE_OCR)

    def test_high_ocr_confidence_is_accepted(self):
        r = classify("Looking for exclusive agent", Src.IMAGE_OCR, source_confidence=0.97)
        self.assertEqual(r.exclusive_agent_status, S.SEEKING_EXCLUSIVE_AGENT)

    def test_truncated_ocr_is_never_completed(self):
        r = classify("Owner jual. Looking for exclusive ag...", Src.IMAGE_OCR, source_confidence=0.9)
        self.assertEqual(r.exclusive_agent_status, S.UNCERTAIN)
        self.assertTrue(r.exclusive_agent_review_required)
        self.assertNotIn("agent", r.exclusive_agent_evidence)
        self.assertIn("ag...", r.exclusive_agent_evidence)

    def test_question_and_sarcasm_are_uncertain(self):
        for text in ["Need exclusive agent? haha", "Maybe looking for exclusive agent", "Agents welcome lol 🙄"]:
            with self.subTest(text=text):
                r = classify(text)
                self.assertEqual(r.exclusive_agent_status, S.UNCERTAIN)
                self.assertTrue(r.exclusive_agent_review_required)

    def test_comment_by_non_owner_ignored(self):
        r = classify_exclusive_agent([
            TextEvidence(Src.CAPTION, "Rumah Bangi RM650k"),
            TextEvidence(Src.COMMENT, "Looking for exclusive agent?", author_is_owner=False),
        ])
        self.assertEqual(r.exclusive_agent_status, S.NO_EVIDENCE)

    def test_owner_comment_counts(self):
        r = classify_exclusive_agent([
            TextEvidence(Src.CAPTION, "Rumah Bangi RM650k"),
            TextEvidence(Src.COMMENT, "Nak lantik seorang ejen sahaja"),
        ])
        self.assertEqual(r.exclusive_agent_status, S.SEEKING_EXCLUSIVE_AGENT)
        self.assertEqual(r.exclusive_agent_evidence_source, Src.COMMENT)

    def test_video_transcript_source(self):
        r = classify_exclusive_agent([TextEvidence(Src.VIDEO_TRANSCRIPT, "saya perlukan ejen untuk jualkan rumah ni", source_confidence=0.95)])
        self.assertEqual(r.exclusive_agent_status, S.SEEKING_EXCLUSIVE_AGENT)
        self.assertEqual(r.exclusive_agent_evidence_source, Src.VIDEO_TRANSCRIPT)

    def test_manual_verification_supersedes(self):
        r = classify_exclusive_agent([
            TextEvidence(Src.CAPTION, "No agent"),
            TextEvidence(Src.MANUALLY_VERIFIED, "Owner confirmed by phone", verified_status=S.OPEN_TO_AGENT_APPOINTMENT, verified_by="aina"),
        ])
        self.assertEqual(r.exclusive_agent_status, S.OPEN_TO_AGENT_APPOINTMENT)
        self.assertEqual(r.exclusive_agent_evidence_source, Src.MANUALLY_VERIFIED)
        self.assertEqual(r.exclusive_agent_confidence, 1.0)

    def test_conflicting_reject_and_welcome_keeps_rejection_for_review(self):
        r = classify("No agent. Agents welcome.")
        self.assertEqual(r.exclusive_agent_status, S.REJECTS_AGENTS)
        self.assertTrue(r.exclusive_agent_review_required)

    def test_conflicting_appointed_and_seeking_is_uncertain(self):
        r = classify("Exclusive listing. Looking for exclusive agent.")
        self.assertEqual(r.exclusive_agent_status, S.UNCERTAIN)
        self.assertTrue(r.exclusive_agent_review_required)

    def test_exclusive_listing_with_cobroke(self):
        r = classify("Exclusive listing, co-broke welcome")
        self.assertEqual(r.exclusive_agent_status, S.ALREADY_HAS_EXCLUSIVE_AGENT)

    def test_appointed_agent_name_and_ren(self):
        r = classify("Sole agent appointed. Contact appointed agent: Ahmad Faizal (REN 31234) 012-3456789")
        self.assertEqual(r.exclusive_agent_status, S.ALREADY_HAS_EXCLUSIVE_AGENT)
        self.assertEqual(r.already_appointed_agent_name, "Ahmad Faizal")
        self.assertEqual(r.already_appointed_agent_ren, "REN 31234")
        self.assertEqual(r.agent_contact_preference, AgentContactPreference.APPOINTED_AGENT_ONLY)

    def test_agent_name_stops_before_ren(self):
        r = classify("Agent: Siti Aminah REN 12345. Exclusive listing")
        self.assertEqual(r.already_appointed_agent_name, "Siti Aminah")
        self.assertEqual(r.already_appointed_agent_ren, "REN 12345")

    def test_agent_details_not_filled_unless_appointed(self):
        r = classify("Looking for exclusive agent. REN 31234 jangan tipu")
        self.assertIsNone(r.already_appointed_agent_ren)

    def test_probability_and_preference(self):
        seeking = classify("Looking for sole agent")
        self.assertGreaterEqual(seeking.exclusive_agent_probability, 0.75)
        self.assertEqual(seeking.agent_contact_preference, AgentContactPreference.EXCLUSIVE_AGENT_WANTED)
        rejects = classify("No agent")
        self.assertEqual(rejects.exclusive_agent_probability, 0.0)
        self.assertEqual(rejects.agent_contact_preference, AgentContactPreference.NO_AGENTS)

    def test_to_dict_serialises_all_fields(self):
        d = classify("Looking for sole agent").to_dict()
        for key in [
            "exclusive_agent_status", "exclusive_agent_probability", "exclusive_agent_confidence",
            "exclusive_agent_evidence", "exclusive_agent_evidence_source", "exclusive_agent_review_required",
            "agent_contact_preference", "already_appointed_agent_name", "already_appointed_agent_ren",
            "exclusive_agent_detected_at",
        ]:
            self.assertIn(key, d)
        self.assertEqual(d["exclusive_agent_status"], "seeking_exclusive_agent")
        self.assertIsInstance(d["exclusive_agent_detected_at"], str)


if __name__ == "__main__":
    unittest.main()
