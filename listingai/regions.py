"""Malaysian place names, regional presets and place matching."""

from __future__ import annotations

import re

REGIONS: dict[str, list[str]] = {
    "Penang Island": [
        "George Town", "Jelutong", "Air Itam", "Paya Terubong", "Farlim", "Tanjung Tokong", "Tanjung Bungah",
        "Batu Ferringhi", "Pulau Tikus", "Gelugor", "Minden", "Batu Uban", "Sungai Dua", "Bayan Baru",
        "Bayan Lepas", "Sungai Ara", "Relau", "Batu Maung", "Teluk Kumbar", "Balik Pulau", "Seri Tanjung Pinang",
        "Green Lane", "Island Glades", "Queensbay", "Sungai Nibong",
    ],
    "Seberang Perai (Penang mainland)": [
        "Butterworth", "Bagan Ajam", "Perai", "Seberang Jaya", "Bukit Mertajam", "Juru", "Alma", "Bukit Tengah",
        "Machang Bubok", "Permatang Pauh", "Simpang Ampat", "Batu Kawan", "Nibong Tebal", "Sungai Bakap",
        "Sungai Jawi", "Kepala Batas", "Bertam", "Tasek Gelugor", "Penaga", "Kubang Semang", "Berapit",
        "Taman Impian", "Seberang Perai",
    ],
    "Kedah": [
        "Alor Setar", "Kota Setar", "Anak Bukit", "Kuala Kedah", "Jitra", "Kubang Pasu", "Changlun",
        "Bukit Kayu Hitam", "Pokok Sena", "Pendang", "Yan", "Gurun", "Bedong", "Merbok", "Sungai Petani",
        "Bakar Arang", "Tikam Batu", "Kuala Muda", "Kuala Ketil", "Kulim", "Lunas", "Padang Serai",
        "Bandar Baharu", "Baling", "Sik", "Langkawi", "Kuah", "Bandar Puteri Jaya", "Taman Ria Jaya",
    ],
    "Perlis": ["Kangar", "Arau", "Padang Besar", "Kuala Perlis", "Simpang Empat"],
    "North Perak": ["Taiping", "Kamunting", "Parit Buntar", "Bagan Serai", "Kuala Kurau", "Selama"],
    "Klang Valley": [
        "Bandar Baru Bangi", "Bangi", "Kajang", "Semenyih", "Cyberjaya", "Putrajaya", "Seri Kembangan", "Serdang",
        "Puchong", "Shah Alam", "Klang", "Subang Jaya", "Petaling Jaya", "Damansara", "Kota Damansara", "Mont Kiara",
        "Cheras", "Ampang", "Setapak", "Wangsa Maju", "Gombak", "Rawang", "Selayang", "Kepong", "Sungai Buloh",
        "Bukit Jalil", "Sri Petaling", "Kuala Lumpur", "Sepang", "Dengkil", "Kota Kemuning", "Bukit Jelutong",
        "Setia Alam", "Balakong", "Bandar Mahkota Cheras", "Hulu Langat",
    ],
    "Other states": [
        "Ipoh", "Seremban", "Nilai", "Melaka", "Johor Bahru", "Iskandar Puteri", "Skudai", "Kulai", "Pasir Gudang",
        "Kota Bharu", "Kuala Terengganu", "Kuantan", "Kota Kinabalu", "Kuching", "Miri",
    ],
}

# Northern regions offered as one-click campaign presets.
PRESETS = ["Penang Island", "Seberang Perai (Penang mainland)", "Kedah", "Perlis", "North Perak"]

# Matched only when no town is named.
STATES = ["Pulau Pinang", "Penang", "Kedah", "Perlis", "Perak", "Selangor", "Johor", "Kelantan", "Terengganu",
          "Pahang", "Negeri Sembilan", "Sabah", "Sarawak"]

# Other spellings owners use for a place.
ALIASES: dict[str, list[str]] = {
    "Alor Setar": ["Alor Star"],
    "Perai": ["Prai"],
    "George Town": ["Georgetown", "GTown"],
    "Pulau Pinang": ["P. Pinang", "P Pinang"],
    "Seberang Perai": ["SPU", "SPT", "SPS", "Seberang Prai"],
    "Seberang Jaya": ["Sbg Jaya"],
}

# Common abbreviations inside place names: "Sg Petani", "Bkt Mertajam", "Tmn Ria Jaya".
_ABBR = {
    "sungai": r"(?:sungai|sg\.?)",
    "bukit": r"(?:bukit|bkt\.?)",
    "taman": r"(?:taman|tmn\.?)",
    "bandar": r"(?:bandar|bdr\.?)",
    "kampung": r"(?:kampung|kampong|kg\.?)",
    "tanjung": r"(?:tanjung|tg\.?|tanjong)",
    "air": r"(?:air|ayer)",
    "seri": r"(?:seri|sri)",
    "simpang": r"(?:simpang|spg\.?)",
}


def _one(name: str) -> str:
    parts = [_ABBR.get(w.lower(), re.escape(w)) for w in name.split()]
    return r"[\s-]*".join(parts)


def place_regex(place: str) -> re.Pattern:
    """Whole-word, case-insensitive pattern for a place, its abbreviations and aliases."""
    names = [place] + ALIASES.get(place, [])
    alts = "|".join(_one(n) for n in names if n.strip())
    return re.compile(rf"(?<![\w])(?:{alts})(?![\w])", re.IGNORECASE)


_CACHE: dict[str, re.Pattern] = {}


def mentions(text: str, place: str) -> bool:
    place = " ".join((place or "").split())
    if not place:
        return False
    if place not in _CACHE:
        _CACHE[place] = place_regex(place)
    return _CACHE[place].search(text or "") is not None


def known_places() -> list[str]:
    seen, out = set(), []
    for places in REGIONS.values():
        for p in places:
            if p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
    return sorted(out, key=len, reverse=True)
