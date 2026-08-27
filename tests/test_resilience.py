"""What happens when things break.

The pipeline makes a promise: one bad signal, one dead source, or one
unreachable town must cost only itself. Every test here is a case where that
promise did not hold and a single local failure destroyed an entire run.

    python3 tests/test_resilience.py
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx                                                 # noqa: E402

from kerb import signals, sources                            # noqa: E402
from kerb.campaign import Campaign                           # noqa: E402
from kerb.models import Business, Cost, Signal, SourceQuery  # noqa: E402
from kerb.pipeline import Pipeline                           # noqa: E402
from kerb.signals import Context                             # noqa: E402
from kerb.sources import overpass                            # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="kerb-res-"))
GOOD = TMP / "good.csv"
GOOD.write_text("cid,title,category,review_count\n"
                "0x7:0x1,Real Dental,Dentist,90\n"
                "0x7:0x2,Other Dental,Dentist,70\n")


# ------------------------------------------------------------------ signals

def test_a_signal_returning_junk_is_contained():
    """Stamping the result happened outside the try, so a signal returning
    anything but a Signal -- None being the easy plugin mistake -- raised
    AttributeError straight through the guard and killed the whole run."""
    for bad_value in (None, "a string", 42, {"not": "a signal"}):
        name = "_junk_%s" % type(bad_value).__name__

        @signals.signal(name=name, cost=Cost.FREE, version=7)
        def _junk(b, c, _v=bad_value):
            return _v

        s = signals.compute(name, Business(cid="0x1:0x1"), Context())
        assert isinstance(s, Signal), "compute must always return a Signal"
        assert s.value == "unknown" and s.confidence == 0.0
        assert "error" in s.evidence
        assert s.version == 7, "the registered version must survive the failure"
    print("  junk signal contained     ok")


def test_a_raising_signal_still_scores_the_rest():
    @signals.signal(name="_raises", cost=Cost.FREE)
    def _raises(b, c):
        raise RuntimeError("boom")

    biz = Business(cid="0x1:0x1", name="X", category="Dentist")
    ctx = Context(options={"trade_match": {"pack": "trades/dentist"}})
    assert signals.compute("_raises", biz, ctx).confidence == 0.0
    assert signals.compute("trade_match", biz, ctx).value == "dentist", \
        "a neighbouring signal must be unaffected"
    print("  raising signal contained  ok")


# ------------------------------------------------------------------ sources

def test_a_dead_source_does_not_lose_the_others():
    @sources.source(id="_dead_src", label="dead")
    def _dead(q):
        raise ConnectionError("host is down")
        yield                                   # pragma: no cover

    campaign = Campaign.from_dict({
        "sources": [{"id": "_dead_src"}, {"id": "csv", "options": {"path": str(GOOD)}}],
        "what": {"packs": ["trades/dentist"]},
        "filters": [{"signal": "trade_match", "op": "!=", "value": False}]})
    pipe = Pipeline(campaign)
    out = list(pipe.run())

    assert len(out) == 2, "results from the working source must survive"
    assert pipe.stats.source_errors["_dead_src"].startswith("ConnectionError")
    assert "source_errors" in pipe.stats.to_dict()
    print("  dead source contained     ok")


def test_a_source_that_dies_midway_keeps_what_it_yielded():
    """The exception can arrive during iteration, not only at creation."""
    @sources.source(id="_half_src", label="half")
    def _half(q):
        yield Business(cid="0x8:0x1", name="Before The Fall", category="Dentist")
        raise TimeoutError("connection dropped")

    campaign = Campaign.from_dict({
        "sources": [{"id": "_half_src"}],
        "what": {"packs": ["trades/dentist"]},
        "filters": []})
    pipe = Pipeline(campaign)
    out = list(pipe.run())
    assert len(out) == 1 and out[0].business.name == "Before The Fall"
    assert "_half_src" in pipe.stats.source_errors
    print("  partial source kept       ok")


def test_source_failure_is_visible_not_swallowed():
    """Containment must not become concealment: a quiet partial result is
    indistinguishable from a complete one."""
    events = []

    @sources.source(id="_dead_src2", label="dead")
    def _dead(q):
        raise ConnectionError("host is down")
        yield                                   # pragma: no cover

    campaign = Campaign.from_dict({
        "sources": [{"id": "_dead_src2"}, {"id": "csv", "options": {"path": str(GOOD)}}],
        "what": {"packs": ["trades/dentist"]}, "filters": []})
    list(Pipeline(campaign, on_progress=events.append).run())
    assert any(e.get("stage") == "source_error" for e in events)
    print("  failure surfaced          ok")


# ----------------------------------------------------------------- overpass

def _fake(nominatim, overpass_response):
    def request(self, method, url, **kw):
        if "nominatim" in str(url):
            return nominatim(kw)
        return overpass_response(kw)
    return request


def _bbox_hit(_kw):
    return httpx.Response(200, json=[{"boundingbox": ["51.5", "51.6", "-0.2", "-0.1"]}],
                          request=httpx.Request("GET", "x"))


def _one_roofer(_kw):
    return httpx.Response(200, json={"elements": [
        {"type": "node", "id": 1, "lat": 51.5, "lon": -0.1,
         "tags": {"name": "A Roofer", "craft": "roofer"}}]},
        request=httpx.Request("POST", "x"))


def test_one_bad_place_does_not_kill_the_others():
    """A timeout on the third of fifty towns used to discard the two already
    collected and the forty-seven not yet tried."""
    def nominatim(kw):
        q = (kw.get("params") or {}).get("q", "")
        if "Bad" in q:
            return httpx.Response(200, json=[], request=httpx.Request("GET", "x"))
        if "Timeout" in q:
            raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", "x"))
        return _bbox_hit(kw)

    with mock.patch.object(httpx.Client, "request", _fake(nominatim, _one_roofer)), \
            mock.patch("time.sleep", lambda *a: None):
        q = SourceQuery(what="roofing",
                        places=["GoodA", "BadPlace", "TimeoutPlace", "GoodB"])
        got = list(overpass.overpass_source(q))

    assert len(got) == 2, "both reachable places must still produce results"
    failed = q.report["failed_places"]
    assert set(failed) == {"BadPlace", "TimeoutPlace"}
    assert "geocoded" in failed["BadPlace"]
    print("  bad place skipped         ok")


def test_rate_limiting_is_retried_not_fatal():
    """Overpass answers 429 as a matter of routine. The first one used to
    abort the entire run."""
    calls = {"n": 0}

    def overpass_resp(kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"},
                                  text="slow down", request=httpx.Request("POST", "x"))
        return _one_roofer(kw)

    with mock.patch.object(httpx.Client, "request", _fake(_bbox_hit, overpass_resp)), \
            mock.patch("time.sleep", lambda *a: None):
        got = list(overpass.overpass_source(
            SourceQuery(what="roofing", places=["Somewhere"])))

    assert calls["n"] == 2, "a 429 must be retried, not raised"
    assert len(got) == 1
    print("  429 retried               ok")


def test_retries_are_finite():
    """Retrying forever is its own failure. Give up, report, move on."""
    calls = {"n": 0}

    def always_429(kw):
        calls["n"] += 1
        return httpx.Response(429, text="no", request=httpx.Request("POST", "x"))

    with mock.patch.object(httpx.Client, "request", _fake(_bbox_hit, always_429)), \
            mock.patch("time.sleep", lambda *a: None):
        q = SourceQuery(what="roofing", places=["Somewhere"],
                        options={"retries": 3, "backoff": 0})
        got = list(overpass.overpass_source(q))

    assert calls["n"] == 3, "exactly the configured budget, got %d" % calls["n"]
    assert got == []
    assert "429" in q.report["failed_places"]["Somewhere"]
    print("  retries bounded           ok")


def test_attribution_rides_along():
    with mock.patch.object(httpx.Client, "request", _fake(_bbox_hit, _one_roofer)), \
            mock.patch("time.sleep", lambda *a: None):
        got = list(overpass.overpass_source(
            SourceQuery(what="roofing", places=["Somewhere"])))
    assert got[0].extras["attribution"] == overpass.ATTRIBUTION, \
        "ODbL attribution must travel with the data, not be remembered later"
    print("  attribution attached      ok")


if __name__ == "__main__":
    print("resilience — one local failure must stay local\n")
    for fn in (test_a_signal_returning_junk_is_contained,
               test_a_raising_signal_still_scores_the_rest,
               test_a_dead_source_does_not_lose_the_others,
               test_a_source_that_dies_midway_keeps_what_it_yielded,
               test_source_failure_is_visible_not_swallowed,
               test_one_bad_place_does_not_kill_the_others,
               test_rate_limiting_is_retried_not_fatal,
               test_retries_are_finite,
               test_attribution_rides_along):
        fn()
    print("\nall resilience checks passed")
