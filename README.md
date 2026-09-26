# ListingAI

Property-listing lead intelligence. This package currently implements
**exclusive-agent intent detection**: whether an owner who posted a listing with a
public phone number or email wants to appoint an exclusive real estate agent.
It also covers the scoring, alert, pipeline and dashboard rules that depend on it.

No third-party dependencies (Python 3.10+). Run tests with:

```sh
python3 -m unittest
```

## Run the app

```sh
python -m listingai serve
```

This opens http://127.0.0.1:8000 in your browser. It runs only on your computer and has four pages:

- **Dashboard**: every listing with its agent-intent status, score, evidence and filters
  (status, campaign, location, search, sort). "Re-check all with AI" appears once an OpenAI key is saved.
- **Add listing**: paste a post (plus photo text or owner comments). Phone, email, price (RM650k,
  RM1.2 juta…), location (your campaign places first, then common Malaysian areas) and whether the
  owner posted it are read from the text when left blank; with an OpenAI key the AI fills gaps, and a
  phone, email or location it gives must appear in the post. It is then checked straight away.
- **Import posts**: a **+ ListingAI** browser button (drag it to the bookmarks bar) that sends the post
  text you select on Facebook, Marketplace, Mudah, Telegram Web or any site to the Add listing form, and a box
  to paste many posts at once, separated by `---` lines. Duplicates are skipped. ListingAI never logs in to or
  scans Facebook or other sites itself; you pick each post.
- **Campaigns**: create a campaign per area, e.g. "Bangi & Kajang landed" with places
  `Bangi, Bandar Baru Bangi, Kajang` and an optional price range. A listing joins a campaign when its
  location or post text names one of the places (whole words) and its price is in range. When at least
  one campaign is active, only listings in an active campaign count as in a target location, so only they
  can trigger exclusive-opportunity alerts. Campaigns can be edited, paused and deleted.
- **Settings**: save your OpenAI API key and model, test the connection, or remove the key.

Your data (settings, campaigns, listings) is stored in `~/.listingai` (set `LISTINGAI_HOME` to change it).
The API key is stored only there, or read from the `OPENAI_API_KEY` environment variable; it is never
put into a page or the repository. On first run the app loads `sample_listings.csv` as example data.

### How the OpenAI check works

The phrase rules always run first. With a key, the post is also sent to OpenAI, which must quote its
supporting phrase exactly from the post; answers that quote anything else are ignored. The AI can add
a status the rules missed, but it cannot overturn a manual verification or resolve an `uncertain` post
(it adds a note for the reviewer). If the rules and the AI disagree, the listing goes to review.
If OpenAI can't be reached, the rule result is kept.

## Static dashboard from a CSV

```sh
python -m listingai                  # builds dashboard.html from sample_listings.csv and opens it
python -m listingai my_listings.csv  # use your own listings
```

The CSV needs at least `id` and `caption`. Optional columns are `post_url`, `location`, `price`,
`public_phone`, `public_email`, `is_direct_owner`, `in_target_location`, `posted_days_ago`,
`scam_risk_score` and `base_score` (see `sample_listings.csv`). The terminal also lists each
listing's status, whether it would trigger an exclusive-opportunity email, and the email subject.

The dashboard shows summary counts, filter chips for every status, search, a location filter,
sorting, and an "alert-ready only" toggle. Each listing shows its score, badges and the
supporting phrase highlighted in the post. Click a listing to see the full evidence,
confidence, score breakdown, alert checks, To Contact status and public contact details.
It follows the system light/dark theme and works on phones.

## Modules

