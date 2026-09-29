"""Running a source through the durable collector.

The plain source protocol -- a generator yielding businesses -- is right for a
file and for a handful of places. It is the wrong shape for a long collection:
a generator has no way to record that place 34 of 200 succeeded, so a process
that dies at place 34 starts again at place 1.

This wraps any place-based source so each place becomes a durable unit of work:
claimed under a lease, retried with backoff, its results committed with its
completion. Killing the process costs the places currently in flight and
nothing else.

    kerb run campaign.yaml --durable
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from .. import sources
from ..collect import (DEFAULT_WORKERS, CollectResult, Fatal, Halt, RateLimiter,
                       collect)
from ..models import Business, SourceQuery
from ..store import Store

# Overpass and Nominatim are volunteer infrastructure with published etiquette:
# roughly one request a second, and no parallel hammering. This is a politeness
# default, not a throughput target -- point `endpoint` (and `geocoder`, for
# Nominatim) at your own instances if you need more.
POLITE = {"overpass": 0.5, "nominatim": 1.0}
DEFAULT_RATE = 1.0


def rate_for(source_id: str, options: Dict[str, Any]) -> RateLimiter:
    if options.get("per_second"):
        return RateLimiter(per_second=float(options["per_second"]))
    return RateLimiter(per_second=POLITE.get(source_id, DEFAULT_RATE))


def _fetch_one_place(source_id: str, trade: Optional[str],
                     options: Dict[str, Any]) -> Callable:
    """One place -> businesses, as the collector wants it.

    Retry policy lives here because only this layer knows which failures are
    worth another attempt. A place that cannot be geocoded will never geocode;
    retrying it four times just delays the run and annoys the endpoint.
    """
    def fetch(place: str, ctx: Dict[str, Any]) -> Iterator[Business]:
        # The collector's limiter goes to the source, so a source that makes
        # several requests per place (pages) paces every one of them, not only
        # the first. `pause` is 0 here precisely because this limiter exists.
        opts = dict(options)
        if ctx.get("limiter") is not None:
            opts["_limiter"] = ctx["limiter"]
        query = SourceQuery(what=trade, places=[place], options=opts)
        for biz in sources.fetch(source_id, query):
            yield biz
        report = query.report
        if report.get("fatal"):
            # The source stopped itself -- a block, a shape it cannot parse.
            # Recording this place as done would bank an empty result for it
            # permanently; Halt stops the run and hands the place back.
            raise Halt(report["fatal"])
        failed = report.get("failed_places") or {}
        if place in failed:
            why = failed[place]
            if "geocoded" in why:
                raise Fatal(why)          # deterministic; retrying cannot help
            raise RuntimeError(why)       # transport or rate limit: retry
    return fetch


def collect_places(store: Store, run_id: str, source_id: str,
                   places: List[str], trade: Optional[str] = None,
                   options: Optional[Dict[str, Any]] = None,
                   workers: int = DEFAULT_WORKERS,
                   on_progress=None, attempts: Optional[int] = None,
                   **kw) -> CollectResult:
    """Collect every place durably, resuming whatever a previous attempt left."""
    options = dict(options or {})
    # The collector paces the requests, so the source must not sleep as well --
    # two independent delays multiply and halve the throughput for no gain.
    options.setdefault("pause", 0)
    return collect(store, run_id, "place:%s" % source_id, places,
                   _fetch_one_place(source_id, trade, options),
                   workers=workers, rate=rate_for(source_id, options),
                   on_progress=on_progress or (lambda e: None),
                   payload={"source": source_id, "trade": trade},
                   attempts=attempts, **kw)
