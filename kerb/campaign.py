"""The campaign -- one runnable unit of work, as data.

Everything the pipeline needs lives in one YAML file: where to look, what to
look for, what disqualifies, how to rank, what it may spend. The UI is a
builder for this file and nothing more, which is what keeps the two honest --
anything the UI can express, the file can express, and vice versa.

That matters for a reason beyond tidiness: a campaign is then reviewable,
diffable, version-controllable and shareable. "Send me your campaign" is a
one-file answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .models import Cost
from .packs import ADHOC_ID, adhoc_trade


@dataclass
class Limits:
    max_results: Optional[int] = None
    max_requests: Optional[int] = None
    max_runtime_seconds: Optional[int] = None
    workers: Dict[str, int] = field(default_factory=lambda: {"discover": 6, "profile": 6})
    # Circuit breaker tuning: {rate, window, min_sample}. Defaults live in
    # health.py; override only with a reason.
    breaker: Dict[str, Any] = field(default_factory=dict)
    # How many times a durable UNIT of work may be attempted before it is
    # recorded as failed. Deliberately separate from a source's `retries`
    # option, which is how many times one HTTP call is retried inside a single
    # attempt -- one name for both would make "retries: 1" mean two things.
    attempts: Optional[int] = None


_DURATION = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(v) -> Optional[int]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower()
    if s[-1] in _DURATION:
        try:
            return int(float(s[:-1]) * _DURATION[s[-1]])
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        return None


@dataclass
class Campaign:
    name: str = "untitled"
    description: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=lambda: [{"id": "overpass"}])
    where: Dict[str, Any] = field(default_factory=dict)
    what: Dict[str, Any] = field(default_factory=dict)
    filters: List[Dict[str, Any]] = field(default_factory=list)
    scoring: Dict[str, Any] = field(default_factory=dict)
    gating: Dict[str, List[str]] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    output: Dict[str, Any] = field(default_factory=dict)
    suppress: Dict[str, Any] = field(default_factory=dict)
    signal_options: Dict[str, Any] = field(default_factory=dict)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Campaign":
        lim = d.get("limits") or {}
        c = cls(
            name=d.get("name", "untitled"),
            description=d.get("description", ""),
            sources=d.get("sources") or [{"id": "overpass"}],
            where=d.get("where") or {},
            what=d.get("what") or {},
            filters=d.get("filters") or [],
            scoring=d.get("scoring") or {},
            gating=d.get("gating") or {},
            output=d.get("output") or {},
            suppress=d.get("suppress") or {},
            signal_options=d.get("signal_options") or {},
            limits=Limits(
                max_results=lim.get("max_results"),
                max_requests=lim.get("max_requests"),
                max_runtime_seconds=parse_duration(lim.get("max_runtime")),
                workers={**{"discover": 6, "profile": 6}, **(lim.get("workers") or {})},
                breaker=lim.get("breaker") or {},
                attempts=lim.get("attempts"),
            ),
        )
        c._wire_trade_pack()
        return c

    @classmethod
    def from_yaml(cls, path_or_text: str) -> "Campaign":
        p = Path(path_or_text).expanduser()
        text = p.read_text() if p.exists() else path_or_text
        return cls.from_dict(yaml.safe_load(text) or {})

    def _wire_trade_pack(self) -> None:
        """`what.packs` is the user-facing control; trade_match needs it as an
        option. Wiring it here means the campaign file never says the same
        thing twice.

        Every pack is passed, not just the first. `packs[0]` was silently
        dropping the rest, so a multi-trade campaign rejected two thirds of
        what it asked for.
        """
        packs = list(self.what.get("packs") or [])
        if self.what.get("trade") and ADHOC_ID not in packs:
            # A trade typed into the brief is wired exactly like a shipped one,
            # so nothing downstream needs to know which it was.
            packs.append(ADHOC_ID)
        if packs:
            opts = dict(self.signal_options.get("trade_match") or {})
            opts.setdefault("packs", list(packs))
            opts.setdefault("pack", packs[0])          # single-trade sources
            opts.setdefault("match", self.what.get("match", "any"))
            self.signal_options["trade_match"] = opts

    @property
    def typed_pack(self):
        """The ad-hoc trade pack this campaign defines, if it typed one.

        Returns a plain dict so the caller decides which library it joins --
        never the process-wide one, which the server shares across campaigns.
        """
        term = self.what.get("trade")
        if not term:
            return None
        return adhoc_trade(term, {
            "categories": self.what.get("categories"),
            "keywords": self.what.get("keywords"),
            "vetoes": self.what.get("vetoes"),
            "osm_tags": self.what.get("osm_tags"),
        })

    # -- derived ---------------------------------------------------------

    @property
    def places(self) -> List[str]:
        """Every place to search, in the order they should be searched.

        Order is not cosmetic. It only matters when a run does not finish --
        which is exactly when it matters most. A run stopped by the breaker at
        40% should have spent that 40% on the places worth the most, and two
        hand-rolled reordering scripts in the predecessor say so.
        """
        from .packs import library
        mode = self.where.get("mode", "search")
        out: List[str] = []

        if mode == "paste" or "places" in self.where:
            out = [p for p in (self.where.get("places") or []) if p]
        elif mode == "packs":
            lib = library()
            for pid in self.where.get("packs") or []:
                pack = lib.maybe(pid)
                if not pack:
                    continue
                for entry in pack.get("places") or []:
                    out.append(entry["name"] if isinstance(entry, dict) else str(entry))
        elif self.where.get("place"):
            out = [self.where["place"]]

        # Exclusions apply however the places were expressed, so a pack can be
        # taken wholesale and trimmed rather than copied and edited.
        excl = {e.lower() for e in (self.where.get("exclude") or [])}
        if excl:
            out = [p for p in out if p.lower() not in excl]

        seen: set = set()
        deduped = [p for p in out if not (p.lower() in seen or seen.add(p.lower()))]
        return self._ordered(deduped)

    def _ordered(self, places: List[str]) -> List[str]:
        order = str(self.where.get("order", "as-listed")).lower()
        if order in ("as-listed", "", "none"):
            return places
        if order == "alphabetical":
            return sorted(places, key=str.lower)
        if order == "random":
            import random
            shuffled = list(places)
            # Seedable, because "random" that cannot be reproduced makes a
            # partial run impossible to reason about afterwards.
            random.Random(self.where.get("seed")).shuffle(shuffled)
            return shuffled
        if order == "priority":
            ranked = [str(p).lower() for p in (self.where.get("priority") or [])]

            def rank(p: str) -> int:
                low = p.lower()
                for i, want in enumerate(ranked):
                    if low == want or low.startswith(want) or want in low:
                        return i
                return len(ranked)          # unlisted places keep their order, last

            return sorted(places, key=lambda p: (rank(p), places.index(p)))
        raise ValueError(
            "where.order must be as-listed, priority, alphabetical or random; "
            "got %r" % order)

    @property
    def trades(self) -> List[str]:
        """Every trade this campaign accepts, shipped packs and typed alike."""
        out = [p.split("/")[-1] for p in (self.what.get("packs") or [])]
        if self.what.get("trade"):
            out.append(str(self.what["trade"]))
        if not out:
            label = (self.what.get("custom") or {}).get("label")
            if label:
                out.append(label)
        return out

    @property
    def trade(self) -> Optional[str]:
        """The first trade, for sources that can only search one thing."""
        t = self.trades
        return t[0] if t else None

    @property
    def weights(self) -> Dict[str, Any]:
        return self.scoring.get("weights") or {}

    @property
    def referenced_signals(self) -> set:
        """Signal names this campaign actually mentions, in filters or weights."""
        names = set()

        def walk(rules):
            for rule in rules or []:
                if "group" in rule:
                    walk(rule.get("of") or [])
                elif rule.get("signal"):
                    names.add(str(rule["signal"]).split(".")[0])

        walk(self.filters)
        for key in (self.weights or {}):
            names.add(str(key).split(".")[0])
        return names

    def signals_for(self, cost: Cost) -> List[str]:
        """Which signals may run at a given cost tier.

        An explicit `gating` block wins outright. Without one:

          FREE       every signal kerb ships. It costs nothing and richer
                     evidence is strictly better.
          CHEAP,     only signals the campaign actually names. These spend
          EXPENSIVE  requests, so they are opt-in -- shipping a new paid signal
                     must never quietly start costing existing campaigns money
                     or start fetching from sites they never asked about.

        Either way, a signal registered by something other than kerb -- a
        plugin, an application, a test module imported into the same process --
        only runs if the campaign names it. One broken third-party signal used
        to be enough to make every business in every campaign fail to measure.
        """
        from . import signals as sig
        named = self.gating.get(cost.value)
        if named is not None:
            return list(named)
        wanted = self.referenced_signals
        if cost is Cost.FREE:
            return [r.name for r in sig.by_cost(cost)
                    if sig.is_builtin(r.name) or r.name in wanted]
        return [r.name for r in sig.by_cost(cost) if r.name in wanted]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "sources": self.sources, "where": self.where, "what": self.what,
            "filters": self.filters, "scoring": self.scoring,
            "gating": self.gating, "output": self.output,
            "suppress": self.suppress,
            "signal_options": self.signal_options,
            "limits": {
                "max_results": self.limits.max_results,
                "max_requests": self.limits.max_requests,
                "max_runtime": self.limits.max_runtime_seconds,
                "workers": self.limits.workers,
                "breaker": self.limits.breaker,
                "attempts": self.limits.attempts,
            },
        }

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)


# Verdict-level fields a column or template may name, beyond the signals.
RECORD_FIELDS = {
    "cid", "name", "category", "address", "phone", "website", "booking_url",
    "rating", "review_count", "lat", "lng", "status_raw", "first_review_year",
    "source", "place_label", "score", "band", "outcome", "qualified",
    "rejected_by", "reject_reason", "failed_signals",
}


def check_output(cfg: Dict[str, Any]) -> List[str]:
    """Problems with the `output:` block.

    Columns and templates are checked against the real field names, because a
    typo that produces a silently blank column is the exact failure this
    project keeps removing -- and in a mail merge it is a blank line in an
    email that went to a customer.
    """
    from . import signals as sig
    if not cfg:
        return []
    problems: List[str] = []
    known = RECORD_FIELDS | {r.name for r in sig.all_signals()}

    for column in cfg.get("columns") or []:
        if str(column) not in known:
            problems.append(
                "output.columns names %r, which is not a field or a signal" % column)

    template = cfg.get("template")
    if template is not None:
        if not isinstance(template, dict):
            problems.append("output.template must be a mapping of column -> field")
        else:
            from .cli import template_fields
            for name in template_fields(template):
                if name not in known:
                    problems.append(
                        "output.template refers to %r, which is not a field or "
                        "a signal" % name)
            if cfg.get("columns"):
                problems.append(
                    "output has both `columns` and `template`; a template names "
                    "its own columns, so only one can apply")

    split = cfg.get("split_by")
    if split and str(split) not in known:
        problems.append("output.split_by names %r, which is not a field" % split)
    for key in ("min_score", "top"):
        val = cfg.get(key)
        if val is not None and (isinstance(val, bool)
                                or not isinstance(val, (int, float))):
            problems.append("output.%s must be a number" % key)
    return problems


def validate(d: Dict[str, Any]) -> List[str]:
    """Problems a user can fix, in their words. Empty list means valid.

    Deliberately returns everything wrong at once rather than raising on the
    first fault -- a builder that reveals one error per attempt is a bad tool.
    """
    from . import signals as sig
    from . import sources as src
    from .packs import library

    problems: List[str] = []
    lib = library()

    for s in d.get("sources") or []:
        sid = s.get("id") if isinstance(s, dict) else s
        try:
            src.get(sid)
        except KeyError:
            problems.append("unknown source %r" % sid)

    # A source that cannot start is a problem you want at validation time, not
    # forty places into a run.
    for entry in (d.get("sources") or []):
        sid = (entry or {}).get("id")

    what = d.get("what") or {}
    named = bool(what.get("packs") or what.get("trade")
                 or (what.get("custom") or {}).get("label"))
    if isinstance(what.get("trade"), str) and not what["trade"].strip():
        problems.append("what.trade is empty -- name the trade to look for.")
        named = False
    if not named:
        # Only a campaign that actually NEEDS a trade is missing one. Scoring a
        # file purely on reviews and web presence is a legitimate brief with no
        # trade in it at all.
        by_filter = any((f or {}).get("signal") == "trade_match"
                        for f in (d.get("filters") or []))
        by_weight = "trade_match" in ((d.get("scoring") or {}).get("weights") or {})
        # A source that searches by trade cannot even build its query without
        # one; it raises at collection time, which is far too late to hear it.
        from . import sources as _src
        searches = [x for x in (d.get("sources") or [{"id": "overpass"}])
                    if _src.get((x or {}).get("id") or "overpass").takes != "path"]
        if by_filter or by_weight:
            problems.append(
                "what: name a trade -- pick a pack or type one (what.trade: hospitals). "
                "trade_match is in use and has nothing to check against.")
        elif searches:
            problems.append(
                "what: name a trade -- pick a pack or type one (what.trade: hospitals). "
                "%s searches by trade and cannot build a query without one."
                % (searches[0].get("id") or "that source"))

    for pid in (d.get("what") or {}).get("packs") or []:
        if pid not in lib:
            problems.append("unknown trade pack %r" % pid)
    for pid in (d.get("where") or {}).get("packs") or []:
        if pid not in lib:
            problems.append("unknown place pack %r" % pid)

    known = {r.name for r in sig.all_signals()}
    for rule in d.get("filters") or []:
        if "group" in rule:
            continue
        name = (rule.get("signal") or "").split(".")[0]
        if name and name not in known:
            problems.append("filter refers to unknown signal %r" % name)
    from .scoring import check_bands, check_weight
    problems.extend(check_bands((d.get("scoring") or {}).get("bands")))
    problems.extend(check_output((d.get("output") or {})))
    for name, spec in (((d.get("scoring") or {}).get("weights")) or {}).items():
        if name.split(".")[0] not in known:
            problems.append("scoring refers to unknown signal %r" % name)
        problems.extend(check_weight(name, spec))

    where = d.get("where") or {}
    if not (where.get("places") or where.get("packs") or where.get("place")):
        # Mirror from_dict's default: no sources means overpass, which needs
        # places. Reading the empty list literally made `any([])` false, so an
        # empty sources list validated clean and then found nothing at runtime.
        srcs = d.get("sources") or [{"id": "overpass"}]
        if any((s.get("id") if isinstance(s, dict) else s) not in ("csv", "gosom")
               for s in srcs):
            problems.append("no places given -- set where.places, where.packs or where.place")
    return problems
