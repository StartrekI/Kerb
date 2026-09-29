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
                # Both spellings. The UI and the README write
                # `max_runtime_seconds`; this read only `max_runtime`, so the
                # runtime cap anyone actually set was silently dropped.
                max_runtime_seconds=parse_duration(
                    lim["max_runtime_seconds"] if "max_runtime_seconds" in lim
                    else lim.get("max_runtime")),
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
        if self._typed_term() and ADHOC_ID not in packs:
            # A trade typed into the brief is wired exactly like a shipped one,
            # so nothing downstream needs to know which it was.
            packs.append(ADHOC_ID)
        if packs:
            opts = dict(self.signal_options.get("trade_match") or {})
            opts.setdefault("packs", list(packs))
            opts.setdefault("pack", packs[0])          # single-trade sources
            opts.setdefault("match", self.what.get("match", "any"))
            self.signal_options["trade_match"] = opts

    def _typed_term(self) -> Optional[str]:
        """The trade this campaign typed rather than picked from a pack.

        `what.custom` is the older spelling of the same thing. It used to be
        read for the search term and nowhere else, so trade_match had no pack to
        check against, answered "unknown" -- and `trade_match != false` passed
        every business, pizza restaurants included.
        """
        term = self.what.get("trade")
        if not term:
            term = (self.what.get("custom") or {}).get("label")
        return str(term) if term else None

    @property
    def typed_pack(self):
        """The ad-hoc trade pack this campaign defines, if it typed one.

        Returns a plain dict so the caller decides which library it joins --
        never the process-wide one, which the server shares across campaigns.
        """
        term = self._typed_term()
        if not term:
            return None
        source = self.what if self.what.get("trade") else (self.what.get("custom") or {})
        return adhoc_trade(term, {
            "categories": source.get("categories"),
            "keywords": source.get("keywords"),
            "vetoes": source.get("vetoes"),
            "osm_tags": source.get("osm_tags"),
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
        out: List[str] = []

        # Every way of naming places counts, whatever `mode` says. The places
        # used to be chosen BY mode, so `where: {packs: [geo/uk-affluent]}` --
        # the form CUSTOMIZATION.md shows -- validated clean (validation looked
        # at the keys) and then searched nothing at all (this looked at mode).
        # Listing places, packs and a single place together is a union.
        out += [str(p) for p in (self.where.get("places") or []) if p]
        lib = library()
        for pid in self.where.get("packs") or []:
            pack = lib.maybe(pid)
            if not pack:
                continue                    # validate() reports the unknown id
            for entry in pack.get("places") or []:
                out.append(entry["name"] if isinstance(entry, dict) else str(entry))
        if self.where.get("place"):
            out.append(str(self.where["place"]))

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
                "max_runtime_seconds": self.limits.max_runtime_seconds,
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


# Every key a campaign may carry, per section. A key outside these is a
# problem, not a silence: `max_runtime_seconds` was once ignored for months
# because nothing said the reader only knew `max_runtime`. CUSTOMIZATION.md
# names the rule -- something must fail if a key is misspelled.
CAMPAIGN_KEYS = {"name", "description", "sources", "where", "what", "filters",
                 "scoring", "gating", "limits", "output", "suppress",
                 "signal_options"}
WHERE_KEYS = {"mode", "places", "packs", "place", "exclude", "order",
              "priority", "seed"}
WHAT_KEYS = {"packs", "trade", "match", "categories", "keywords", "vetoes",
             "osm_tags", "custom"}
SCORING_KEYS = {"weights", "normalise", "confidence", "bands"}
LIMIT_KEYS = {"max_results", "max_requests", "max_runtime",
              "max_runtime_seconds", "workers", "breaker", "attempts"}
SUPPRESS_KEYS = {"lists", "cids", "runs", "after"}
OUTPUT_KEYS = {"columns", "template", "min_score", "top", "split_by", "sort"}
BREAKER_KEYS = {"rate", "window", "min_sample"}
TIERS = {c.value for c in Cost}
WHERE_MODES = {"paste", "packs", "search"}
ORDERS = {"as-listed", "", "none", "alphabetical", "random", "priority"}


def _unknown(section: str, got: Dict[str, Any], allowed: set) -> List[str]:
    return ["%s%s is not a setting Kerb reads (known: %s)"
            % (section, k, ", ".join(sorted(allowed)))
            for k in got if k not in allowed]


def _source_id(spec) -> Optional[str]:
    """`{id: csv, options: ...}` or the bare string `csv` -- the pipeline takes both."""
    if isinstance(spec, dict):
        return spec.get("id")
    if isinstance(spec, str):
        return spec
    return None


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def check_rules(rules, known_signals: set, where: str = "filters") -> List[str]:
    """Problems with a filter list, groups included. Never raises.

    An unknown operator used to pass validation and then raise inside the
    pipeline, killing the run on its first business.
    """
    from .scoring import ORDERING, OPS
    if rules is None:
        return []
    if not isinstance(rules, list):
        return ["%s must be a list of conditions" % where]
    problems: List[str] = []
    for i, rule in enumerate(rules):
        label = "%s[%d]" % (where, i)
        if not isinstance(rule, dict):
            problems.append("%s must be a mapping like {signal, op, value}" % label)
            continue
        if "group" in rule:
            if rule.get("group") not in ("any", "all"):
                problems.append("%s.group must be 'any' or 'all', got %r"
                                % (label, rule.get("group")))
            problems.extend(check_rules(rule.get("of") or [], known_signals,
                                        label + ".of"))
            continue
        name = str(rule.get("signal") or "").split(".")[0]
        if not name:
            problems.append("%s names no signal" % label)
        elif name not in known_signals:
            problems.append("filter refers to unknown signal %r" % name)
        op = rule.get("op", "==")
        if op not in OPS and op not in ORDERING:
            problems.append("%s uses operator %r; use one of %s"
                            % (label, op, ", ".join(list(OPS) + list(ORDERING))))
    return problems


def check_limits(lim) -> List[str]:
    if lim is None:
        return []
    if not isinstance(lim, dict):
        return ["limits must be a mapping"]
    problems = _unknown("limits.", lim, LIMIT_KEYS)
    for key in ("max_results", "max_requests", "attempts"):
        if key in lim and lim[key] is not None and not _positive_int(lim[key]):
            # Zero is refused rather than read as "no cap": the pipeline has
            # always treated 0 as unset, and a user writing 0 means something.
            problems.append("limits.%s must be a whole number above 0, or left out"
                            % key)
    if "max_runtime" in lim and "max_runtime_seconds" in lim \
            and parse_duration(lim["max_runtime"]) != parse_duration(lim["max_runtime_seconds"]):
        problems.append("limits sets both max_runtime and max_runtime_seconds "
                        "to different values; keep one")
    for key in ("max_runtime", "max_runtime_seconds"):
        if key in lim and lim[key] is not None:
            secs = parse_duration(lim[key])
            if secs is None or secs <= 0:
                problems.append("limits.%s must be a positive duration like "
                                "1800 or 30m, got %r" % (key, lim[key]))
    workers = lim.get("workers")
    if workers is not None:
        if not isinstance(workers, dict):
            problems.append("limits.workers must be a mapping like {discover: 3}")
        else:
            for k, v in workers.items():
                if not _positive_int(v):
                    problems.append("limits.workers.%s must be a whole number above 0" % k)
    breaker = lim.get("breaker")
    if breaker is not None:
        if not isinstance(breaker, dict):
            problems.append("limits.breaker must be a mapping of rate, window, min_sample")
        else:
            problems.extend(_unknown("limits.breaker.", breaker, BREAKER_KEYS))
    return problems


def durable_problem(campaign: "Campaign") -> Optional[str]:
    """Why this campaign cannot run durably, or None.

    A durable run makes each place a unit of work, from ONE source. It used to
    take the first source silently -- dropping any others -- and would try to
    run a file source place by place, which fails every unit.
    """
    from . import sources as src
    if not campaign.places:
        return ("a durable run needs places -- it makes each one a unit of work. "
                "A file source is a single unit; run it normally.")
    ids = [_source_id(s) for s in campaign.sources
           if not (isinstance(s, dict) and s.get("enabled") is False)]
    if len(ids) != 1:
        return ("a durable run collects from exactly one source; this campaign "
                "lists %d (%s)" % (len(ids), ", ".join(map(str, ids))))
    try:
        takes = src.get(ids[0]).takes
    except KeyError:
        return "unknown source %r" % ids[0]
    if takes == "path":
        return ("%s reads a file, which is a single unit of work; run it "
                "normally rather than durably" % ids[0])
    return None


def validate(d: Dict[str, Any]) -> List[str]:
    """Problems a user can fix, in their words. Empty list means valid.

    Deliberately returns everything wrong at once rather than raising on the
    first fault -- a builder that reveals one error per attempt is a bad tool.
    And it never raises: a malformed campaign sent over HTTP used to answer
    500 from here instead of saying what was wrong with it.
    """
    from . import signals as sig
    from . import sources as src
    from .packs import library
    from .scoring import check_bands, check_weight

    if not isinstance(d, dict):
        return ["a campaign must be a mapping of settings"]
    problems: List[str] = _unknown("", d, CAMPAIGN_KEYS)
    lib = library()

    def section(key: str, allowed: Optional[set] = None) -> Dict[str, Any]:
        value = d.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            problems.append("%s must be a mapping" % key)
            return {}
        if allowed is not None:
            problems.extend(_unknown(key + ".", value, allowed))
        return value

    where = section("where", WHERE_KEYS)
    what = section("what", WHAT_KEYS)
    scoring = section("scoring", SCORING_KEYS)
    suppress = section("suppress", SUPPRESS_KEYS)
    output = section("output", OUTPUT_KEYS)
    gating = section("gating", TIERS)

    # -- sources --------------------------------------------------------
    raw_sources = d.get("sources")
    if raw_sources is not None and not isinstance(raw_sources, list):
        problems.append("sources must be a list, like [{id: csv, options: {path: x.csv}}]")
        raw_sources = []
    # Mirror from_dict's default: no sources means overpass.
    specs = raw_sources or [{"id": "overpass"}]
    registered = []
    for spec in specs:
        sid = _source_id(spec)
        if not sid:
            problems.append("each source needs an id, got %r" % (spec,))
            continue
        try:
            reg = src.get(sid)
        except KeyError:
            problems.append("unknown source %r" % sid)
            continue
        registered.append((spec, reg))
        # A source that cannot start is a problem you want at validation time,
        # not forty places into a run.
        opts = (spec.get("options") if isinstance(spec, dict) else None) or {}
        if not isinstance(opts, dict):
            problems.append("sources[%s].options must be a mapping" % sid)
        elif reg.takes == "path" and not str(opts.get("path") or "").strip():
            problems.append("%s needs a file: set options.path" % sid)

    place_based = [reg for _, reg in registered if reg.takes != "path"]
    # An unrecognised source is treated as place-based for the places check,
    # so a typo in the id does not also hide the missing places.
    unknown_ids = len(specs) - len(registered)

    # -- what -----------------------------------------------------------
    custom = what.get("custom") or {}
    named = bool(what.get("packs") or what.get("trade")
                 or (isinstance(custom, dict) and custom.get("label")))
    if isinstance(what.get("trade"), str) and not what["trade"].strip():
        problems.append("what.trade is empty -- name the trade to look for.")
        named = False
    if what.get("match") not in (None, "any", "all"):
        problems.append("what.match must be 'any' or 'all', got %r" % what.get("match"))
    packs = what.get("packs") or []
    if not isinstance(packs, list):
        problems.append("what.packs must be a list")
        packs = []
    for pid in packs:
        if pid not in lib:
            problems.append("unknown trade pack %r" % pid)

    if not named:
        # Only a campaign that actually NEEDS a trade is missing one. Scoring a
        # file purely on reviews and web presence is a legitimate brief with no
        # trade in it at all.
        filters_ = d.get("filters") if isinstance(d.get("filters"), list) else []
        by_filter = any(isinstance(f, dict) and f.get("signal") == "trade_match"
                        for f in filters_)
        by_weight = "trade_match" in (scoring.get("weights") or {}) \
            if isinstance(scoring.get("weights"), dict) else False
        if by_filter or by_weight:
            problems.append(
                "what: name a trade -- pick a pack or type one (what.trade: hospitals). "
                "trade_match is in use and has nothing to check against.")
        elif place_based:
            # A source that searches by trade cannot even build its query
            # without one; it raises at collection time, far too late to hear.
            problems.append(
                "what: name a trade -- pick a pack or type one (what.trade: hospitals). "
                "%s searches by trade and cannot build a query without one."
                % place_based[0].id)

    # -- where ----------------------------------------------------------
    place_packs = where.get("packs") or []
    if not isinstance(place_packs, list):
        problems.append("where.packs must be a list")
        place_packs = []
    for pid in place_packs:
        if pid not in lib:
            problems.append("unknown place pack %r" % pid)
    if where.get("mode") not in (None,) and where.get("mode") not in WHERE_MODES:
        problems.append("where.mode must be one of %s, got %r"
                        % (", ".join(sorted(WHERE_MODES)), where.get("mode")))
    if str(where.get("order", "as-listed")).lower() not in ORDERS:
        problems.append("where.order must be as-listed, priority, alphabetical or "
                        "random; got %r" % where.get("order"))
    if not (where.get("places") or place_packs or where.get("place")):
        if place_based or unknown_ids:
            problems.append("no places given -- set where.places, where.packs or where.place")

    # -- conditions and ranking -------------------------------------------
    known = {r.name for r in sig.all_signals()}
    problems.extend(check_rules(d.get("filters"), known))
    problems.extend(check_bands(scoring.get("bands")))
    weights = scoring.get("weights")
    if weights is not None and not isinstance(weights, dict):
        problems.append("scoring.weights must be a mapping of signal -> weight")
    else:
        for name, spec in (weights or {}).items():
            if str(name).split(".")[0] not in known:
                problems.append("scoring refers to unknown signal %r" % name)
            problems.extend(check_weight(name, spec))

    for tier, names in gating.items():
        if not isinstance(names, list):
            problems.append("gating.%s must be a list of signal names" % tier)
            continue
        for name in names:
            if name not in known:
                problems.append("gating.%s names unknown signal %r" % (tier, name))

    # -- the rest ---------------------------------------------------------
    problems.extend(check_limits(d.get("limits")))
    problems.extend(check_output(output))
    for key in ("lists", "cids", "runs"):
        if key in suppress and not isinstance(suppress[key], list):
            problems.append("suppress.%s must be a list" % key)
    if suppress.get("after") is not None and parse_duration(suppress["after"]) is None:
        problems.append("suppress.after must be a duration like 90d, got %r"
                        % suppress["after"])
    return problems
