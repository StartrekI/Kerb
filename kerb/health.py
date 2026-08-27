"""Watching a run for the failures that do not announce themselves.

Ported from the pipeline this project grew out of, where both of these were
learned the expensive way. Nothing here does I/O; both classes watch a sequence
of outcomes and answer one question each.

**DiscoveryHealth** — "is this run still getting what the source actually has?"
A source under throttling stops erroring and starts quietly serving less: HTTP
200, well-formed, most of the market missing. On the run that produced this
code, three searches returned 0 and four returned exactly 20 against a ~70
baseline, and the pipeline cheerfully reported zero failures.

**FailureBreaker** — "has the window closed?" This one matters more than it
looks. A failed measurement does not merely waste a request: the business is
recorded with the failure as its verdict, so a throttled window permanently
marks thousands of perfectly good businesses as rejected and nothing ever
revisits them. Stopping early leaves them unevaluated for the next attempt.

Run `python -m kerb.health` to execute the self-checks at the bottom.
"""

from __future__ import annotations

import statistics
from typing import Dict, List, Optional

# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

# A search must clear this to count toward the baseline at all.
BASELINE_MIN = 1
# Searches needed before any judgement is made. Below this there is no baseline
# to compare against and every verdict would be noise.
WARMUP = 3
# Result counts that are pagination artefacts rather than real totals.
PAGE_CAPS = (10, 20)
# A page-cap value only means anything if the baseline is well above it.
PAGE_CAP_HEADROOM = 2.0
# Recent yield below this fraction of baseline = collapse.
COLLAPSE_RATIO = 0.6
# How many recent searches the collapse check averages over.
WINDOW = 5


