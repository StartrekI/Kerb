"""Things that must not leak into, or out of, a run.

Every case here was found by asking "is this actually true?" rather than by
something failing. They share a shape: a control that looks present and does
nothing, or state that escapes the thing that created it.

    python3 tests/test_isolation.py
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx                                                  # noqa: E402

from kerb import signals                                      # noqa: E402
from kerb.campaign import Campaign                            # noqa: E402
from kerb.models import Cost, Signal, SourceQuery             # noqa: E402
from kerb.pipeline import Pipeline                            # noqa: E402
from kerb.sources import csv_ingest                           # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="kerb-iso-"))


# ------------------------------------------------- the global signal registry

def test_a_stray_signal_does_not_join_every_campaign():
    """`signals_for` used to return every registered signal.

    That meant anything which had registered itself -- a plugin, an
    application, a test module imported into the same process -- silently
    joined every campaign. One broken third-party signal was then enough to
    make every business in every campaign fail to measure, and the campaign
    file gave no clue why.
    """
    campaign = Campaign.from_dict({
        "filters": [{"signal": "reviews", "op": ">=", "value": 10}],
        "where": {"place": "X"}})
    before = set(campaign.signals_for(Cost.FREE))

    @signals.signal(name="_stray_plugin_signal", cost=Cost.FREE)
    def _stray(b, c):
        raise RuntimeError("a plugin someone installed")

    after = set(campaign.signals_for(Cost.FREE))
    assert after == before, "a stray signal joined uninvited: %s" % (after - before)
    assert not signals.is_builtin("_stray_plugin_signal")
    assert signals.is_builtin("web_presence")
    print("  stray signal excluded     ok")


def test_a_campaign_can_still_opt_in_to_a_plugin_signal():
    """Excluding strays must not make plugins unusable -- naming one in a
    filter, a weight or an explicit gating block opts in."""
    @signals.signal(name="_opt_in_signal", cost=Cost.FREE)
    def _opt(b, c):
        return Signal("_opt_in_signal", 1, 1.0, {})

    by_filter = Campaign.from_dict({
        "filters": [{"signal": "_opt_in_signal", "op": "==", "value": 1}],
        "where": {"place": "X"}})
    assert "_opt_in_signal" in by_filter.signals_for(Cost.FREE)

    by_weight = Campaign.from_dict({
        "scoring": {"weights": {"_opt_in_signal": 10}}, "where": {"place": "X"}})
    assert "_opt_in_signal" in by_weight.signals_for(Cost.FREE)

    by_gating = Campaign.from_dict({
        "gating": {"free": ["_opt_in_signal"]}, "where": {"place": "X"}})
    assert by_gating.signals_for(Cost.FREE) == ["_opt_in_signal"]

    nested = Campaign.from_dict({
        "filters": [{"group": "any", "of": [
            {"signal": "_opt_in_signal", "op": "==", "value": 1}]}],
        "where": {"place": "X"}})
    assert "_opt_in_signal" in nested.signals_for(Cost.FREE), \
        "a signal referenced inside a group was not detected"
    print("  opt-in still works        ok")


# ------------------------------------------------------------- request budget

def _fake_overpass():
    def request(self, method, url, **kw):
        if "nominatim" in str(url):
            return httpx.Response(
                200, json=[{"boundingbox": ["51.5", "51.6", "-0.2", "-0.1"]}],
                request=httpx.Request("GET", "x"))
        return httpx.Response(200, json={"elements": [
            {"type": "node", "id": i, "lat": 51.5, "lon": -0.1,
             "tags": {"name": "R%d" % i, "craft": "roofer"}} for i in range(5)]},
            request=httpx.Request("POST", "x"))
    return request


BASE = {"sources": [{"id": "overpass", "options": {"pause": 0}}],
        "where": {"mode": "paste", "places": ["P%d" % i for i in range(10)]},
        "what": {"packs": ["trades/roofing"]}, "filters": []}


def run_with(budget):
    c = Campaign.from_dict({**BASE,
                            "limits": {"max_requests": budget} if budget else {}})
    with mock.patch.object(httpx.Client, "request", _fake_overpass()), \
            mock.patch("time.sleep", lambda *a: None):
        pipe = Pipeline(c)
        out = list(pipe.run())
    return pipe, out


def test_max_requests_is_actually_enforced():
    """It was a documented spend cap that nothing could ever trigger: the
    counter it checked was never incremented by anything."""
    unlimited, _ = run_with(None)
    assert unlimited.stats.requests == 20, unlimited.stats.requests
    assert unlimited.stats.stopped_reason is None

    capped, _ = run_with(6)
    assert capped.stats.requests <= 8, capped.stats.requests
    assert "budget" in (capped.stats.stopped_reason or "")
    print("  request budget enforced   ok")


def test_the_budget_survives_heavy_deduplication():
    """The check used to run only per business yielded. A source that dedupes
    hard can burn a whole budget while yielding almost nothing, so a
    yield-driven check never fires."""
    pipe, out = run_with(6)
    assert len(out) <= 5, "these places all return the same five businesses"
    assert pipe.stats.requests <= 8, \
        "spent %d requests against a cap of 6" % pipe.stats.requests
    print("  budget survives dedupe    ok")


def test_retries_count_against_the_budget():
    """A retry is spend. A budget that ignores them is not a budget."""
    calls = {"n": 0}

    def flaky(self, method, url, **kw):
        if "nominatim" in str(url):
            return httpx.Response(
                200, json=[{"boundingbox": ["51.5", "51.6", "-0.2", "-0.1"]}],
                request=httpx.Request("GET", "x"))
        calls["n"] += 1
        return httpx.Response(429, text="slow down", request=httpx.Request("POST", "x"))

    q = SourceQuery(what="roofing", places=["One"],
                    options={"retries": 3, "backoff": 0, "pause": 0})
    from kerb.sources import overpass
    with mock.patch.object(httpx.Client, "request", flaky), \
            mock.patch("time.sleep", lambda *a: None):
        list(overpass.overpass_source(q))
    assert q.report["requests"] == 4, \
        "1 geocode + 3 attempts should be 4, got %s" % q.report["requests"]
    print("  retries counted           ok")


# --------------------------------------------------------------- memory shape

def test_ingest_streams_instead_of_materialising():
    """Holding a multi-million-row export in memory to read its header is a
    needless way to run a machine out of RAM."""
    big = TMP / "big.csv"
    with big.open("w") as fh:
        fh.write("cid,title,category,review_count\n")
        for i in range(50_000):
            fh.write("0xf:0x%05x,Business %d,Dentist,%d\n" % (i, i, 20 + i % 100))

    import resource
    unit = 1024 * 1024 if sys.platform == "darwin" else 1024
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / unit
    n = sum(1 for _ in csv_ingest.csv_source(SourceQuery(path=str(big))))
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / unit

    assert n == 50_000, n
    assert after - before < 100, "grew %.0f MB reading a %.0f MB file" % (
        after - before, big.stat().st_size / 1e6)
    print("  ingest streams            ok  (%.0f MB growth)" % (after - before))


def test_inspect_does_not_materialise_either():
    big = TMP / "big.csv"
    d = csv_ingest.inspect(str(big))
    assert d["rows"] == 50_000
    assert d["detected_profile"] is None or isinstance(d["detected_profile"], str)
    assert d["rows_without_identity"] == 0
    print("  inspect streams           ok")


# ------------------------------------------------- refusing a false all-clear

def test_stop_never_claims_clean_when_it_cannot_check():
    """`kerb stop` exists because orphaned browsers once ate 88GB. An empty
    process list from a machine with no working `ps` used to read as "nothing
    to stop -- clean", which is the worst thing it could say."""
    import subprocess as sp
    from kerb import cli

    with mock.patch.object(sp, "run", side_effect=OSError("no ps here")):
        try:
            cli.find_managed()
        except cli.CannotEnumerate:
            pass
        else:
            raise AssertionError("an unlistable system reported an empty list")

        class Args:
            dry_run = False
        assert cli.cmd_stop(Args()) != 0, "reported success without checking"
    print("  no false all-clear        ok")


if __name__ == "__main__":
    print("isolation — controls that must actually control\n")
    for fn in (test_a_stray_signal_does_not_join_every_campaign,
               test_a_campaign_can_still_opt_in_to_a_plugin_signal,
               test_max_requests_is_actually_enforced,
               test_the_budget_survives_heavy_deduplication,
               test_retries_count_against_the_budget,
               test_ingest_streams_instead_of_materialising,
               test_inspect_does_not_materialise_either,
               test_stop_never_claims_clean_when_it_cannot_check):
        fn()
    print("\nall isolation checks passed")
