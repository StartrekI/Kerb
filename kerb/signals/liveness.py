"""Is the business actually operating?

The trap this avoids: Google writes "Closed - Opens 9 am Tue" on a profile that
is merely shut RIGHT NOW. Reading that as closed-down throws away a perfectly
good prospect. Only the explicit permanent/temporary wording disqualifies.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import Business, Cost, Liveness, Signal
from . import Context, signal

PERM = ("permanently closed",)
TEMP = ("temporarily closed",)


@signal(name="liveness", cost=Cost.FREE, version=1,
        label="Liveness",
        description="open / temp_closed / perm_closed / stale",
        kind="categorical", values=["open", "temp_closed", "perm_closed", "stale", "unknown"],
        suggest={"op": "==", "value": "open", "default_on": True})
def liveness(biz: Business, ctx: Context) -> Signal:
    raw = (biz.status_raw or "").strip()
    low = raw.lower()

    for marker in PERM:
        if marker in low:
            return Signal("liveness", Liveness.PERM_CLOSED, 1.0, {"status": raw})
    for marker in TEMP:
        if marker in low:
            return Signal("liveness", Liveness.TEMP_CLOSED, 1.0, {"status": raw})

    # Stale: listed long ago and essentially nothing happened since.
    #
    # The condition used to be `year and not biz.review_count`, which could
    # only fire on self-contradictory data -- a record claiming a first-review
    # year while also claiming zero reviews. For every consistent record the
    # branch was unreachable, so a state the signal advertises could never be
    # returned. It now fires on the case it was meant to describe: an old
    # listing that never accumulated activity.
    #
    # What this deliberately cannot see: a business that was busy and recently
    # went quiet. No listing carries a last-activity date, and inferring one
    # from a first-review year would be a guess presented as a measurement.
    months = int(ctx.opt("liveness", "stale_after_months", 24) or 24)
    max_reviews = int(ctx.opt("liveness", "stale_max_reviews", 3) or 3)
    year = biz.first_review_year
    if year:
        age_years = datetime.now(timezone.utc).year - int(year)
        if (biz.review_count or 0) <= max_reviews and age_years * 12 >= months:
            return Signal("liveness", Liveness.STALE, 0.5,
                          {"first_review_year": int(year),
                           "reviews": biz.review_count or 0,
                           "threshold_months": months,
                           "max_reviews": max_reviews,
                           "caveat": "detects a listing that never grew; cannot see "
                                     "a busy business that recently went quiet"})

    if not raw:
        return Signal("liveness", Liveness.OPEN, 0.6,
                      {"note": "no status field; assumed open"})
    return Signal("liveness", Liveness.OPEN, 1.0, {"status": raw})