class DiscoveryHealth:
    """Watches per-search result counts and flags silent degradation.

    Every signal is relative to the run's OWN baseline rather than to a fixed
    number, because a genuinely thin market ("roofer in a village") looks
    exactly like throttling if you only look at one search.
    """

    def __init__(self, warmup: int = WARMUP, window: int = WINDOW):
        self.warmup = warmup
        self.window = window
        self.counts: List[int] = []
        self.labels: List[str] = []
        self.flags: List[Dict[str, object]] = []

    @property
    def baseline(self) -> Optional[float]:
        """What a HEALTHY search returns, or None while too few to judge.

        Median of the upper half rather than of everything: a plain median is
        contaminated the moment throttling affects half the run, which is
        exactly when the baseline matters most. On the real trace the plain
        median read 36 -- already halfway to the damage -- and scored a
        60%-loss run as merely "degraded". The upper half reads 58 and calls
        it throttled, which is what had happened.

        The cost is a mild optimism bias on a genuinely uneven market, which
        is the right way round: over-reporting a suspicion is cheap, silently
        shipping a half-empty lead list is not.
        """
        good = sorted(c for c in self.counts if c >= BASELINE_MIN)
        if len(good) < self.warmup:
            return None
        upper = good[len(good) // 2:] or good
        return statistics.median(upper)

    def record(self, label: str, count: int) -> Optional[str]:
        """Log one search. Returns a human-readable warning, or None."""
        self.counts.append(count)
        self.labels.append(label)

        base = self.baseline
        if base is None:
            return None
        reason = self._diagnose(count, base)
        if reason is None:
            return None
        self.flags.append({"label": label, "count": count,
                           "baseline": base, "reason": reason})
        return ("suspect: %s -- %d found, baseline %.0f (%s)"
                % (label, count, base, reason))

    def _diagnose(self, count: int, base: float) -> Optional[str]:
        if count == 0:
            return ("zero results where similar searches return ~%.0f; "
                    "likely throttling, not an empty market" % base)
        if count in PAGE_CAPS and base >= count * PAGE_CAP_HEADROOM:
            return ("exactly %d looks like a page cap, not a real total "
                    "(baseline ~%.0f)" % (count, base))
        if count < base * COLLAPSE_RATIO:
            return "yield %.0f%% of baseline" % (count / base * 100)
        return None

    def should_back_off(self) -> bool:
        """True when the RECENT window has collapsed.

        Window-based, not cumulative: one bad search early should not condemn
        a long healthy run.
        """
        base = self.baseline
        if base is None or len(self.counts) < self.window:
            return False
        recent = self.counts[-self.window:]
        return statistics.fmean(recent) < base * COLLAPSE_RATIO

    def yield_ratio(self) -> float:
        base = self.baseline
        if not base or not self.counts:
            return 1.0
        return sum(self.counts) / (base * len(self.counts))

    def verdict(self) -> str:
        if self.baseline is None:
            return "unknown"
        r = self.yield_ratio()
        if r >= 0.9:
            return "healthy"
        if r >= 0.6:
            return "degraded"
        return "throttled"

    def summary(self) -> Dict[str, object]:
        base = self.baseline
        total = len(self.counts)
        found = sum(self.counts)
        expected = base * total if base else None
        return {
            "searches": total,
            "found": found,
            "zero_results": sum(1 for c in self.counts if c == 0),
            "suspect": len(self.flags),
            "baseline": base,
            "expected_if_healthy": expected,
            "yield_pct": round(found / expected * 100, 1) if expected else None,
            "verdict": self.verdict(),
            "flagged": [f["label"] for f in self.flags],
        }

    def advice(self) -> Optional[str]:
        if self.verdict() in ("healthy", "unknown"):
            return None
        return ("%d of %d searches returned far less than this run's own "
                "baseline. The results are there; this session is not seeing "
                "them. Lower the worker count, wait, and re-run the flagged "
                "places." % (len(self.flags), len(self.counts)))


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

# Fail rate over the recent window that means "the window has closed", not
# "these particular businesses are broken".
FAIL_RATE = 0.4
# Below this there is not enough evidence -- three failures in the first three
# businesses is a bad batch, not a closed window.
FAIL_MIN_SAMPLE = 10
FAIL_WINDOW = 20


class FailureBreaker:
    """Trips when the RECENT failure rate says the window has closed.

    Streaming rather than chunked, because results arrive as they complete
    rather than in fixed batches.

    The reason this matters more than it looks: a failed measurement is
    recorded against the business, so a throttled window does not just waste
    time -- it marks thousands of perfectly good businesses as rejected, and
    nothing ever revisits them. Stopping early leaves them unevaluated.
    """

    def __init__(self, rate: float = FAIL_RATE, window: int = FAIL_WINDOW,
                 min_sample: int = FAIL_MIN_SAMPLE):
        self.rate = rate
        self.window = window
        self.min_sample = min_sample
        self.recent: List[bool] = []
        self.total = 0
        self.failures = 0
        self.tripped = False

    def record(self, ok: bool) -> bool:
        """Log one outcome. Returns True the moment the breaker trips."""
        self.total += 1
        if not ok:
            self.failures += 1
        self.recent.append(ok)
        if len(self.recent) > self.window:
            self.recent.pop(0)
        if self.tripped or len(self.recent) < self.min_sample:
            return False
        failed = sum(1 for r in self.recent if not r)
        if failed / len(self.recent) >= self.rate:
            self.tripped = True
            return True
        return False

    def fail_rate(self) -> float:
        if not self.recent:
            return 0.0
        return sum(1 for r in self.recent if not r) / len(self.recent)

    def message(self) -> str:
        return ("%.0f%% of the last %d businesses could not be measured -- "
                "this looks like throttling or an outage, not bad data. "
                "Stopping so the rest stay unevaluated instead of being "
                "recorded as rejected. Wait, then re-run the same command; "
                "nothing already collected is lost."
                % (self.fail_rate() * 100, len(self.recent)))


# --------------------------------------------------------------------------

def _selfcheck() -> None:
    # -- DiscoveryHealth ---------------------------------------------------

    # 1. A healthy run raises nothing.
    h = DiscoveryHealth()
    for i, n in enumerate([78, 77, 80, 76, 79, 74]):
        assert h.record("s%d" % i, n) is None, "healthy run flagged at %d" % n
    assert h.verdict() == "healthy", h.verdict()
    assert not h.should_back_off()
    assert h.advice() is None

    # 2. The real throttled trace: zeros and 20s against a ~70 baseline. A
    #    plain median would read 36 here and call this merely "degraded".
    h = DiscoveryHealth()
    warnings = [h.record("s%d" % i, n)
                for i, n in enumerate([58, 59, 74, 20, 0, 20, 0, 36, 0, 20])]
    assert h.baseline >= 55, "baseline contaminated by the damage: %s" % h.baseline
    assert h.verdict() == "throttled", h.verdict()
    assert sum(1 for w in warnings if w) >= 6
    assert h.should_back_off()
    assert h.advice()

    # 3. A genuinely thin market must NOT be called throttling.
    h = DiscoveryHealth()
    for i, n in enumerate([3, 2, 4, 3, 2, 3]):
        h.record("village%d" % i, n)
    assert h.verdict() == "healthy", "a thin market was mistaken for throttling"

    # 4. Page caps only count when the baseline is well above them.
    h = DiscoveryHealth()
    for n in (20, 20, 20, 20):
        h.record("x", n)
    assert h.verdict() == "healthy", "20 IS the real total when nothing beats it"

    h = DiscoveryHealth()
    for n in (80, 75, 90, 20):
        h.record("x", n)
    assert h.flags and "page cap" in h.flags[-1]["reason"]

    # 5. No judgement before warmup.
    h = DiscoveryHealth()
    assert h.record("a", 0) is None and h.baseline is None
    assert not h.should_back_off()

    # -- FailureBreaker ----------------------------------------------------

    # 6. A clean stream never trips.
    b = FailureBreaker()
    for _ in range(50):
        assert b.record(True) is False
    assert not b.tripped and b.fail_rate() == 0.0

    # 7. Not enough evidence yet: 3 failures in the first 3 must not trip.
    b = FailureBreaker()
    for _ in range(3):
        assert b.record(False) is False, "tripped below min_sample"
    assert not b.tripped

    # 8. A sustained failure rate trips, once.
    b = FailureBreaker()
    trips = [b.record(i % 2 == 0) for i in range(30)]   # 50% failures
    assert sum(1 for t in trips if t) == 1, "breaker must trip exactly once"
    assert b.tripped

    # 9. It is the RECENT window that decides, not the whole run: a long
    #    healthy stream followed by a bad patch must still trip.
    b = FailureBreaker()
    for _ in range(200):
        b.record(True)
    assert not b.tripped
    tripped = any(b.record(False) for _ in range(12))
    assert tripped, "a late outage must still be caught"

    # 10. A MINORITY of early failures must not condemn the run. Three bad
    #     businesses in the first ten is a bad batch; the threshold is a rate,
    #     so it stays below it and the run continues.
    b = FailureBreaker()
    for _ in range(3):
        b.record(False)
    for _ in range(40):
        b.record(True)
    assert not b.tripped, "a small early bad patch condemned a healthy run"

    # 11. ...but a sustained opening burst is an outage, and stopping is the
    #     whole point. Nine straight failures must trip as soon as there is
    #     enough evidence to judge -- waiting longer only converts more good
    #     businesses into permanent rejections.
    b = FailureBreaker()
    for _ in range(9):
        assert b.record(False) is False, "tripped before min_sample"
    assert b.record(True) is True, "9 straight failures must trip at sample 10"

    # 12. Tripping latches: it is a stop signal, not a fluctuating gauge.
    b = FailureBreaker()
    for i in range(30):
        b.record(i % 2 == 0)
    assert b.tripped
    for _ in range(100):
        b.record(True)
    assert b.tripped, "the breaker un-tripped itself"

    print("health: all self-checks passed")


if __name__ == "__main__":
    _selfcheck()