| Module | Purpose |
| --- | --- |
| `listingai/config.py` | `ExclusiveAgentConfig`: thresholds, score bonuses/penalties, alert limits |
| `listingai/models.py` | `Listing`, `TextEvidence`, `ExclusiveAgentResult` and enums |
| `listingai/exclusive_agent.py` | English + Bahasa Malaysia classifier |
| `listingai/scoring.py` | Opportunity score adjustment and hard gates |
| `listingai/alerts.py` | Alert eligibility and `[EXCLUSIVE OPPORTUNITY]` email builder |
| `listingai/pipeline.py` | To Contact gating, manual override, Review Queue |
| `listingai/dashboard.py` | Badges, status filters, HTML dashboard |
| `listingai/campaigns.py` | Location campaigns and matching |
| `listingai/extract.py` | Reads phone, email, price, location and owner from post text |
| `listingai/llm.py` | Optional OpenAI second opinion |
| `listingai/settings.py` | Local settings and listing storage |
| `listingai/server.py` | Local web app (`python -m listingai serve`) |

## Classification

`exclusive_agent_status` is one of `seeking_exclusive_agent`,
`open_to_agent_appointment`, `already_has_exclusive_agent`, `rejects_agents`,
`no_evidence` or `uncertain`. The result also carries
`exclusive_agent_probability`, `exclusive_agent_confidence`,
`exclusive_agent_evidence` (an exact substring of the source text),
`exclusive_agent_evidence_source` (`caption`, `image_ocr`, `video_transcript`,
`profile`, `comment`, `manually_verified`), `exclusive_agent_review_required`,
`agent_contact_preference`, `already_appointed_agent_name`,
`already_appointed_agent_ren` and `exclusive_agent_detected_at`.

Rules:

- Only listings with a public phone or email are classified.
- Direct-owner status is never an input. "Owner sell" yields `no_evidence`.
- Confidence is the pattern weight × source reliability × OCR/transcript confidence.
  Questions, hedges ("maybe", "mungkin"), sarcasm markers ("haha", "🙄") and
  negations lower it. Anything below `confidence_threshold` becomes `uncertain`
  and goes to the Review Queue.
- Truncated OCR (`"Looking for exclusive ag..."`) is never completed. It becomes
  `uncertain` with the fragment quoted as-is.
- Comments only count when written by the owner (`author_is_owner=True`).
- A `manually_verified` evidence item with `verified_status` overrides all
  automated signals.
- Conflicts: a rejection together with a positive signal stays `rejects_agents`
  but is flagged for review. An appointed agent together with a request for one
  becomes `uncertain`. "Exclusive listing, co-broke welcome" becomes `already_has_exclusive_agent`.

## Scoring and alerts

| Status | Score effect | Immediate alerts |
| --- | --- | --- |
| seeking_exclusive_agent | `+seeking_exclusive_bonus` (15) | yes, with `[EXCLUSIVE OPPORTUNITY]` |
| open_to_agent_appointment | `+open_to_appointment_bonus` (5) | yes |
| already_has_exclusive_agent | `already_has_agent_penalty` (−20) | yes |
| rejects_agents | none | excluded unless `alert_on_rejects_agents` |
| no_evidence | none | yes |
| uncertain | none | held for review |

Bonuses are never applied to a listing that fails a hard gate: no eligible contact,
privacy block, scam risk ≥ `max_scam_risk_score`, outside a target location,
confirmed duplicate, or older than `max_listing_age_days`.

An exclusive-opportunity email is sent only when all of these hold:
`has_eligible_contact`, status `seeking_exclusive_agent`, confidence ≥ threshold,
target location, not a confirmed duplicate, within max age, and scam risk below the limit.

Example subject: `[EXCLUSIVE OPPORTUNITY][92/100] Direct Owner – Bangi RM650,000`.

The email body shows the status, confidence, supporting phrase, evidence source,
public phone/email, the original post link and the dashboard link.

## Pipeline

`rejects_agents` (and `uncertain` / review-required) listings cannot enter
To Contact unless a user records an override with `override_contact_block(listing, user, reason)`.
The dashboard shows the owner's instruction in red, for example
*Pemilik tidak mahu dihubungi oleh ejen: "Ejen jangan hubungi"*.

## Dashboard badges

Mencari Ejen Eksklusif (top priority) · Terbuka Melantik Ejen · Perlu Semakan ·
Tiada Bukti · Sudah Ada Ejen Eksklusif · Tidak Mahu Ejen. There is a filter for every status.
