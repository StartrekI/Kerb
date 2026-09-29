"""How long the business has plausibly been listed.

No platform publishes a listing-created date -- not on the profile, not in the
DOM, not in any API field. The oldest review is the closest available proxy.

It is wrong in two directions and the evidence says so every time, because a
proxy presented as a fact is how bad data spreads: a 2019 listing that sat
unreviewed until 2025 looks new, and a genuinely new listing with no reviews
is invisible.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import Business, Cost, Signal
from . import Context, signal


# The value is a YEAR, so it filters ("listed since 2020") but does not rank:
# weighted as a magnitude, every business with any year scored full marks.
@signal(name="establishment_age", cost=Cost.FREE, version=1,
        label="First review year",
        description="Oldest-review year, a proxy for how long it has been listed",
        kind="number", rank=False)
def establishment_age(biz: Business, ctx: Context) -> Signal:
    year = biz.first_review_year
    if not year:
        return Signal("establishment_age", None, 0.0,
                      {"note": "no dated reviews -- age cannot be inferred"})

    now = datetime.now(timezone.utc).year
    years = max(0, now - int(year))
    return Signal("establishment_age", int(year), 0.7,
                  {"first_review_year": int(year),
                   "approx_age_years": years,
                   "proxy": "oldest review date",
                   "caveat": "understates a listing that sat unreviewed; "
                             "invisible for a listing with no reviews"})
