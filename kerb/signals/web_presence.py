"""What the website slot on a listing actually means.

The single most valuable signal in the project, and the one competitors do not
have: they return a URL string, we return what it implies about the business.

Five states, because the truth has five. Collapsing them to has/hasn't -- which
is what filtering a scraper export on `website IS NULL` does -- silently
discards every business renting a booking page, and in the salon and barber
trades that is a large share of the market.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..models import Business, Cost, Signal, WebPresence
from ..packs import host_matches
from . import Context, signal

BOOKING = "web-presence/booking-hosts"
SOCIAL = "web-presence/social-hosts"
BUILDER = "web-presence/site-builders"


# An email or phone link is contact detail, not a web presence. Without this
# check `mailto:hi@acme.com` parsed down to `acme.com` and was reported as the
# business owning a website -- the exact false negative this signal exists to
# prevent, since it would drop a real prospect.
NON_WEB_SCHEMES = frozenset({"mailto", "tel", "sms", "fax", "callto", "skype"})


def hostname(url: str) -> str:
    """Bare host, lowercased, www stripped, port and credentials removed.

    Returns "" for anything that is not a web address.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if urlsplit(raw).scheme.lower() in NON_WEB_SCHEMES:
        return ""
    netloc = urlsplit(raw).netloc or urlsplit("//" + raw).netloc
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _classify(host: str, ctx: Context):
    """(state, matched_entry, pack_ref) for one host."""
    for pack_id, state in ((BOOKING, WebPresence.BOOKING_ONLY),
                           (SOCIAL, WebPresence.SOCIAL_ONLY),
                           (BUILDER, WebPresence.BUILDER)):
        pack = ctx.packs.maybe(pack_id)
        if not pack:
            continue
        hit = host_matches(host, pack.list("hosts"))
        if hit:
            return state, hit, pack.ref
    return WebPresence.OWNED_DOMAIN, None, None


@signal(name="web_presence", cost=Cost.FREE, version=1,
        label="Web presence",
        description="none / social_only / booking_only / builder / owned_domain",
        kind="categorical",
        values=["none", "social_only", "booking_only", "builder", "owned_domain", "unknown"],
        suggest={"op": "in", "value": ["none", "booking_only", "social_only"], "default_on": True})
def web_presence(biz: Business, ctx: Context) -> Signal:
    website = (biz.website or "").strip()
    booking = (biz.booking_url or "").strip()

    if website:
        host = hostname(website)
        if host and "." in host:
            state, hit, ref = _classify(host, ctx)
            ev = {"field": "website", "host": host}
            if hit:
                ev.update(matched=hit, pack=ref)
            return Signal("web_presence", state, 1.0, ev)

    # Google sometimes carries the business's OWN domain in the Book Online
    # action with the Website field left empty -- "actionblack.us" surfaced
    # only there. That domain is their real site regardless of which button
    # Maps hung it on, so the booking field is checked too.
    if booking:
        host = hostname(booking)
        if host and "." in host:
            state, hit, ref = _classify(host, ctx)
            if state is WebPresence.BOOKING_ONLY:
                # A third-party booking link in the booking field is the normal
                # case and means there is NO website -- not "booking_only",
                # because there was no Website-field link to show as their
                # presence. The asymmetry is deliberate.
                return Signal("web_presence", WebPresence.NONE, 1.0,
                              {"field": "booking_url", "host": host,
                               "matched": hit, "pack": ref,
                               "note": "third-party booking link only"})
            ev = {"field": "booking_url", "host": host}
            if hit:
                ev.update(matched=hit, pack=ref)
            return Signal("web_presence", state, 0.9, ev)

    return Signal("web_presence", WebPresence.NONE, 1.0, {})
