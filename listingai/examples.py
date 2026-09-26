"""Captions of the example listings that earlier versions loaded on first start.

ListingAI now starts empty. On start-up it removes any of these examples still
in the user's data; only listings with an example id (L1…, EX1…) and exactly
one of these captions are removed, so the user's own listings are never touched.
"""

from __future__ import annotations

import re

from .models import Listing

SAMPLE_CAPTIONS = frozenset({
    'Owner jual rumah teres. Nak lantik seorang ejen sahaja. WhatsApp 012-3456789',
    'Agents are welcome, boleh co-broke',
    'Exclusive listing. Contact appointed agent: Ahmad Faizal REN 31234',
    'Rumah teres 2 tingkat, direct owner, freehold',
    'Ejen jangan hubungi. Direct buyer only',
    'Looking for exclusive ag...',
    'Saya owner dan perlukan agent jualkan rumah',
    'Rumah semi-D Kajang. Perlukan ejen untuk uruskan jualan, owner sibuk kerja luar negeri',
    'Condo Cyberjaya fully furnished. No agent please, owner deal only',
    'Need exclusive agent? haha semua agent sama je',
    'Apartment Seri Kembangan, ejen dialu-alukan, komisen 2%',
    'Under exclusive agency. Sole agent: Nurul Huda REN 45120',
    'Owner jual terus, harga nego. Nak cari ejen eksklusif untuk handle semua viewing',
    'Rumah teres setingkat, owner sendiri. Agent boleh PM',
    'Owner jual rumah teres 2 tingkat Bukit Mertajam. Nak lantik seorang ejen sahaja. WhatsApp 012-3456789',
    'Rumah semi-D Sg Petani. Perlukan ejen untuk uruskan jualan, owner kerja di KL',
    'Condo Bayan Lepas dekat FTZ. Agents are welcome, boleh co-broke',
    'Rumah teres setingkat Alor Star, owner sendiri. Agent boleh PM',
    'Exclusive listing. Contact appointed agent: Ahmad Faizal REN 31234',
    'Rumah teres Kulim Hi-Tech, direct owner, freehold',
    'Ejen jangan hubungi. Direct buyer only',
    'Apartment Butterworth. Looking for exclusive ag...',
    'Homestay Kuah Langkawi. No agent please, owner deal only',
    'Saya owner dan perlukan agent jualkan rumah Seberang Jaya',
})


def is_example(listing: Listing) -> bool:
    return re.fullmatch(r"(?:L|EX)\d+", listing.id) is not None and listing.caption in SAMPLE_CAPTIONS
