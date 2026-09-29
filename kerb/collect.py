"""The durable collector.

Everything that makes a long collection survive lives here rather than in any
fetcher, because none of it is specific to what is being fetched. Give it a
list of units and a function that turns one unit into businesses, and it will:

  * keep the queue on disk, so the process can die at any instant
  * hand each unit to one worker under a lease, so a dead worker's unit
    returns to the queue instead of being lost
  * write results and mark the unit done in one transaction, so a crash can
    neither duplicate nor drop work
  * retry a failed unit with backoff, a bounded number of times, and keep the
    reason when it finally gives up
  * pace every worker through one shared rate limiter, per host
  * stop the whole run when failure becomes the norm, rather than grinding the
    remaining units into permanent failures
  * notice when a source stops erroring and starts quietly returning less

The fetcher itself stays a plain function. That is deliberate: the awkward,
fragile, site-specific part should be the part with the least responsibility.

    def fetch(unit, ctx):        # a place name, a cid, a URL -- anything
        yield Business(...)

    result = collect(store, run_id, "discover", units, fetch, workers=6)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from .health import DiscoveryHealth, FailureBreaker
from .models import Business
from .store import Store

# Measured, not guessed: on the benchmark this project came from, 6 and 10
# workers finished the same 60-search job in the same 121 seconds, so 6 is the
# point past which more concurrency buys nothing and only adds exposure.
DEFAULT_WORKERS = 6


class RateLimiter:
    """One shared token bucket, so N workers stay polite in aggregate.

    Per-worker delays do not work: six workers each "waiting a second" still
    produce six requests a second. The limiter has to be shared, which is the
    whole reason it is here and not in the fetcher.
    """

    def __init__(self, per_second: float = 1.0, burst: int = 1):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        # The configured rate is a ceiling, not a starting point. `ease` used to
        # decay the interval with no floor, so a limiter told "20 a second"
        # crept past it after a few dozen successes and quietly became the
        # opposite of a rate limit.
        self.base_interval = self.interval
        self.burst = max(1, burst)
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> float:
        if self.interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        wait = start - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        return max(0.0, wait)

    def slow_down(self, factor: float = 2.0, cap: float = 60.0) -> None:
        """Back off after being told to. Politeness on the way up is cheap;
        being throttled because we ignored a 429 is not."""
        with self._lock:
            self.interval = min(cap, max(self.interval, 0.05) * factor)

    def ease(self, factor: float = 0.9) -> None:
        """Recover toward the configured rate after a back-off -- never past it."""
        with self._lock:
            if self.interval > self.base_interval:
                self.interval = max(self.base_interval, self.interval * factor)


@dataclass
class CollectResult:
    run_id: str
    kind: str
    # Rows the source handed back, and rows that were genuinely new. They differ
    # whenever search areas overlap, and quoting the first as the haul overstates
    # a run by exactly that overlap.
    fetched: int = 0
    collected: int = 0
    units_done: int = 0
    units_failed: int = 0
    stopped: Optional[str] = None
    health: Dict[str, Any] = field(default_factory=dict)
    failures: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.stopped is None and self.units_failed == 0

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "kind": self.kind,
                "fetched": self.fetched, "collected": self.collected,
                "duplicates": self.fetched - self.collected,
                "units_done": self.units_done,
                "units_failed": self.units_failed, "stopped": self.stopped,
                "complete": self.complete, "health": self.health,
                "failures": self.failures}


class Retryable(Exception):
    """Worth another attempt: a timeout, a 5xx, a rate limit."""


class Fatal(Exception):
    """Not worth retrying: a bad query, a 404, an auth failure.

    Retrying these three more times wastes the budget and, on a rate-limited
    source, actively makes things worse.
    """


class Halt(RuntimeError):
    """Stop the whole collection, and give this unit back untouched.

    For trouble that is about the SOURCE rather than the unit: a block on the
    address, a response shape the parser no longer understands. Every other
    unit would hit it too, so grinding on only burns attempts -- and marking
    the unit done or failed would lose it. It goes back to the queue, and a
    resume picks it up once the cause has passed.
    """


ProgressFn = Callable[[Dict[str, Any]], None]
FetchFn = Callable[[str, Dict[str, Any]], Iterable[Business]]


def _noop(_: Dict[str, Any]) -> None:
    pass


def collect(store: Store, run_id: str, kind: str, units: Iterable[str],
            fetch: FetchFn, workers: int = DEFAULT_WORKERS,
            rate: Optional[RateLimiter] = None,
            on_progress: ProgressFn = _noop,
            should_stop: Optional[threading.Event] = None,
            payload: Optional[Dict[str, Any]] = None,
            breaker: Optional[FailureBreaker] = None,
            health: Optional[DiscoveryHealth] = None,
            attempts: Optional[int] = None) -> CollectResult:
    """Run `fetch` over `units`, durably.

    Safe to call again with the same run_id and units: enqueueing is
    idempotent and finished units are skipped, so resuming after a crash is
    the same call.
    """
    units = list(units)
    store.add_tasks(run_id, kind, units, payload)
    reclaimed = store.reclaim_all(run_id)
    if reclaimed:
        on_progress({"stage": "reclaim", "kind": kind, "tasks": reclaimed,
                     "note": "returned to the queue from a previous attempt"})

    limiter = rate or RateLimiter(per_second=1.0)
    breaker = breaker or FailureBreaker()
    health = health or DiscoveryHealth()
    stop = should_stop or threading.Event()
    result = CollectResult(run_id=run_id, kind=kind)
    lock = threading.Lock()

    def worker(n: int) -> None:
        while not stop.is_set():
            task = store.claim(run_id, kind)
            if task is None:
                return
            if stop.is_set():
                store.release(task["id"])
                return

            unit = task["key"]
            ctx = {"payload": task["payload"], "attempt": task["attempts"],
                   "worker": n, "limiter": limiter}
            try:
                limiter.acquire()
                rows = [b.to_dict() if isinstance(b, Business) else b
                        for b in fetch(unit, ctx)]
            except Halt as exc:
                store.release(task["id"])
                with lock:
                    if not result.stopped:
                        result.stopped = str(exc)
                stop.set()
                on_progress({"stage": "halt", "unit": unit, "reason": str(exc)})
                return
            except Fatal as exc:
                store.fail(task["id"], "%s: %s" % (type(exc).__name__, exc),
                           max_attempts=1)
                with lock:
                    result.units_failed += 1
                on_progress({"stage": "unit_failed", "unit": unit,
                             "error": str(exc), "retryable": False})
                if breaker.record(False):
                    _trip(result, breaker, stop, on_progress)
                continue
            except Exception as exc:               # noqa: BLE001 -- retryable
                detail = "%s: %s" % (type(exc).__name__, exc)
                state = (store.fail(task["id"], detail, max_attempts=attempts)
                         if attempts else store.fail(task["id"], detail))
                if _looks_throttled(exc):
                    limiter.slow_down()
                with lock:
                    if state == "failed":
                        result.units_failed += 1
                on_progress({"stage": "unit_error", "unit": unit,
                             "error": detail, "state": state,
                             "attempt": task["attempts"]})
                if breaker.record(False):
                    _trip(result, breaker, stop, on_progress)
                continue

            # Results and "done" land together. Nothing can crash between them.
            new = store.complete(task["id"], rows, run_id=run_id)
            with lock:
                result.fetched += len(rows)
                result.collected += new
                result.units_done += 1
                warning = health.record(unit, len(rows))
            limiter.ease()
            on_progress({"stage": "unit_done", "unit": unit, "found": len(rows),
                         "new": new, "done": result.units_done,
                         "total": len(units)})
            if warning:
                on_progress({"stage": "yield_suspect", "unit": unit,
                             "warning": warning})
            if health.should_back_off():
                limiter.slow_down(factor=1.5)
                on_progress({"stage": "backing_off", "verdict": health.verdict(),
                             "advice": health.advice()})
            if breaker.record(True):               # pragma: no cover
                _trip(result, breaker, stop, on_progress)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True,
                                name="kerb-collect-%d" % i)
               for i in range(max(1, workers))]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join(timeout=10)
        result.stopped = result.stopped or "interrupted"

    result.health = health.summary() if health.counts else {}
    # This collection's failures only. The run may hold other kinds of task --
    # a requalify pass, a second source -- and counting theirs here would
    # blame this collection for work it never did.
    result.failures = [f for f in store.failures(run_id) if f.get("kind") == kind]
    result.units_failed = len(result.failures)
    return result


def _trip(result: CollectResult, breaker: FailureBreaker,
          stop: threading.Event, on_progress: ProgressFn) -> None:
    result.stopped = breaker.message()
    stop.set()
    on_progress({"stage": "breaker", "reason": result.stopped,
                 "fail_rate": round(breaker.fail_rate(), 3)})


_THROTTLE_HINTS = ("429", "too many requests", "rate limit", "503", "slow down",
                   "quota", "throttl")


def _looks_throttled(exc: Exception) -> bool:
    text = ("%s %s" % (type(exc).__name__, exc)).lower()
    return any(h in text for h in _THROTTLE_HINTS)
