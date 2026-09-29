"""Signals tested against REAL production failures.

Every case below is a business that actually appeared in a run and was either
wrongly kept or wrongly rejected before the rule existed. They are the
regression suite for the accumulated corrections that make this project worth
anything -- if these pass, the port from the original pipeline is faithful.

    ../scarrper/.venv/bin/python tests/test_signals.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerb import signals                                    # noqa: E402
from kerb.models import Business, ReviewIntegrity, WebPresence   # noqa: E402
from kerb.signals import Context                            # noqa: E402


def biz(**kw):
    kw.setdefault("cid", "0x0:0x1")
    return Business(**kw)


def val(name, business, **opts):
    ctx = Context(options=opts)
    return signals.compute(name, business, ctx).value


def sig(name, business, **opts):
    return signals.compute(name, business, Context(options=opts))


# ---------------------------------------------------------------- web presence

def test_web_presence():
    W = WebPresence

    assert val("web_presence", biz()) == W.NONE.value
    assert val("web_presence", biz(website="https://theirsalon.com")) == W.OWNED_DOMAIN.value

    # The square trap: two Square products, opposite meanings. Getting this
    # wrong disqualified a real business (hannah-heller.square.site).
    assert val("web_presence", biz(website="https://squareup.com/a/x")) == W.BOOKING_ONLY.value
    assert val("web_presence", biz(website="https://hannah-heller.square.site")) == W.BUILDER.value

    assert val("web_presence", biz(website="https://vagaro.com/x")) == W.BOOKING_ONLY.value
    assert val("web_presence", biz(website="https://facebook.com/x")) == W.SOCIAL_ONLY.value

    # Dot-boundary matching: a naive substring test disqualifies this business.
    assert val("web_presence", biz(website="https://notfresha.com")) == W.OWNED_DOMAIN.value
    # ...but a real per-business subdomain must still match.
    assert val("web_presence", biz(website="https://salon.booksy.com")) == W.BOOKING_ONLY.value

    # Found by the collision rule in production, all previously scored as
    # owned domains and wrongly disqualifying their businesses.
    for host in ("mynewbooking.com", "go.bychronos.com", "blismo.com",
                 "getsquire.com", "book.thecut.co", "barberly.app"):
        assert val("web_presence", biz(website="https://%s/x" % host)) == W.BOOKING_ONLY.value, host

    # Booking link in the BOOKING field with no website = NONE, not
    # booking_only -- there is no website-field link to show as their presence.
    assert val("web_presence", biz(booking_url="https://vagaro.com/x")) == W.NONE.value
    # But their OWN domain in that field is still their site ("actionblack.us").
    assert val("web_presence", biz(booking_url="https://actionblack.us")) == W.OWNED_DOMAIN.value

    # Evidence must name what matched, or the verdict cannot be defended.
    e = sig("web_presence", biz(website="https://vagaro.com/x")).evidence
    assert e["matched"] == "vagaro.com" and "booking-hosts" in e["pack"]
    print("  web_presence            ok")


# ----------------------------------------------------------------- trade match

def test_trade_match_real_failures():
    """Each of these shipped as a qualified lead before the veto list existed."""
    roofing = {"trade_match": {"pack": "trades/roofing"}}
    painting = {"trade_match": {"pack": "trades/painting"}}

    wrong_for_roofing = [
        ("ZouZou Turkish & Lebanese Restaurant", "Turkish restaurant"),
        ("Hills View - Dubai Hills Estate", "Gated community"),
        ("Red Roof Building Karama", "Apartment building"),
        ("ALMAROOF PERFORMANCE jetski", "Water skiing service"),
        ("Roof Auto", "Car repair and maintenance service"),
        ("JBR - Jumeirah Beach Residence", "Community center"),
        ("Why cafe roof top Al Qana", "Surveyor"),
        ("Prestige Paving Malibu", "Paving contractor"),
        ("LaJolla Garage Door Repair", "Garage door supplier"),
        ("Dryforce Water Damage & Roofing", "Water damage restoration service"),
    ]
    for name, category in wrong_for_roofing:
        assert val("trade_match", biz(name=name, category=category), **roofing) is False, name

    wrong_for_painting = [
        ("THUKIRA TANJORE PAINTING ART GALLERY", "Art gallery"),
        ("InkPaint tattoo Studio", "Tattoo shop"),
        ("SUSIL TOOLS & PAINTS", "Paintings store"),
        ("Giri Paints", "Paint store"),
        ("Rita Decorators - Painting Decorator", "Gypsum product supplier"),
        ("Quick Service - Plumbing, Painting", "Plumber"),
        ("Yaalmozhi Painting & Cleaning", "House cleaning service"),
        ("THADAM finearts academy painting classes", "College"),
    ]
    for name, category in wrong_for_painting:
        assert val("trade_match", biz(name=name, category=category), **painting) is False, name

    # And the genuine articles must still pass.
    assert val("trade_match", biz(name="Tony the Roofer",
                                  category="Roofing contractor"), **roofing) == "roofing"
    assert val("trade_match", biz(name="Sri Bindhu Painting Works",
                                  category="Painting"), **painting) == "painting"

    # Name fallback only when the category is generic -- this is what keeps
    # "Red Roof Building" out while letting a real roofer with a vague
    # category in.
    assert val("trade_match", biz(name="Greene Construction Roofing",
                                  category="Contractor"), **roofing) == "roofing"
    got = sig("trade_match", biz(name="Greene Construction Roofing",
                                 category="Contractor"), **roofing)
    assert got.confidence < 0.95, "name fallback must be less confident than a category match"

    # Non-English trade terms, via locale_terms.
    assert val("trade_match", biz(name="Van Est Dakwerken",
                                  category="Contractor"), **roofing) == "roofing"
    print("  trade_match             ok  (%d real failures blocked)"
          % (len(wrong_for_roofing) + len(wrong_for_painting)))


# -------------------------------------------------------------------- liveness

def test_liveness():
    assert val("liveness", biz(status_raw="Permanently closed")) == "perm_closed"
    assert val("liveness", biz(status_raw="Temporarily closed")) == "temp_closed"
    # The trap: shut right now is not shut down.
    assert val("liveness", biz(status_raw="Closed - Opens 9 am Tue")) == "open"
    assert val("liveness", biz(status_raw="Open 24 hours")) == "open"
    assert val("liveness", biz()) == "open"
    print("  liveness                ok")


# ------------------------------------------------------------------- integrity

def test_review_integrity():
    I = ReviewIntegrity
    # The real signature: 90 claimed, 5 arrived, no error raised anywhere.
    assert val("review_integrity", biz(review_count=90, reviews_fetched=5)) == I.TRUNCATED.value
    assert val("review_integrity", biz(review_count=90, reviews_fetched=90)) == I.COMPLETE.value
    # Small counts are not evidence of truncation.
    assert val("review_integrity", biz(review_count=6, reviews_fetched=5)) == I.COMPLETE.value
    assert val("review_integrity", biz(review_count=90)) == I.UNAVAILABLE.value
    print("  review_integrity        ok")


def test_establishment_age():
    s = sig("establishment_age", biz(first_review_year=2023))
    assert s.value == 2023
    assert "caveat" in s.evidence, "a proxy must always carry its caveat"
    assert sig("establishment_age", biz()).confidence == 0.0
    print("  establishment_age       ok")


# ------------------------------------------------- bugs found in the audit

def test_stale_is_actually_reachable():
    """The STALE branch used to be dead code.

    Its condition was `year and not biz.review_count` -- a record claiming a
    first-review year AND zero reviews, which is self-contradictory. For every
    consistent record the branch could never fire, so `liveness` advertised a
    state it was incapable of returning.
    """
    assert val("liveness", biz(first_review_year=2005, review_count=1)) == "stale"
    assert val("liveness", biz(first_review_year=2005, review_count=0)) == "stale"

    # An old listing that DID grow is not stale -- it is a busy business.
    assert val("liveness", biz(first_review_year=2005, review_count=40)) == "open"
    # Nor is a young one.
    assert val("liveness", biz(first_review_year=2025, review_count=1)) == "open"

    # Both thresholds are tunable from the campaign.
    assert val("liveness", biz(first_review_year=2005, review_count=10),
               liveness={"stale_max_reviews": 20}) == "stale"
    assert val("liveness", biz(first_review_year=2005, review_count=1),
               liveness={"stale_after_months": 9999}) == "open"
    print("  liveness stale reachable ok")


def test_unknown_review_count_is_not_zero():
    """`reviews` returned 0 for a record that never carried a count.

    Scored as 0 it is indistinguishable from a real business with no reviews,
    and the rejection reason quoted a number the data never contained.
    """
    s = sig("reviews", biz())
    assert s.value is None, "unknown must stay unknown, not become 0"
    assert s.confidence == 0.0

    # A genuine zero is still a genuine zero.
    z = sig("reviews", biz(review_count=0))
    assert z.value == 0 and z.confidence == 1.0
    print("  reviews unknown != zero  ok")


def test_contact_links_are_not_a_website():
    """`mailto:hi@acme.com` parsed down to `acme.com` and was reported as the
    business owning a website -- dropping a real prospect."""
    for url in ("mailto:hi@acme.com", "tel:+441234567", "sms:+441234567"):
        assert val("web_presence", biz(website=url)) == WebPresence.NONE.value, url
    # Real sites are unaffected.
    assert val("web_presence", biz(website="https://acme.com")) == WebPresence.OWNED_DOMAIN.value
    assert val("web_presence", biz(website="https://vagaro.com/x")) == WebPresence.BOOKING_ONLY.value
    print("  contact links != website ok")


def test_a_broken_signal_never_kills_a_run():
    """One bad business must not cost the other 15,000 results."""
    bad = biz(name="x")
    bad.category = 12345                       # wrong type on purpose
    s = signals.compute("trade_match", bad, Context(
        options={"trade_match": {"pack": "trades/roofing"}}))
    assert s.confidence == 0.0 and "error" in s.evidence
    print("  error containment       ok")


def test_a_painting_class_is_not_a_painter():
    """The cafe fix defers any shared veto that overlaps the trade's own terms.
    "painting" overlaps "painting class", so the veto meant FOR this pack was
    dropped and an art class scored as a painting contractor at 0.95."""
    painting = {"trade_match": {"pack": "trades/painting"}}
    got = sig("trade_match", biz(name="Kids Art Hub", category="Painting class"),
              **painting)
    assert got.value is False and got.evidence["reason"] == "vetoed category", got.evidence
    assert val("trade_match", biz(name="Ace Decorators", category="Painter"),
               **painting) == "painting"
    print("  painting class vetoed    ok")


def test_parking_hosts_match_on_a_boundary():
    """`dan.com` in host made jordan.com, sheridan.com and aidan.com "parked":
    a live business reported as having lost its website."""
    from kerb.signals.site import _parked
    for live in ("https://jordan.com/", "https://sheridan.com/", "https://aidan.com/",
                 "https://mybodis.com/", "https://godaddy.com/"):
        assert _parked(live, "<html>fine</html>") is None, live
    for parked in ("https://dan.com/buy-domain/x", "https://www.sedoparking.com/x",
                   "https://ww12.bodis.com/", "https://www.godaddy.com/forsale/x.com"):
        assert _parked(parked, ""), parked
    assert _parked("https://example.com/", "This domain may be for sale!")
    print("  parking hosts bounded    ok")


def test_review_counts_are_read_not_guessed():
    """Stripping every dot read a rating, "(4.8)", as 48 reviews; "1.2K" was
    never read at all."""
    from kerb.signals.detail import _count
    assert _count("Rated 4.8 (4.8) stars") is None
    assert _count("4.5 (1,234)") == 1234
    assert _count("1.2K reviews") == 1200
    assert _count("4.6 (2.345)") == 2345
    assert _count("12 reviews") == 12
    print("  review counts parsed     ok")


def test_places_api_closure_statuses():
    """CLOSED_PERMANENTLY, as the Places API writes it, read as OPEN."""
    assert val("liveness", biz(status_raw="CLOSED_PERMANENTLY")) == "perm_closed"
    assert val("liveness", biz(status_raw="CLOSED_TEMPORARILY")) == "temp_closed"
    assert val("liveness", biz(status_raw="OPERATIONAL")) == "open"
    print("  places api statuses      ok")


def test_zero_is_a_threshold_not_unset():
    """`or 24` turned a deliberate 0 into the default."""
    assert val("liveness", biz(first_review_year=2025, review_count=0),
               liveness={"stale_after_months": 0}) == "stale"
    assert val("rating_band", biz(rating=4.9, review_count=1),
               rating_band={"min_reviews": 0}) == "excellent"
    print("  zero thresholds honoured ok")


def test_lower_is_better_ranks_the_right_way():
    """The UI ranked chain_size with higher-is-better, so a 3-branch chain
    outscored an independent. The registry now says how each number ranks."""
    from kerb import scoring
    spec = dict(signals.get("chain_size").rank, weight=40)
    assert spec["invert"] is True
    assert scoring._points(1, spec) > scoring._points(3, spec) > scoring._points(10, spec)
    assert scoring._points(None, spec) == 0.0, "unknown must not earn points"
    assert signals.get("establishment_age").rank is False, "a year is not a magnitude"
    assert signals.get("reviews").rank["cap"] == 300
    assert scoring.check_weight("x", {"weight": 1, "cap": 5, "invert": "yes"})
    assert scoring.check_weight("x", {"weight": 1, "cap": 5, "capp": 6})
    print("  rank hints and invert    ok")


if __name__ == "__main__":
    print("signals — regression against real production failures\n")
    test_web_presence()
    test_trade_match_real_failures()
    test_liveness()
    test_review_integrity()
    test_establishment_age()
    test_stale_is_actually_reachable()
    test_unknown_review_count_is_not_zero()
    test_contact_links_are_not_a_website()
    test_a_broken_signal_never_kills_a_run()
    test_a_painting_class_is_not_a_painter()
    test_parking_hosts_match_on_a_boundary()
    test_review_counts_are_read_not_guessed()
    test_places_api_closure_statuses()
    test_zero_is_a_threshold_not_unset()
    test_lower_is_better_ranks_the_right_way()
    print("\nall signal checks passed")
