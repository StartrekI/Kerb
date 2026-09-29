"""The pipeline: source -> resolve -> gate -> score.

The gate ordering is the economics of the whole product, so it is worth being
explicit about what it does:

    FREE       arithmetic on data already held         rejects the majority
    CHEAP      one request per business
    EXPENSIVE  many requests, a browser, or an LLM

Each tier only ever sees what survived the one before. On a real production
run 74% of candidates were rejected before the expensive stage ran at all.

A tool that charges per record cannot do this -- filtering before fetching
would cannibalise its own revenue -- which is why the ordering is a moat and
not merely a tidy implementation.

Progress is reported through a callback rather than printed, so the CLI, the
API's SSE stream and the tests can all watch the same run without the pipeline
knowing which is listening.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from . import scoring, signals, sources
from .campaign import Campaign
from .health import (FAIL_MIN_SAMPLE, FAIL_RATE, FAIL_WINDOW, DiscoveryHealth,
                     FailureBreaker)
from .models import Business, Cost, Outcome, Signal, SourceQuery, Verdict


@dataclass
class RunStats:
    discovered: int = 0
    deduped: int = 0
    qualified: int = 0
    rejected: int = 0
    # Businesses we never found out about, because a measurement broke. Kept
    # apart from `rejected` on purpose: one is a verdict, the other is a gap.
    unevaluated: int = 0
    resumed: int = 0
    # Businesses withheld because they were already dealt with, or already
    # seen with the same verdict. Counted so "fewer results" is never a
    # mystery -- the user must be able to see what was hidden and why.
    suppressed: int = 0
    stopped_reason: Optional[str] = None
    rejected_by: Dict[str, int] = field(default_factory=dict)
    # A source that failed, and why. Surfaced rather than swallowed: "I got
    # 40 results" means something different when one of three sources was down.
    source_errors: Dict[str, str] = field(default_factory=dict)
    # Places a source could not read but survived. Partial coverage is not the
    # same as no results, and the user has to be able to tell them apart.
    skipped_places: Dict[str, str] = field(default_factory=dict)
    # Places that were read and held nothing. Not a failure -- a village has no
    # roofer -- but still worth seeing, because a list of them is also what a
    # throttled source looks like.
    empty_places: Dict[str, str] = field(default_factory=dict)
    # Things a source wants the user told before they act on the results, such
    # as "signed out of Google: review data is missing".
    notes: List[str] = field(default_factory=list)
    discovery_health: Dict[str, Any] = field(default_factory=dict)
    suppression: Dict[str, Any] = field(default_factory=dict)
    requests: int = 0
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started

    @property
    def qualify_rate(self) -> float:
        # Unevaluated businesses are excluded from the denominator. Counting a
        # business we never measured as a miss understates the qualify rate by
        # exactly the size of the outage.
        seen = self.qualified + self.rejected
        return (self.qualified / seen) if seen else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "discovered": self.discovered, "deduped": self.deduped,
            "qualified": self.qualified, "rejected": self.rejected,
            "unevaluated": self.unevaluated,
            "resumed": self.resumed,
            "suppressed": self.suppressed,
            "suppression": dict(self.suppression or {}),
            "stopped_reason": self.stopped_reason,
            "rejected_by": dict(sorted(self.rejected_by.items(),
                                       key=lambda kv: -kv[1])),
            "source_errors": dict(self.source_errors),
            "skipped_places": dict(self.skipped_places),
            "empty_places": dict(self.empty_places),
            "notes": list(self.notes),
            "discovery_health": dict(self.discovery_health or {}),
            "requests": self.requests,
            "elapsed_seconds": round(self.elapsed, 2),
            "qualify_rate": round(self.qualify_rate, 4),
            # Below a second the divisor is mostly timer noise, and a run that
            # took 1ms is not really going at 728,000 records a minute. Report
            # nothing rather than a number the user would be right to distrust.
            "per_minute": (round(self.deduped / self.elapsed * 60, 1)
                           if self.elapsed >= 1.0 else None),
        }


ProgressFn = Callable[[Dict[str, Any]], None]


def _noop(_: Dict[str, Any]) -> None:
    pass


def terminal_status(stats: Dict[str, Any], stopping: bool = False) -> str:
    """The terminal status a run has earned, from its stats.

    `done` has to mean finished AND successful. A source that died or stopped
    itself, a place that could not be read, or a cap or breaker that cut the
    run short all land on `partial` -- otherwise the caller believes they saw
    everything there was. A place that was read and held nothing is NOT a
    failure: a village with no roofer is a complete answer.
    """
    if stopping:
        return "stopped"
    if (stats.get("source_errors") or stats.get("skipped_places")
            or stats.get("stopped_reason")):
        return "partial"
    return "done"


class Pipeline:
    def __init__(self, campaign: Campaign, on_progress: ProgressFn = _noop,
                 skip: Optional[Iterable[str]] = None,
                 suppression=None,
                 reuse: Optional[Dict[str, Dict[str, Signal]]] = None):
        self.c = campaign
        # cid -> {signal name: Signal} already measured, for re-judging a
        # stored run without refetching anything. See _measure.
        self._reuse = {str(k).lower(): v for k, v in (reuse or {}).items()}
        self.on_progress = on_progress
        self.stats = RunStats()
        self.ctx = signals.Context(options=campaign.signal_options)
        # A typed trade joins a COPY of the library, never the shared singleton:
        # the server runs many campaigns and one run's trade must not leak into
        # the next.
        typed = campaign.typed_pack
        if typed:
            self.ctx.packs = self.ctx.packs.with_pack(typed)
        # Pre-seeding the dedupe set is what makes a run resumable: cids from
        # a previous attempt are treated as already-seen, so a crash at 90%
        # costs the last few rather than the whole day.
        self._seen: set = {str(c).lower() for c in (skip or ())}
        self._source_requests = 0
        self.stats.resumed = len(self._seen)
        self.suppression = suppression
        # Watches the run for the two failures that do not announce themselves:
        # a source quietly serving less than it has, and a window of broken
        # measurements being written down as rejections.
        self.health = DiscoveryHealth()
        self.breaker = FailureBreaker(
            rate=float((campaign.limits.breaker or {}).get("rate", FAIL_RATE)),
            window=int((campaign.limits.breaker or {}).get("window", FAIL_WINDOW)),
            min_sample=int((campaign.limits.breaker or {}).get("min_sample",
                                                              FAIL_MIN_SAMPLE)))

    def _over_budget(self, query: Optional[SourceQuery] = None) -> bool:
        """Live request total against the cap.

        Sources report into their own query as they go, so the in-flight one is
        added separately from the sources already finished.
        """
        live = int((query.report.get("requests") or 0)) if query else 0
        self.stats.requests = self._source_requests + self.ctx.requests + live
        cap = self.c.limits.max_requests
        if cap and self.stats.requests >= cap:
            if not self.stats.stopped_reason:
                self.stats.stopped_reason = (
                    "request budget reached (%d of %d)" % (self.stats.requests, cap))
            return True
        return False

    # -- stages ----------------------------------------------------------

    def discover(self) -> Iterator[Business]:
        """Every enabled source, deduplicated on cid as results arrive.

        Dedupe happens here rather than at the end so a business found by three
        sources costs the expensive stages once, not three times.

        Each source is isolated. A source that dies -- a network outage, a
        missing file, a rate limit -- costs its own results and nothing else;
        the failure is recorded and the next source still runs. Previously one
        unreachable source aborted the whole run and discarded everything the
        other sources had already produced.
        """
        places = self.c.places
        for spec in self.c.sources:
            sid = spec.get("id") if isinstance(spec, dict) else spec
            if isinstance(spec, dict) and spec.get("enabled") is False:
                continue
            opts = (spec.get("options") if isinstance(spec, dict) else {}) or {}
            # A source that can parallelise should be told how much it may.
            # Without this, limits.workers was a setting the collector never saw.
            opts = dict(opts)
            opts.setdefault("workers", self.c.limits.workers.get("discover", 1))
            # A trade pack's OSM tags -- typed (`what.osm_tags`) or curated --
            # reach the source unless the source options name their own. They
            # were parsed into the pack and never passed on, while the overpass
            # error message told users to set exactly them.
            if "tags" not in opts:
                tags = self._pack_osm_tags()
                if tags:
                    opts["tags"] = tags
            query = SourceQuery(what=self.c.trade, places=places,
                                path=opts.get("path"), limit=self.c.limits.max_results,
                                options=opts)
            if self._over_budget(query):
                break
            self.on_progress({"stage": "discover", "source": sid, "status": "start"})
            per_place: Dict[str, int] = {}
            stream = None
            try:
                stream = sources.fetch(sid, query)
                for biz in stream:
                    self.stats.discovered += 1
                    if biz.place_label:
                        per_place[biz.place_label] = per_place.get(biz.place_label, 0) + 1
                    # Checked here, not only in the consumer loop. Discovery is
                    # where the requests are actually spent, and a source that
                    # dedupes heavily can burn a whole budget while yielding
                    # almost nothing -- so a check driven by yields never fires.
                    if self._over_budget(query):
                        self.on_progress({"stage": "stop",
                                          "reason": self.stats.stopped_reason})
                        break
                    if biz.cid in self._seen:
                        continue
                    self._seen.add(biz.cid)
                    # Hard suppression happens HERE, not after judging: a
                    # business the user has already contacted should not cost
                    # a single request to re-confirm.
                    if self.suppression is not None and self.suppression.hide(biz.cid):
                        self.stats.suppressed += 1
                        continue
                    self.stats.deduped += 1
                    if self.stats.deduped % 100 == 0:
                        self.on_progress({"stage": "discover", "source": sid,
                                          "found": self.stats.deduped})
                    yield biz
            except Exception as exc:               # noqa: BLE001
                detail = "%s: %s" % (type(exc).__name__, exc)
                self.stats.source_errors[str(sid)] = detail
                self.on_progress({"stage": "source_error", "source": sid,
                                  "error": detail})
            finally:
                # Closed explicitly, not left to garbage collection. A source
                # with worker threads stops them when it is closed, and a
                # `break` above -- the request budget -- must reach it now,
                # not whenever the interpreter gets round to it.
                close = getattr(stream, "close", None)
                if close is not None:
                    close()

            # Requests the source actually made, so the spend cap is checked
            # against a real number rather than a counter nothing ever wrote to.
            self._source_requests += int(query.report.get("requests") or 0)
            self.stats.requests = self._source_requests + self.ctx.requests

            # Everything the source reported, read BEFORE any budget check can
            # leave the loop. Every key of SourceQuery.report is read here: a
            # Google block used to be written into `fatal`, read by nobody,
            # and the run finished as a clean, complete, empty "done".
            self._fold_report(sid, query.report, per_place)
            if self._over_budget():
                break               # also skips the health check below: a place
                                    # cut short by the budget is not a collapse

            # Per-place yields, for the degradation check. Only meaningful for
            # place-based discovery: a file has one "search" and no baseline.
            if len(per_place) >= self.health.warmup:
                for place in (places or list(per_place)):
                    if place in per_place:
                        warning = self.health.record(place, per_place[place])
                        if warning:
                            self.on_progress({"stage": "discover_suspect",
                                              "source": sid, "place": place,
                                              "warning": warning})
                if self.health.should_back_off():
                    self.on_progress({
                        "stage": "discover_degraded", "source": sid,
                        "verdict": self.health.verdict(),
                        "advice": self.health.advice()})

    def _pack_osm_tags(self) -> List[str]:
        tags: List[str] = []
        for pid in self.ctx.opt("trade_match", "packs") or []:
            pack = self.ctx.packs.maybe(pid)
            for tag in (pack.list("osm_tags") if pack else []):
                if tag not in tags:
                    tags.append(tag)
        return tags

    def _fold_report(self, sid, report: Dict[str, Any],
                     per_place: Dict[str, int]) -> None:
        """Fold one source's SourceQuery.report into the run's stats.

        Partial trouble the source survived is reported even on success,
        because "40 results" means something different when a tenth of the
        search area could not be read -- or when the source stopped itself
        because Google blocked the address.
        """
        failed = report.get("failed_places") or {}
        for place, why in failed.items():
            self.stats.skipped_places["%s/%s" % (sid, place)] = why
            per_place.setdefault(place, 0)          # a dead place yielded zero
        if failed:
            self.on_progress({"stage": "source_partial", "source": sid,
                              "skipped": len(failed)})

        for place, why in (report.get("empty_places") or {}).items():
            self.stats.empty_places["%s/%s" % (sid, place)] = why
            per_place.setdefault(place, 0)

        fatal = report.get("fatal")
        if fatal:
            earlier = self.stats.source_errors.get(str(sid))
            self.stats.source_errors[str(sid)] = (
                "%s; %s" % (earlier, fatal) if earlier else str(fatal))
            self.on_progress({"stage": "source_error", "source": sid,
                              "error": str(fatal)})

        for note in report.get("notes") or []:
            line = "%s: %s" % (sid, note)
            if line not in self.stats.notes:
                self.stats.notes.append(line)
                self.on_progress({"stage": "source_note", "source": sid,
                                  "note": str(note)})

    def _seed_chain_counts(self, businesses: List[Business]) -> None:
        """Tally brand names across the whole set.

        Only possible when the set is materialised. A streaming run cannot know
        that the business in front of it is one of forty branches until it has
        seen the other thirty-nine, so `chain_size` falls back to the chains
        pack there and says so in its confidence -- an order-dependent count
        would make the same dataset produce different verdicts on each run.
        """
        from collections import Counter
        from .signals.shape import chain_key, known_chains
        counts = Counter(chain_key(b.name) for b in businesses if b.name)
        opts = dict(self.ctx.options.get("chain_size") or {})
        opts.setdefault("counts", counts)
        # One parse of the chains pack, shared with the streaming path -- this
        # used to be a second copy of the same loop.
        opts.setdefault("known", known_chains(self.ctx.packs.maybe("chains/known")))
        self.ctx.options["chain_size"] = opts

    def qualify(self, businesses: Iterable[Business]) -> Iterator[Verdict]:
        """Gate and score businesses someone already collected.

        Exactly the tiering `run` applies, minus discovery. The durable path
        needs this because there collection and qualification are separate
        phases: fetching is what fails and is worth a ledger, while judging is
        arithmetic over what was already fetched and can simply be redone.
        """
        materialised = list(businesses)
        self._seed_chain_counts(materialised)
        yield from self._gate(materialised)

    def run(self) -> Iterator[Verdict]:
        """Discover, then gate and score.

        Rejected businesses are yielded too, carrying the reason. A run that
        silently drops them cannot answer "why did I only get 12 results",
        which is the first question anyone asks.
        """
        yield from self._gate(self.discover())

    def _gate(self, businesses: Iterable[Business]) -> Iterator[Verdict]:
        # The input is closed however this generator ends -- finished, broken
        # out of by a cap or the breaker, or abandoned by ITS consumer (the
        # API's Stop button). Discovery is a chain of generators down to a
        # source that may own worker threads; leaving the close to garbage
        # collection meant those threads kept fetching after the run was over.
        try:
            yield from self._judge(businesses)
        finally:
            close = getattr(businesses, "close", None)
            if close is not None:
                close()

    def _measure(self, name: str, biz: Business, tier: Cost) -> Signal:
        """One signal for one business -- or the stored one, when re-judging.

        Re-judging a stored run recomputes the FREE tier, because that is
        where packs and rules live and a changed rule is the reason to re-judge.
        Paid signals are reused as they were measured: recomputing them would
        refetch every website -- and reopen a browser -- to answer a question
        about rules, which is exactly what `requalify` promises not to do.
        """
        if tier is not Cost.FREE and self._reuse:
            stored = (self._reuse.get(biz.cid) or {}).get(name)
            if stored is not None:
                return stored
        return signals.compute(name, biz, self.ctx)

    def _judge(self, businesses: Iterable[Business]) -> Iterator[Verdict]:
        free = self.c.signals_for(Cost.FREE)
        cheap = self.c.signals_for(Cost.CHEAP)
        expensive = self.c.signals_for(Cost.EXPENSIVE)
        weights = self.c.weights
        normalise = (self.c.scoring or {}).get("normalise", True) is not False
        use_conf = bool((self.c.scoring or {}).get("confidence"))
        bands = (self.c.scoring or {}).get("bands")
        cap = self.c.limits.max_results
        deadline = (self.stats.started + self.c.limits.max_runtime_seconds
                    if self.c.limits.max_runtime_seconds else None)

        for biz in businesses:
            if deadline and time.time() > deadline:
                self.stats.stopped_reason = "runtime budget reached"
                self.on_progress({"stage": "stop", "reason": self.stats.stopped_reason})
                break
            # Signals that fetch report through the context; both they and the
            # sources spend against the one budget.
            if self._over_budget():
                self.on_progress({"stage": "stop", "reason": self.stats.stopped_reason})
                break

            verdict = Verdict(business=biz)

            # Tier by tier, stopping the moment a filter is failed. This is
            # what makes the expensive tier cheap.
            settled = False
            measured: set = set()
            for cost, tier in ((Cost.FREE, free), (Cost.CHEAP, cheap),
                               (Cost.EXPENSIVE, expensive)):
                for name in tier:
                    verdict.add(self._measure(name, biz, cost))
                    measured.add(name)
                # Only the signals measured so far may judge. A filter on a
                # tier that has not run yet must wait for it, not reject on the
                # None it would otherwise resolve to.
                ok, failed_by, reason = scoring.evaluate(
                    verdict, self.c.filters, available=measured)
                if not ok:
                    verdict.rejected_by = failed_by
                    verdict.reject_reason = reason
                    if str(failed_by).startswith("unmeasurable:"):
                        # We never found out. Not a verdict, and not counted
                        # against the qualify rate.
                        verdict.outcome = Outcome.UNEVALUATED
                        self.stats.unevaluated += 1
                    else:
                        verdict.outcome = Outcome.REJECTED
                        self.stats.rejected += 1
                        key = failed_by or "unknown"
                        self.stats.rejected_by[key] = \
                            self.stats.rejected_by.get(key, 0) + 1
                    settled = True
                    break

            if not settled:
                verdict.outcome = Outcome.QUALIFIED
                scoring.score(verdict, weights, normalise, confidence=use_conf)
                verdict.band = scoring.band_for(verdict.score, bands)
                self.stats.qualified += 1
                self.on_progress({"stage": "qualified", "name": biz.display,
                                  "score": verdict.score,
                                  "total": self.stats.qualified})

            # Soft suppression can only be decided now, because it depends on
            # whether the verdict CHANGED. A business seen last week whose site
            # has since died is news, not a duplicate.
            if self.suppression is not None and self.suppression.hide_verdict(
                    biz.cid, verdict.outcome.value):
                self.stats.suppressed += 1
                self.breaker.record(not verdict.failed_signals)
                continue

            # One business, one vote: did every measurement we attempted work?
            # A tripped breaker means the window has closed, and continuing
            # would convert the rest of the dataset into permanent rejections.
            if self.breaker.record(not verdict.failed_signals):
                self.stats.stopped_reason = self.breaker.message()
                self.on_progress({"stage": "breaker",
                                  "fail_rate": round(self.breaker.fail_rate(), 3),
                                  "reason": self.stats.stopped_reason})
                yield verdict
                break

            yield verdict

            if cap and self.stats.qualified >= cap:
                self.stats.stopped_reason = "result cap reached"
                self.on_progress({"stage": "stop", "reason": "result cap reached"})
                break

        self.stats.requests = self._source_requests + self.ctx.requests
        if self.suppression is not None:
            self.stats.suppression = self.suppression.to_dict()
        self.stats.finished = time.time()
        if self.health.counts:
            self.stats.discovery_health = self.health.summary()
        self.on_progress({"stage": "done", **self.stats.to_dict()})


def stored_signals(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Signal]]:
    """cid -> {name: Signal} from stored verdict rows, for Pipeline(reuse=...)."""
    out: Dict[str, Dict[str, Signal]] = {}
    for row in rows:
        cid = str(row.get("cid") or "").lower()
        if cid:
            out[cid] = {name: Signal.from_dict(name, data)
                        for name, data in (row.get("signals") or {}).items()}
    return out


def run(campaign: Campaign, on_progress: ProgressFn = _noop):
    """Convenience: run to completion, return (qualified, all, stats)."""
    p = Pipeline(campaign, on_progress)
    everything = list(p.run())
    qualified = sorted([v for v in everything if v.qualified],
                       key=lambda v: -(v.score or 0))
    return qualified, everything, p.stats
