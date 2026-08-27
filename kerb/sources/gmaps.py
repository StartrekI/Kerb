"""Google Maps, collected by Kerb itself.

No API key, no browser, no driver, no third-party scraper. It asks the same
endpoint the Maps web UI asks and reads the answer, using httpx -- which Kerb
already depends on for website checks -- and the standard library.

WHY NO BROWSER
--------------
The obvious build is a headless Chrome driven by a driver, which is what the
predecessor did. Its worst failure came from exactly that: every crashed run
left a chromedriver and its Chrome children behind, and enough of them filled a
disk. A request has nothing to leak. It is also perhaps fifty times faster,
because nothing renders.

WHAT IS FRAGILE HERE, SAID PLAINLY
----------------------------------
The endpoint is internal and undocumented. The response is a deeply nested
array whose field positions Google may move without notice. That is a real risk
and this module answers it in one way: **it fails loudly.** If the shape stops
matching, `parse` raises `ShapeChanged` naming what it could not find, and the
run reports it. It never returns an empty list that looks like "no businesses
here" -- a collector that silently returns nothing is worse than one that stops,
because the first is indistinguishable from a genuine empty area.

The field map below is DATA, not code, for the same reason: when a position
moves, one number changes.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Dict, Iterator, List, Optional

import httpx

from ..models import Business, SourceQuery
from ..session import Profile
from . import source
from .overpass import geocode

ENDPOINT = "https://www.google.com/search"

# Where each field sits inside one place record. Verified against a live
# response; when Google moves one, change the number here and nothing else.
FIELDS = {
    "cid":        10,      # "0x4876...:0xedbf..." -- the same identity gosom uses
    "name":       11,
    "categories": 13,      # list, most specific first
    "address":    39,
    "coords":      9,      # [_, _, lat, lng]
    "rating":      4,      # [.., 7] = stars
    "website":     7,      # [url, display domain]
    "phone":     178,      # deeply nested; see _phone
    "hours":     203,
}
RATING_AT = 7              # index inside FIELDS["rating"]

# The request template. Everything Google returns is decided by `pb`, so it is
# exposed as an option: a future field can be enabled without editing code.
#   1d = viewport metres, 2d/3d = lng/lat, 7i = page size, 8i = offset
PB_TEMPLATE = ("!4m12!1m3!1d{span}!2d{lng}!3d{lat}!2m3!1f0!2f0!3f0"
               "!3m2!1i1024!2i768!4f13.1!7i{take}!8i{skip}!10b1"
               "!12m3!1e3!2b1!3e2!2b1!4b1!9b0")

PAGE = 20                  # the endpoint's page size
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
RETRIES = 4
BACKOFF = 2.0


class ShapeChanged(RuntimeError):
    """The response no longer looks like a search result.

    Deliberately distinct from a network error: this one means the code needs
    updating, and telling the user "0 results" instead would be a lie.
    """


class Blocked(RuntimeError):
    """Google served a consent wall, a CAPTCHA, or a rate-limit page.

    Kerb does not attempt to solve or bypass any of these. It stops and says so,
    because a collector that fights a block is one that gets an address banned.
    """


def _clean(raw: str) -> Any:
    """Strip the XSSI prefix and parse. Anything else is a block page."""
    text = raw.lstrip()
    if text.startswith(")]}'"):
        text = text[text.index("\n") + 1:]
    elif text[:15].lower().startswith("<!doctype") or text[:6].lower() == "<html>":
        raise Blocked("Google returned a web page instead of data -- usually a "
                      "consent wall or a rate-limit block. Run `kerb setup`, or "
                      "wait and lower the worker count.")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ShapeChanged("the response did not parse as JSON: %s" % exc)


def parse(raw: str, place_label: str = "") -> List[Business]:
    """One search response -> businesses. Raises rather than returning nothing."""
    data = _clean(raw)
    try:
        entries = data[0][1]
    except (TypeError, IndexError, KeyError):
        raise ShapeChanged(
            "no result list at data[0][1] -- the response shape has moved. "
            "Kerb is refusing to report this as 'no businesses found'.")
    if not isinstance(entries, list):
        raise ShapeChanged("data[0][1] was %s, expected a list"
                           % type(entries).__name__)

    out: List[Business] = []
    seen_record = False
    for entry in entries:
        rec = entry[14] if (isinstance(entry, list) and len(entry) > 14
                            and isinstance(entry[14], list)) else None
        if rec is None:
            continue                      # the first entry is a header, not a place
        seen_record = True
        biz = _business(rec, place_label)
        if biz is not None:
            out.append(biz)

    if entries and not seen_record:
        raise ShapeChanged(
            "%d entries came back but none held a place record at [14] -- the "
            "response shape has moved." % len(entries))
    return out


def _at(rec: List[Any], key: str) -> Any:
    i = FIELDS[key]
    return rec[i] if len(rec) > i else None


def _phone(rec: List[Any]) -> Optional[str]:
    """The display number, out of a five-deep nest. Best effort by design."""
    node = _at(rec, "phone")
    try:
        return node[0][0] or None
    except (TypeError, IndexError):
        return None


def _website(rec: List[Any]) -> Optional[str]:
    node = _at(rec, "website")
    if isinstance(node, list) and node:
        return node[0] or None
    return node if isinstance(node, str) else None


def _business(rec: List[Any], place_label: str) -> Optional[Business]:
    cid = _at(rec, "cid")
    name = _at(rec, "name")
    if not cid or not isinstance(cid, str) or not name:
        return None                       # no identity, or no subject: not a lead

    cats = _at(rec, "categories")
    coords = _at(rec, "coords") or []
    rating_node = _at(rec, "rating")
    rating = None
    if isinstance(rating_node, list) and len(rating_node) > RATING_AT:
        rating = _num(rating_node[RATING_AT])

    return Business(
        cid=cid,                          # already "0x...:0x...", Kerb's own format
        name=str(name),
        category=(cats[0] if isinstance(cats, list) and cats else None),
        address=_at(rec, "address"),
        phone=_phone(rec),
        website=_website(rec),
        rating=rating,
        # review_count is deliberately absent -- see the module note in
        # gmaps_source(). Guessing it would be worse than leaving it unknown,
        # because a wrong count silently changes every score.
        review_count=None,
        lat=_num(coords[2]) if len(coords) > 2 else None,
        lng=_num(coords[3]) if len(coords) > 3 else None,
        hours=None,
        source="gmaps",
        place_label=place_label,
        # Nothing is discarded: a field ignored today is a signal someone writes
        # next month, and re-collecting to recover it costs far more.
        extras={"categories": cats if isinstance(cats, list) else [],
                "maps_url": "https://www.google.com/maps?cid=%s" % _decimal_cid(cid)},
    )


def _decimal_cid(cid: str) -> str:
    """`0xAAA:0xBBB` -> the decimal cid a maps.google.com/?cid= link wants."""
    try:
        return str(int(cid.split(":")[1], 16))
    except (IndexError, ValueError):
        return cid


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _request(client: httpx.Client, params: Dict[str, str],
             retries: int, backoff: float,
             tally: Optional[Dict[str, Any]] = None) -> str:
    """One page, retried on the failures that are expected of a busy endpoint."""
    last: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            # Counted before the response, so retries count too: a retry is
            # spend, and a budget that ignores them is not a budget.
            if tally is not None:
                tally["requests"] = tally.get("requests", 0) + 1
            r = client.get(ENDPOINT, params=params)
            if r.status_code == 429 or "/sorry/" in str(r.url):
                raise Blocked(
                    "Google is rate-limiting this address. Kerb will not try to "
                    "work around that. Wait, lower workers, and re-run.")
            if r.status_code in RETRY_STATUS:
                if attempt == retries - 1:
                    raise RuntimeError("Google answered %s repeatedly" % r.status_code)
                time.sleep(backoff * (2 ** attempt))
                continue
            r.raise_for_status()
            return r.text
        except Blocked:
            raise                                     # never retried: it is a decision
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise last if last else RuntimeError("unreachable")


def _ago(sec: float) -> str:
    for unit, n in (("h", 3600), ("m", 60)):
        if sec >= n:
            return "%d%s" % (sec // n, unit)
    return "%ds" % sec


def _sleep(base: float) -> None:
    """Pause, jittered.

    A fixed interval is itself a signature -- nothing human requests every
    2.000 seconds. The jitter costs nothing and removes the pattern.
    """
    if base <= 0:
        return
    time.sleep(base * random.uniform(0.65, 1.45))


def _span(zoom_km: float) -> int:
    """Viewport metres for the `1d` slot. Bigger = wider search."""
    return max(500, int(zoom_km * 1000))


@source(id="gmaps", label="Google Maps",
        description="Collected by Kerb itself -- no key, no browser, no scraper",
        takes="places", cannot_measure=("reviews",),
        instead={"reviews": "rating_band"},
        legal_note="Reads public business listings the way the Maps site does. "
                   "This is against Google's Terms of Service, and the endpoint "
                   "is undocumented and may change. Kerb never solves a CAPTCHA "
                   "or works around a block -- it stops and tells you.")
def gmaps_source(q: SourceQuery) -> Iterator[Business]:
    """Search each place for the trade, page through, yield businesses.

    KNOWN GAP -- review counts. The endpoint returns them only for a `pb`
    template that has not been worked out yet, so `review_count` arrives as
    None and the `reviews` condition will report those businesses as
    UNEVALUATED rather than rejecting them. That is the correct behaviour for a
    measurement Kerb could not take, and it is why it is not faked. Two ways
    forward: turn the review-count condition off for gmaps runs, or supply a
    better template with `options.pb`.
    """
    if not q.what:
        raise ValueError("the gmaps source searches by trade. Name one "
                         "(what.trade: hospitals, or pick a trade pack).")
    if not q.places:
        raise ValueError("the gmaps source needs at least one place to search.")

    prof = Profile.load()
    # Walking back into a live block is how a short penalty becomes a long one.
    cool = prof.cooling()
    if cool and not q.options.get("ignore_cooldown"):
        raise Blocked(
            "Google blocked this address recently; Kerb is cooling down for "
            "another %s. Re-run after that, or lower the worker count first. "
            "(`kerb setup --check` shows the remaining time.)" % _ago(cool))

    pages = max(1, int(q.options.get("max_pages", 5)))
    pause = float(q.options.get("pause", 2.0))
    zoom_km = float(q.options.get("zoom_km", 10))
    retries = int(q.options.get("retries", RETRIES))
    backoff = float(q.options.get("backoff", BACKOFF))
    template = str(q.options.get("pb") or PB_TEMPLATE)
    hl = str(q.options.get("hl") or prof.locale.get("hl", "en"))
    gl = str(q.options.get("gl") or prof.locale.get("gl", "us"))

    yielded = 0
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0),
                      headers=prof.headers(), cookies=prof.cookies) as client:
        for place in q.places:
            try:
                box = geocode(place, client, retries=retries, backoff=backoff)
                if not box:
                    q.report.setdefault("skipped_places", {})[
                        "gmaps/%s" % place] = "could not be geocoded"
                    continue
                lat = (box["north"] + box["south"]) / 2.0
                lng = (box["east"] + box["west"]) / 2.0

                seen_here = 0
                for page in range(pages):
                    pb = template.format(span=_span(zoom_km), lat=lat, lng=lng,
                                         take=PAGE, skip=page * PAGE)
                    raw = _request(client, {"tbm": "map", "authuser": "0",
                                            "hl": hl, "gl": gl,
                                            "q": "%s %s" % (q.what, place),
                                            "pb": pb},
                                   retries, backoff, tally=q.report)
                    found = parse(raw, place)
                    for biz in found:
                        yield biz
                        yielded += 1
                        seen_here += 1
                        if q.limit and yielded >= q.limit:
                            return
                    if len(found) < PAGE:
                        break                       # last page for this place
                    _sleep(pause)

                if seen_here == 0:
                    # Reported, not swallowed. "Nothing here" and "we could not
                    # read here" look identical in a result count.
                    q.report.setdefault("skipped_places", {})[
                        "gmaps/%s" % place] = "no listings returned for this trade"
            except Blocked as exc:
                # A block applies to the address, not the place, so every
                # remaining place would hit it too. Stopping is the honest move
                # -- and the penalty is recorded so the NEXT run waits too.
                wait = prof.record_block()
                q.report["fatal"] = ("%s Kerb will stay off this endpoint for %s."
                                     % (exc, _ago(wait)))
                q.report["cooldown_seconds"] = wait
                return
            except ShapeChanged as exc:
                q.report["fatal"] = (
                    "%s Kerb stopped rather than report an empty result." % exc)
                return
            except Exception as exc:                # noqa: BLE001
                q.report.setdefault("skipped_places", {})[
                    "gmaps/%s" % place] = "%s: %s" % (type(exc).__name__, exc)
                continue
            _sleep(pause)

        # Every place completed without a block: nothing is owed.
        prof.record_success()
