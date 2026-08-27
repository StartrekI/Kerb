"""Signals about the shape of a business, computed from what is already held.

No requests, no keys, no latency. That makes these the best value in the
project and also the ones nobody asks for, because you cannot request a
measurement you do not know is derivable.

Each answers a question that changed a real decision:

  chain_size       is this an independent or one branch of forty? A freelancer
                   selling websites cannot sell to a chain, and a dataset full
                   of branches looks like a dataset full of leads.
  name_script      what writing system is the name in? Outreach written in
                   English to a business named in Cyrillic is wasted, and this
                   was being filtered by hand.
  review_velocity  reviews per year. Two hundred reviews over fifteen years is
                   a sleepy practice; two hundred over two is one that is
                   growing and spending.
  contactable      is there any way to reach them at all? A lead you cannot
                   contact is not a lead.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..models import Business, Cost, Signal
from . import Context, signal

# ---------------------------------------------------------------- chain size

# Suffixes and noise that make two branches of the same brand look different.
_CHAIN_NOISE = re.compile(
    r"\b(ltd|limited|llc|inc|plc|gmbh|bv|pty|llp|co|corp|company|group|"
    r"holdings|uk|usa|international)\b\.?", re.I)
_BRANCH_HINT = re.compile(
    r"\s[-–—|(]\s?[^-–—|()]{1,40}\)?$|\s(branch|store|clinic|centre|center)\s*\d*$",
    re.I)
_PUNCT = re.compile(r"[^\w\s]", re.U)


def chain_key(name: str) -> str:
    """A name reduced to what two branches of one brand would share.

    "Specsavers Opticians - Islington" and "Specsavers Opticians (Camden)"
    both reduce to "specsavers opticians". Deliberately conservative: over-
    merging would mark independents as a chain and delete real leads.
    """
    n = (name or "").strip().lower()
    n = _BRANCH_HINT.sub("", n)
    n = _CHAIN_NOISE.sub(" ", n)
    n = _PUNCT.sub(" ", n)
    n = unicodedata.normalize("NFKD", n)
    return " ".join(n.split())


def match_brand(key: str, known: Dict[str, int]) -> Optional[str]:
    """The known brand this name belongs to, matched on WHOLE WORDS.

    Listings append the category to the brand -- "Specsavers Opticians",
    "Specsavers Hearing Centre" -- so an exact key match misses almost every
    real row. A plain string prefix would be worse than useless: "boots" is a
    prefix of "bootsy s diner", and a false positive here deletes a genuine
    independent from the results.

    Matching whole leading words gets both right.
    """
    if not key or not known:
        return None
    words = key.split()
    # Longest brand first, so "boots opticians" wins over "boots".
    for n in range(min(len(words), 4), 0, -1):
        candidate = " ".join(words[:n])
        if candidate in known:
            return candidate
    return None


_KNOWN_CACHE: Dict[int, Dict[str, int]] = {}


def _known_chains(ctx: Context) -> Dict[str, int]:
    """Brands from the chains pack, keyed the same way names are.

    Cached per library instance: this runs once per business, and re-parsing a
    pack for every row of a 200,000-row file would make a free signal expensive.
    """
    seeded = ctx.opt("chain_size", "known")
    if seeded:
        return seeded
    ident = id(ctx.packs)
    if ident not in _KNOWN_CACHE:
        table: Dict[str, int] = {}
        pack = ctx.packs.maybe("chains/known")
        if pack:
            for entry in pack.get("brands") or []:
                if isinstance(entry, dict):
                    name, n = entry.get("name", ""), entry.get("locations", 99)
                else:
                    name, n = str(entry), 99
                key = chain_key(name)
                if key:
                    table[key] = int(n)
        _KNOWN_CACHE[ident] = table
    return _KNOWN_CACHE[ident]


@signal(name="chain_size", cost=Cost.FREE, version=1,
        label="Chain size",
        description="How many locations of this brand are in the dataset",
        kind="number", suggest={"op": "<=", "value": 3})
def chain_size(biz: Business, ctx: Context) -> Signal:
    key = chain_key(biz.name)
    if not key:
        return Signal("chain_size", None, 0.0, {"note": "no usable name"})

    # The pack is consulted FIRST and always, because recognising a brand from
    # a single row is the entire reason it exists. It used to be reachable only
    # when a dataset tally had been seeded -- which is exactly the case where
    # the tally already answers -- so on the streaming path a national chain
    # with one branch in the search area came back as an independent.
    known = _known_chains(ctx)
    brand = match_brand(key, known)
    if brand:
        return Signal("chain_size", int(known[brand]), 0.95,
                      {"key": key, "brand": brand, "source": "chains pack",
                       "note": "a brand recognisable from one row"})

    # Counted across the run, which needs the whole set, so the pipeline seeds
    # it where one exists. A single record cannot know it is one of forty.
    counts: Optional[Counter] = ctx.opt("chain_size", "counts")
    if counts is None:
        return Signal("chain_size", 1, 0.3,
                      {"key": key,
                       "note": "no dataset tally available; assumed independent"})
    n = int(counts.get(key, 1))
    return Signal("chain_size", n, 0.9 if n > 1 else 0.8,
                  {"key": key, "locations_in_dataset": n,
                   "note": "counted across this run only -- a chain with one "
                           "branch in the search area still looks independent"})


# --------------------------------------------------------------- name script

_SCRIPT_RANGES = [
    ("latin", "LATIN"), ("cyrillic", "CYRILLIC"), ("greek", "GREEK"),
    ("arabic", "ARABIC"), ("hebrew", "HEBREW"), ("devanagari", "DEVANAGARI"),
    ("han", "CJK"), ("hiragana", "HIRAGANA"), ("katakana", "KATAKANA"),
    ("hangul", "HANGUL"), ("thai", "THAI"), ("tamil", "TAMIL"),
    ("bengali", "BENGALI"), ("telugu", "TELUGU"),
]


def script_of(ch: str) -> Optional[str]:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    for label, marker in _SCRIPT_RANGES:
        if name.startswith(marker) or (" " + marker) in name:
            return label
    return None


@signal(name="name_script", cost=Cost.FREE, version=1,
        label="Name script",
        description="The dominant writing system of the business name",
        kind="categorical",
        values=["latin", "cyrillic", "greek", "arabic", "hebrew", "devanagari", "han",
                "hiragana", "katakana", "hangul", "thai", "tamil", "bengali", "telugu", "unknown"],
        suggest={"op": "in", "value": ["latin"]})
def name_script(biz: Business, ctx: Context) -> Signal:
    name = (biz.name or "").strip()
    letters = [c for c in name if c.isalpha()]
    if not letters:
        return Signal("name_script", "unknown", 0.0,
                      {"note": "the name has no letters to judge"})

    counts = Counter(s for s in (script_of(c) for c in letters) if s)
    if not counts:
        return Signal("name_script", "unknown", 0.0, {"name": name})

    dominant, n = counts.most_common(1)[0]
    share = n / len(letters)
    # Mixed names are ordinary -- "Café Größe 東京" -- so the share is reported
    # rather than forced to a single answer.
    return Signal("name_script", dominant, round(min(1.0, share), 2),
                  {"name": name, "share": round(share, 2),
                   "scripts": dict(counts),
                   "mixed": len(counts) > 1})


# ----------------------------------------------------------- review velocity

@signal(name="review_velocity", cost=Cost.FREE, version=1,
        label="Review velocity",
        description="Reviews per year -- growing, or established and quiet?",
        kind="number")
def review_velocity(biz: Business, ctx: Context) -> Signal:
    count = biz.review_count
    year = biz.first_review_year
    if count is None or not year:
        return Signal("review_velocity", None, 0.0,
                      {"note": "needs both a review count and a first-review year"})

    years = max(1.0, datetime.now(timezone.utc).year - int(year) + 1)
    rate = round(int(count) / years, 2)
    return Signal("review_velocity", rate, 0.7,
                  {"reviews": int(count), "first_review_year": int(year),
                   "years": years,
                   # Inherits establishment_age's proxy, so it inherits its
                   # caveat too. Stating the rate without this would present a
                   # guess as a measurement.
                   "caveat": "first-review year is a proxy for listing age; it "
                             "understates a listing that sat unreviewed"})


# -------------------------------------------------------------- rating band

# Star thresholds, applied top-down. Override per campaign with
# signal_options.rating_band.bands.
RATING_BANDS = (("excellent", 4.5), ("good", 4.0), ("mixed", 3.0), ("poor", 0.0))
# Below this many reviews a rating is one bad afternoon, not a reputation.
RATING_MIN_REVIEWS = 5
# The scale everything here assumes. A source reporting out of 10 would make
# every business "excellent", so it is detected rather than silently banded.
RATING_MAX = 5.0


@signal(name="rating_band", cost=Cost.FREE, version=1,
        label="Rating band",
        description="excellent / good / mixed / poor -- with volume taken into account",
        kind="categorical", values=["excellent", "good", "mixed", "poor", "unrated", "unknown"])
def rating_band(biz: Business, ctx: Context) -> Signal:
    """Which end of the market this is, as a category rather than a number.

    Both ends are worth targeting and which one depends on the pitch: a poorly
    rated business needs help, a well rated one can pay for it. As a raw number
    that is awkward to express; as a band it is a categorical weight.

    The part that matters is volume. A 5.0 from two reviews and a 4.6 from four
    hundred are not the same claim, so review count sets the confidence -- and
    below a handful of reviews the band is withheld entirely rather than
    asserted from noise.
    """
    rating = biz.rating
    if rating is None:
        return Signal("rating_band", "unrated", 1.0,
                      {"note": "no rating on the listing"})

    try:
        value = float(rating)
    except (TypeError, ValueError):
        return Signal("rating_band", "unknown", 0.0,
                      {"error": "rating is not a number: %r" % rating})

    ceiling = float(ctx.opt("rating_band", "scale", RATING_MAX) or RATING_MAX)
    if value < 0 or value > ceiling:
        # Refused rather than banded. Silently treating a 10-point rating as a
        # 5-point one would mark every business excellent.
        return Signal("rating_band", "unknown", 0.0,
                      {"error": "rating %s is outside the 0-%g scale; set "
                                "signal_options.rating_band.scale if the source "
                                "uses another" % (value, ceiling)})

    # `or 0` conflated two different facts: a business with no reviews, and a
    # SOURCE that does not report review counts at all. The second was being
    # told "too few reviews for the rating to mean anything" about a 4.8 from a
    # collector that simply never supplies the number -- a confident statement
    # about something never measured.
    reviews = biz.review_count
    floor = int(ctx.opt("rating_band", "min_reviews", RATING_MIN_REVIEWS)
                or RATING_MIN_REVIEWS)
    if reviews is not None and reviews < floor:
        return Signal("rating_band", "unrated", 0.4,
                      {"rating": value, "reviews": reviews, "min_reviews": floor,
                       "note": "too few reviews for the rating to mean anything"})

    bands = ctx.opt("rating_band", "bands") or RATING_BANDS
    if isinstance(bands, dict):
        bands = sorted(bands.items(), key=lambda kv: -float(kv[1]))
    label = "poor"
    for name, threshold in bands:
        if value >= float(threshold):
            label = name
            break

    if reviews is None:
        # The band is still real -- 4.8 stars is 4.8 stars -- but its weight is
        # unknown, and confidence is exactly where that belongs. Reporting the
        # band at low confidence keeps the filter usable and keeps the record
        # honest about what was and was not measured.
        return Signal("rating_band", label, 0.35,
                      {"rating": value, "reviews": None,
                       "note": "this source reports no review count, so the "
                               "rating's weight is unknown -- band given at low "
                               "confidence"})

    # More reviews, more confidence, levelling off -- the same saturating shape
    # the review weighting uses, for the same reason.
    confidence = min(1.0, 0.5 + 0.5 * math.log10(reviews + 1) / math.log10(101))
    return Signal("rating_band", label, round(confidence, 2),
                  {"rating": value, "reviews": reviews,
                   "note": "confidence follows review volume: a 5.0 from two "
                           "reviews is not a 4.6 from four hundred"})


# --------------------------------------------------------------- contactable

@signal(name="contactable", cost=Cost.FREE, version=1,
        label="Contactable",
        description="Is there any way to reach this business at all?",
        kind="boolean", suggest={"op": "==", "value": True})
def contactable(biz: Business, ctx: Context) -> Signal:
    ways: List[str] = []
    if (biz.phone or "").strip():
        ways.append("phone")
    if (biz.website or "").strip():
        ways.append("website")
    if (biz.booking_url or "").strip():
        ways.append("booking")
    email = (biz.extras or {}).get("email")
    if email:
        ways.append("email")
    return Signal("contactable", bool(ways), 1.0,
                  {"ways": ways} if ways else
                  {"note": "no phone, site or booking link -- nothing to act on"})
