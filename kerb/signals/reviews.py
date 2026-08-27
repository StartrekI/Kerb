"""Review volume, and whether the set we hold can be trusted.

Two signals, because they answer different questions and must not be conflated:

  reviews           how many the listing claims -- a proxy for establishment
  review_integrity  whether what we actually FETCHED is complete

The second exists because Google's feed truncates silently: HTTP 200, well
formed, just short. It stamped 17 businesses with the wrong first-review year
before it was caught. Anything derived from a truncated set is unsafe, so the
integrity signal is what stops a wrong number being scored as if it were right.
"""

from __future__ import annotations

from ..models import Business, Cost, ReviewIntegrity, Signal
from . import Context, signal

# The RPC's smallest real page. Getting exactly this many, when far more were
# claimed, is the truncation signature rather than a real count.
PAGE_SIZE = 5


@signal(name="reviews", cost=Cost.FREE, version=1,
        label="Review count", description="How many reviews the listing claims",
        kind="number", suggest={"op": ">=", "value": 30, "default_on": True})
def reviews(biz: Business, ctx: Context) -> Signal:
    n = biz.review_count
    if n is None:
        # Not zero. A record that never carried a count is unknown, and this
        # project's whole argument is that unknown must not quietly become a
        # number -- scored as 0 it is indistinguishable from a real business
        # with no reviews, and the rejection reason would state a count the
        # data never contained.
        return Signal("reviews", None, 0.0,
                      {"note": "no review count on the record -- unknown, not zero"})
    return Signal("reviews", int(n), 1.0, {"claimed": int(n)})


@signal(name="review_integrity", cost=Cost.FREE, version=1,
        label="Review integrity",
        description="complete / truncated / unavailable",
        kind="categorical", values=["complete", "truncated", "unavailable"])
def review_integrity(biz: Business, ctx: Context) -> Signal:
    claimed = biz.review_count
    fetched = biz.reviews_fetched

    if fetched is None:
        return Signal("review_integrity", ReviewIntegrity.UNAVAILABLE, 1.0,
                      {"note": "no reviews were fetched for this business"})
    if not claimed:
        return Signal("review_integrity", ReviewIntegrity.COMPLETE, 0.5,
                      {"fetched": fetched, "claimed": None})

    # Truncated when far more were claimed than arrived AND what arrived sits
    # at the page boundary. Both conditions matter: a business with 6 claimed
    # and 5 fetched is not evidence of anything.
    if claimed > PAGE_SIZE * 3 and fetched <= PAGE_SIZE:
        return Signal("review_integrity", ReviewIntegrity.TRUNCATED, 0.95,
                      {"claimed": claimed, "fetched": fetched,
                       "page_size": PAGE_SIZE,
                       "note": "stopped at the first page -- do not score on this"})
    return Signal("review_integrity", ReviewIntegrity.COMPLETE, 1.0,
                  {"claimed": claimed, "fetched": fetched})
