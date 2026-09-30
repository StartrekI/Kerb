"""Source adapters -- where businesses come from.

The strategic core of the project lives in this package's existence. Kerb's
value is computed AFTER the source, so a source is swappable: OpenStreetMap,
an official API, a CSV another scraper produced, or our own collector.

That has two consequences worth stating plainly:

  1. Every extraction advantage a dedicated scraper has -- proxy pools, grid
     subdivision, distributed queues, years of anti-bot hardening -- becomes
     ours the moment we accept its output. We do not compete with that work;
     we consume it.

  2. No single hostile source can break the project. A tool whose value is
     inside the scraper is one Google change away from nothing.

A source yields Business records. Nothing else is required of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List

from ..models import Business, SourceQuery


@dataclass
class RegisteredSource:
    fn: Callable[[SourceQuery], Iterator[Business]]
    id: str
    label: str
    description: str
    needs_key: bool
    needs_browser: bool
    legal_note: str
    # What the source is asking the caller for. The UI used to infer this by
    # testing the source id against a hardcoded list, so a new source rendered
    # the wrong field until someone remembered to edit the front end.
    takes: str = "places"          # "path" | "places"
    # Signals this source structurally cannot supply. Declared here so the UI
    # can warn BEFORE a run instead of the user reading a screen of
    # "unevaluated" afterwards and assuming Kerb is broken. A tuple, or a
    # function returning one when the answer depends on what the service has
    # been seen to send -- read it through unmeasurable().
    cannot_measure: Any = ()
    # What to use instead, when the source cannot supply something. Lets the UI
    # offer a way forward rather than only a refusal.
    instead: dict = None
    # The environment variable a keyed source reads its key from. The UI names
    # it in the "set this up first" notice; it used to hard-code one variable
    # name for every keyed source, belonging to a source that does not exist.
    key_env: str = ""

    def unmeasurable(self) -> tuple:
        """What this source cannot supply right now."""
        c = self.cannot_measure
        return tuple(c() if callable(c) else c)


_SOURCES: Dict[str, RegisteredSource] = {}


def source(id: str, label: str = "", description: str = "",
           needs_key: bool = False, needs_browser: bool = False,
           legal_note: str = "", takes: str = "places",
           cannot_measure: Any = (), instead: dict = None,
           key_env: str = ""):
    def wrap(fn):
        _SOURCES[id] = RegisteredSource(
            fn=fn, id=id, label=label or id,
            description=description or (fn.__doc__ or "").strip().split("\n")[0],
            needs_key=needs_key, needs_browser=needs_browser,
            legal_note=legal_note, takes=takes,
            cannot_measure=(cannot_measure if callable(cannot_measure)
                            else tuple(cannot_measure)),
            instead=dict(instead or {}),
            key_env=key_env)
        return fn
    return wrap


def get(source_id: str) -> RegisteredSource:
    try:
        return _SOURCES[source_id]
    except KeyError:
        raise KeyError("no source %r (have: %s)"
                       % (source_id, ", ".join(sorted(_SOURCES)))) from None


def all_sources() -> List[RegisteredSource]:
    return [_SOURCES[k] for k in sorted(_SOURCES)]


def fetch(source_id: str, query: SourceQuery) -> Iterator[Business]:
    return get(source_id).fn(query)


from . import csv_ingest, gmaps, overpass  # noqa: E402,F401
