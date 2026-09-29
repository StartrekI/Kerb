"""OpenStreetMap via the Overpass API -- the default source.

Chosen as the default deliberately. It is free, needs no key and no billing,
and is explicitly cleared for commercial use under ODbL with attribution. The
tool is therefore fully functional the moment it is installed, before the user
configures anything and without touching a single site's terms of service.

That is a product decision as much as a legal one: a tool that shows nothing
until you supply credentials has already lost most of the people who tried it.

Attribution requirement: any output derived from this source must carry
"© OpenStreetMap contributors". The sink layer adds it automatically.

Politeness: the public endpoint is volunteer-run infrastructure. Defaults here
are conservative on purpose, and heavy users should point `endpoint` at their
own instance rather than making a free service carry a commercial workload.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterator, List, Optional

import httpx

from ..models import Business, SourceQuery
from . import source

OVERPASS = "https://overpass-api.de/api/interpreter"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "kerb/0.1 (open-source local business qualification; +https://github.com/StartrekI/Kerb)"

ATTRIBUTION = "© OpenStreetMap contributors"

# Trade -> OSM tag filters. OSM classifies by shop/craft/amenity/office rather
# than by free text, so this is a translation table, not a keyword search.
TRADE_TAGS: Dict[str, List[str]] = {
    "dentist":  ['amenity=dentist', 'healthcare=dentist'],
    "medical":  ['amenity=doctors', 'amenity=clinic', 'healthcare=doctor'],
    "vet":      ['amenity=veterinary', 'healthcare=veterinary'],
    "roofing":  ['craft=roofer'],
    "painting": ['craft=painter'],
    "hvac":     ['craft=hvac', 'craft=plumber'],
    "salon":    ['shop=hairdresser', 'shop=beauty'],
    "gym":      ['leisure=fitness_centre', 'amenity=gym'],
    "plumber":  ['craft=plumber'],
    "electrician": ['craft=electrician'],
}


# The keys OSM actually classifies businesses under. A guessed tag that does
# not exist simply returns nothing, so casting across all of them costs one
# query and never errors.
GUESS_KEYS = ("amenity", "shop", "healthcare", "craft", "office", "leisure", "tourism")


def guess_tags(term: str) -> List[str]:
    """Candidate OSM tags for a trade nobody has curated.

    TRADE_TAGS is a hand-written map, which means an open-source tool could
    only discover the ten trades its author thought of. OSM tag VALUES are
    overwhelmingly the lowercase singular noun -- `amenity=hospital`,
    `shop=bakery`, `craft=carpenter` -- so the phrase somebody typed usually
    IS the value. Cast it across every key that classifies businesses and let
    Overpass discard the combinations that do not exist.

    A curated entry still wins: this only runs when there is nothing better.
    """
    t = "_".join(str(term or "").lower().split())
    t = "".join(ch for ch in t if ch.isalnum() or ch == "_").strip("_")
    if not t:
        return []
    stems = {t}
    if t.endswith("ies") and len(t) > 4:
        stems.add(t[:-3] + "y")
    elif t.endswith("es") and len(t) > 4 and t[-3] in "sxzh":
        stems.add(t[:-2])
    elif t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        stems.add(t[:-1])
    return ["%s=%s" % (k, stem) for stem in sorted(stems) for k in GUESS_KEYS]


# Overpass answers 429 (slot exhausted) and 504 (query timed out) as a matter
# of routine -- it is free volunteer infrastructure with a scheduler, and being
# told to wait is normal operation rather than an error. Without backoff the
# first 429 aborted the entire run.
RETRY_STATUS = frozenset({429, 502, 503, 504})
RETRIES = 3
BACKOFF = 5.0


def _request(client: httpx.Client, method: str, url: str,
             retries: int = RETRIES, backoff: float = BACKOFF,
             tally: Optional[Dict[str, Any]] = None,
             **kw) -> httpx.Response:
    """One request, retried with backoff on the failures that are expected.

    Honours Retry-After when the server sends it: guessing an interval shorter
    than the one we were explicitly given is how a client gets banned.
    """
    last: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            # Counted before the response, so retries count too -- a retry is
            # spend, and a budget that ignores them is not a budget.
            if tally is not None:
                tally["requests"] = tally.get("requests", 0) + 1
            r = client.request(method, url, **kw)
            if r.status_code in RETRY_STATUS:
                if attempt == retries - 1:
                    r.raise_for_status()
                wait = backoff * (2 ** attempt)
                try:
                    wait = max(wait, float(r.headers.get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise last if last else RuntimeError("unreachable")


def geocode(place: str, client: httpx.Client, endpoint: str = NOMINATIM,
            **retry) -> Optional[Dict[str, float]]:
    """Place name -> bounding box, via Nominatim or a compatible `endpoint`."""
    r = _request(client, "GET", endpoint,
                 params={"q": place, "format": "json", "limit": 1},
                 headers={"User-Agent": UA}, timeout=30, **retry)
    hits = r.json()
    if not hits:
        return None
    bb = hits[0]["boundingbox"]        # [south, north, west, east]
    return {"south": float(bb[0]), "north": float(bb[1]),
            "west": float(bb[2]), "east": float(bb[3])}


def build_query(tags: List[str], bbox: Dict[str, float], limit: int = 500) -> str:
    box = "%(south)f,%(west)f,%(north)f,%(east)f" % bbox
    parts = []
    for tag in tags:
        key, _, value = tag.partition("=")
        sel = '["%s"="%s"]' % (key, value) if value else '["%s"]' % key
        for kind in ("node", "way"):
            parts.append("  %s%s(%s);" % (kind, sel, box))
    return "[out:json][timeout:60];\n(\n%s\n);\nout center %d;" % ("\n".join(parts), limit)


def _addr(tags: Dict[str, str]) -> Optional[str]:
    bits = [tags.get("addr:housenumber"), tags.get("addr:street"),
            tags.get("addr:city"), tags.get("addr:postcode")]
    joined = " ".join(b for b in bits if b).strip()
    return joined or None


def _category(tags: Dict[str, str]) -> Optional[str]:
    for key in ("craft", "shop", "amenity", "healthcare", "office", "leisure"):
        if key in tags:
            return tags[key].replace("_", " ").title()
    return None


@source(id="overpass", label="OpenStreetMap",
        description="Free, licensed, no key required — the default source",
        legal_note="ODbL. Commercial use permitted with attribution: %s" % ATTRIBUTION)
def overpass_source(q: SourceQuery) -> Iterator[Business]:
    tags = (q.options.get("tags")
            or TRADE_TAGS.get((q.what or "").lower())
            or guess_tags(q.what))
    if not tags:
        raise ValueError(
            "no OSM tags for trade %r. Supply options.tags (e.g. ['craft=roofer']) "
            "or add osm_tags to the trade pack. OSM classifies by tag, not free text."
            % q.what)

    pause = float(q.options.get("pause", 2.0))
    retry = {"retries": int(q.options.get("retries", RETRIES)),
             "backoff": float(q.options.get("backoff", BACKOFF)),
             "tally": q.report}
    endpoint = q.options.get("endpoint", OVERPASS)
    # Its own option, like `endpoint`: a self-hosted Overpass usually comes
    # with a self-hosted Nominatim, and without this every place still went
    # to the public one first.
    geocoder = q.options.get("geocoder", NOMINATIM)
    per_place = int(q.options.get("per_place", 500))
    emitted = 0

    # Written through to the caller as we go, so an early return on q.limit
    # still leaves an accurate record behind.
    failed: Dict[str, str] = q.report.setdefault("failed_places", {})
    with httpx.Client(follow_redirects=True) as client:
        for place in (q.places or []):
            # One unreachable place must cost that place and nothing more.
            # A timeout on the third of fifty towns used to discard the two
            # already collected and the forty-seven not yet tried.
            try:
                bbox = geocode(place, client, endpoint=geocoder, **retry)
                if bbox is None:
                    failed[place] = "could not be geocoded"
                    continue
                time.sleep(pause)      # Nominatim asks for <=1 req/sec
                r = _request(client, "POST", endpoint,
                             data={"data": build_query(tags, bbox, per_place)},
                             headers={"User-Agent": UA}, timeout=120, **retry)
                elements = r.json().get("elements", [])
            except Exception as exc:               # noqa: BLE001
                failed[place] = "%s: %s" % (type(exc).__name__, exc)
                continue

            for el in elements:
                tg = el.get("tags") or {}
                name = (tg.get("name") or "").strip()
                if not name:
                    continue           # an unnamed POI is not a business record

                centre = el.get("center") or {}
                yield Business(
                    # OSM's own stable identity. Not a Google CID, but exact
                    # within this source and never invented.
                    cid="osm:%s/%s" % (el.get("type"), el.get("id")),
                    name=name,
                    category=_category(tg),
                    address=_addr(tg),
                    phone=tg.get("phone") or tg.get("contact:phone"),
                    website=tg.get("website") or tg.get("contact:website"),
                    lat=el.get("lat") or centre.get("lat"),
                    lng=el.get("lon") or centre.get("lon"),
                    status_raw=tg.get("opening_hours"),
                    source="overpass",
                    place_label=place,
                    extras={"osm_tags": tg, "attribution": ATTRIBUTION},
                )
                emitted += 1
                if q.limit and emitted >= q.limit:
                    return
            time.sleep(pause)
