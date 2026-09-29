"""What the business's own website says — the first signals that fetch.

Everything above this was arithmetic on a listing. These go and look, which
puts them in the CHEAP tier: one request per business, only for businesses that
survived the free filters. On a real run that is roughly a quarter of them.

Three things a listing cannot tell you, and each one changes a verdict:

  site_status    the listing shows a website; does it actually load? A domain
                 that 404s, times out, or now redirects to a registrar parking
                 page belongs to a business that HAD a site and lost it. That
                 is a better prospect than one that never had one, and by
                 listing data alone the two look identical.

  site_platform  what it is built on, read from the page rather than guessed
                 from the URL. A custom domain in front of Wix is still Wix.
                 The URL says `theirsalon.com`; the HTML says Wix.

  site_contact   an email on the page, which is what makes the lead actionable.

The listing-only `web_presence` signal stays exactly as it was. These refine
it; they do not replace it, because they cost a request and it does not.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from ..models import Business, Cost, Signal
from . import Context, signal

# Hosts a dead or expired domain typically lands on. A redirect to one of these
# is not a website -- it is the corpse of one, and it means the business is
# reachable by phone and nothing else.
PARKING = (
    "sedoparking.com", "parkingcrew.net", "bodis.com", "afternic.com",
    "dan.com", "hugedomains.com", "godaddy.com/forsale", "buydomains.com",
    "namecheap.com/domains/registration", "domainmarket.com", "undeveloped.com",
    "porkbun.com/market", "squadhelp.com", "brandbucket.com",
)

# Markers in the served HTML. Far more reliable than the URL: these strings are
# emitted by the platform itself and a custom domain does not hide them.
PLATFORM_MARKERS: List[Tuple[str, Tuple[str, ...]]] = [
    ("wix",         ("wix.com", "wixstatic.com", "X-Wix-", "wixsite")),
    ("squarespace", ("squarespace.com", "squarespace-cdn.com", "static1.squarespace")),
    ("shopify",     ("cdn.shopify.com", "shopify.com/s/", "Shopify.theme")),
    ("wordpress",   ("wp-content", "wp-includes", "wp-json")),
    ("godaddy",     ("godaddysites.com", "img1.wsimg.com", "GoDaddy Website Builder")),
    ("weebly",      ("weebly.com", "weeblysite.com", "editmysite.com")),
    ("webflow",     ("webflow.com", "assets.website-files.com")),
    ("duda",        ("dudamobile.com", "multiscreensite.com", "duda.co")),
    ("square",      ("square.site", "squarespace-cdn", "squareup.com")),
    ("facebook",    ("facebook.com/plugins", "connect.facebook.net")),
]

# Pages that exist but say nothing. A live 200 that is a placeholder is, for
# prospecting purposes, the same as no site -- and looks like a real one.
PLACEHOLDER = (
    "under construction", "coming soon", "site is being built",
    "default web page", "welcome to nginx", "apache2 ubuntu default",
    "this domain is parked", "future home of", "index of /",
    "account suspended", "website coming soon", "page not found",
)

EMAIL_RE = re.compile(
    r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", re.I)
# Addresses that belong to the site's toolchain rather than the business.
EMAIL_NOISE = ("example.com", "sentry.io", "wixpress.com", "godaddy.com",
               "squarespace.com", "@2x", "@sentry", "domain.com", "email.com",
               "yourdomain", "@media", "u003e")


def _target(biz: Business) -> str:
    return (biz.website or biz.booking_url or "").strip()


def _fetcher(ctx: Context, name: str):
    """The shared fetcher, unless the campaign supplied one (tests do)."""
    given = ctx.opt(name, "fetcher")
    if given is not None:
        return given
    from ..fetch import shared
    return shared()


def _get(ctx: Context, name: str, url: str):
    """Fetch, and charge what it cost to the run's request budget.

    `Context.note_request` existed and nothing called it, so `max_requests`
    counted the source's requests and never a single website fetch: a budget
    of 3 made 20 of them. A fetcher that does not report its cost is charged
    one request per answer that did not come from a cache.
    """
    resp = _fetcher(ctx, name).get(url)
    spent = getattr(resp, "requests", None)
    if spent is None:
        spent = 0 if getattr(resp, "from_cache", False) else 1
    if spent:
        ctx.note_request(int(spent))
    return resp


def _parked(url: str, text: str) -> Optional[str]:
    """The parking service this URL landed on, or a parked-page phrase.

    Hosts match on a DOT BOUNDARY, and an entry with a path matches only under
    that path. A plain substring test made `jordan.com`, `sheridan.com` and
    `aidan.com` all "parked" on `dan.com` -- a live business reported as having
    lost its website, which is the most persuasive lead this signal can make.
    """
    from ..packs import host_matches
    parts = urlsplit(url)
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    path = parts.path or "/"
    for p in PARKING:
        entry_host, _, entry_path = p.partition("/")
        if not host_matches(host, [entry_host]):
            continue
        if entry_path and not path.lstrip("/").startswith(entry_path):
            continue
        return p
    low = text[:4000].lower()
    for p in ("this domain may be for sale", "buy this domain",
              "domain is for sale", "parked free, courtesy"):
        if p in low:
            return p
    return None


@signal(name="site_status", cost=Cost.CHEAP, version=1,
        label="Website status",
        description="live / dead / parked / placeholder — does the site actually load?",
        kind="categorical",
        values=["live", "dead", "parked", "placeholder", "no_site", "unknown"],
        suggest={"op": "in", "value": ["dead", "parked", "placeholder", "no_site"]})
def site_status(biz: Business, ctx: Context) -> Signal:
    url = _target(biz)
    if not url:
        return Signal("site_status", "no_site", 1.0,
                      {"note": "the listing gives no website to check"})

    resp = _get(ctx, "site_status", url)

    if resp.error:
        # A fetch that failed is NOT evidence the site is dead -- our network
        # could be the broken one. Kept as a failed measurement so the run's
        # breaker sees it and the business is left unevaluated, never rejected.
        return Signal("site_status", "unknown", 0.0,
                      {"error": resp.error, "url": url})

    ev = {"url": url, "final_url": resp.final_url, "status": resp.status,
          "redirected": resp.redirected, "from_cache": resp.from_cache}

    parked = _parked(resp.final_url, resp.text)
    if parked:
        ev["matched"] = parked
        return Signal("site_status", "parked", 0.95,
                      dict(ev, note="the domain now points at a parking page -- "
                                    "they had a website and lost it"))

    if resp.status in (404, 410):
        return Signal("site_status", "dead", 1.0, dict(ev, note="page is gone"))
    if resp.status >= 500:
        return Signal("site_status", "unknown", 0.3,
                      dict(ev, note="server error; may be temporary"))
    if not resp.ok:
        return Signal("site_status", "dead", 0.8, ev)

    low = resp.text[:6000].lower()
    for marker in PLACEHOLDER:
        if marker in low:
            return Signal("site_status", "placeholder", 0.85,
                          dict(ev, matched=marker,
                               note="loads, but there is no site behind it"))

    # A page with almost no text is a placeholder that forgot to say so.
    stripped = re.sub(r"<[^>]+>", " ", resp.text)
    words = len(stripped.split())
    ev["words"] = words
    if words < 40:
        return Signal("site_status", "placeholder", 0.6,
                      dict(ev, note="almost no content on the page"))

    return Signal("site_status", "live", 1.0, ev)


@signal(name="site_platform", cost=Cost.CHEAP, version=1,
        label="Site platform",
        description="What the site is actually built on, read from the HTML",
        kind="categorical",
        values=["wix", "squarespace", "shopify", "wordpress", "godaddy", "weebly",
                "webflow", "duda", "square", "facebook", "custom", "unknown"])
def site_platform(biz: Business, ctx: Context) -> Signal:
    url = _target(biz)
    if not url:
        return Signal("site_platform", None, 1.0, {"note": "no website to inspect"})

    resp = _get(ctx, "site_platform", url)
    if resp.error:
        return Signal("site_platform", "unknown", 0.0, {"error": resp.error, "url": url})
    if not resp.ok:
        return Signal("site_platform", None, 0.5,
                      {"status": resp.status, "note": "nothing served to inspect"})

    hay = (resp.text[:200_000] + " " + " ".join(
        "%s: %s" % kv for kv in resp.headers.items())).lower()
    for name, markers in PLATFORM_MARKERS:
        for marker in markers:
            if marker.lower() in hay:
                return Signal("site_platform", name, 0.9,
                              {"matched": marker, "final_url": resp.final_url,
                               "note": "read from what the site served, so a "
                                       "custom domain does not hide it"})
    return Signal("site_platform", "custom", 0.6,
                  {"final_url": resp.final_url,
                   "note": "no known builder markers found"})


@signal(name="site_contact", cost=Cost.CHEAP, version=1,
        label="Contact on site",
        description="An email address published on the business's own site",
        kind="text")
def site_contact(biz: Business, ctx: Context) -> Signal:
    url = _target(biz)
    if not url:
        return Signal("site_contact", None, 1.0, {"note": "no website to read"})

    resp = _get(ctx, "site_contact", url)
    if resp.error:
        return Signal("site_contact", "unknown", 0.0, {"error": resp.error, "url": url})
    if not resp.ok:
        return Signal("site_contact", None, 0.5, {"status": resp.status})

    found: List[str] = []
    for match in EMAIL_RE.finditer(resp.text[:200_000]):
        email = match.group(0).strip(".").lower()
        if any(n in email for n in EMAIL_NOISE):
            continue
        if email not in found:
            found.append(email)
        if len(found) >= 5:
            break
    if not found:
        return Signal("site_contact", None, 0.9,
                      {"note": "no address published on the page fetched"})
    return Signal("site_contact", found[0], 0.9,
                  {"emails": found, "final_url": resp.final_url})
