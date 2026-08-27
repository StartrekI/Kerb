"""The Google Maps collector Kerb runs itself.

Every test is offline. The fixture is a REAL response, trimmed to the fields the
parser reads, so a shape change breaks these tests rather than a user's run.

The failure modes matter more than the happy path here, because the endpoint is
undocumented. The one that matters most is `test_a_moved_shape_raises_instead_
of_returning_nothing`: a collector that silently returns an empty list is
indistinguishable from a genuinely empty area, and that is the exact dishonesty
this project keeps removing.

    python3 tests/test_gmaps.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx                                                    # noqa: E402

from kerb import session                                        # noqa: E402
from kerb.models import SourceQuery                             # noqa: E402
from kerb.sources import gmaps                                  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures" / "gmaps_search.json").read_text()
_REAL_CLIENT = httpx.Client          # captured before any patching

# The collector records a real cooldown when it is blocked, and a cooldown is
# PERSISTED. Without this the block test would write to ~/.kerb/session.json and
# refuse to run the developer's next real collection -- a test that sabotages
# the machine it runs on.
import os                                                       # noqa: E402
TEST_PROFILE = Path("/tmp/kerb-test-profile.json")
os.environ["KERB_PROFILE"] = str(TEST_PROFILE)


def _fresh_profile():
    if TEST_PROFILE.exists():
        TEST_PROFILE.unlink()


def drive(q, handler):
    """Run the source against a stubbed transport: no network, no real profile."""
    httpx.Client = lambda **kw: _REAL_CLIENT(                   # noqa: E731
        transport=httpx.MockTransport(handler))
    real_geo = gmaps.geocode
    gmaps.geocode = lambda place, client, **kw: (                # noqa: E731
        None if "Nowhere" in place
        else {"north": 51.6, "south": 51.5, "east": -0.05, "west": -0.15})
    try:
        return list(gmaps.gmaps_source(q))
    finally:
        httpx.Client = _REAL_CLIENT
        gmaps.geocode = real_geo


# ------------------------------------------------------------------ parsing

def test_parses_a_real_response():
    rows = gmaps.parse(FIXTURE, "Islington, London")
    assert len(rows) == 4, len(rows)
    b = rows[0]
    # The cid is already Kerb's identity format, so a business collected here
    # and the same business imported from a CSV deduplicate against each other.
    assert b.cid.startswith("0x") and ":" in b.cid, b.cid
    assert b.name == "London City Smiles"
    assert b.category == "Dental clinic"
    assert b.address.endswith("N1 9LQ")
    assert b.phone == "020 7837 2300"
    assert b.website.startswith("https://")
    assert b.rating == 4.8
    assert b.lat and b.lng
    assert b.source == "gmaps"
    assert b.place_label == "Islington, London"
    assert b.extras["maps_url"].endswith("17131541737225693666")
    print("  parses a real response       ok")


def test_review_count_is_absent_not_invented():
    """The endpoint does not return it under this template. A guessed count
    would silently change every score, which is worse than not knowing."""
    for b in gmaps.parse(FIXTURE, "x"):
        assert b.review_count is None
    print("  review count not faked       ok")


def test_a_moved_shape_raises_instead_of_returning_nothing():
    """THE test. Returning [] here would read as 'no businesses in this area'."""
    for bad, why in (
        (')]}\'\n[["q", []]]', "empty result list is fine"),          # legitimately empty
        (')]}\'\n[["q"]]', "no list at [0][1]"),
        (')]}\'\n{"a":1}', "not the array shape"),
        (')]}\'\n[["q", [[1,2,3]]]]', "entries with no record at [14]"),
    ):
        try:
            got = gmaps.parse(bad, "x")
        except gmaps.ShapeChanged:
            continue
        # only the genuinely-empty case may return cleanly
        assert got == [] and "empty" in why, (why, got)
    print("  moved shape raises           ok")


def test_a_web_page_is_a_block_not_a_parse_error():
    for body in ("<!DOCTYPE html><html><body>consent</body></html>",
                 "<html>sorry</html>"):
        try:
            gmaps.parse(body, "x")
        except gmaps.Blocked:
            continue
        raise AssertionError("an HTML body must be reported as a block")
    print("  html body is a block         ok")


def test_a_record_without_identity_is_skipped():
    data = json.loads(FIXTURE[FIXTURE.index("\n") + 1:])
    rec = [None] * 260
    rec[gmaps.FIELDS["name"]] = "No cid"
    data[0][1].append([None] * 14 + [rec])
    rows = gmaps.parse(")]}'\n" + json.dumps(data), "x")
    assert len(rows) == 4, "the id-less record must be dropped, not counted"
    print("  id-less record skipped       ok")


# ------------------------------------------------------------------ running

def test_pagination_stops_on_a_short_page():
    _fresh_profile()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text=FIXTURE)      # 4 records < PAGE

    rows = drive(SourceQuery(what="dentist", places=["Islington"],
                             options={"pause": 0}), handler)
    assert len(rows) == 4
    assert calls["n"] == 1, "a short page means the last page"
    print("  stops on a short page        ok")


def test_limit_stops_early():
    _fresh_profile()
    handler = lambda r: httpx.Response(200, text=FIXTURE)        # noqa: E731
    rows = drive(SourceQuery(what="dentist", places=["A", "B", "C"], limit=6,
                             options={"pause": 0}), handler)
    assert len(rows) == 6, len(rows)
    print("  limit honoured               ok")


def test_an_unreadable_place_is_reported_not_swallowed():
    _fresh_profile()
    """A place that returns nothing must be visible. Fewer results with no
    explanation is the failure this project exists to remove."""
    def handler(request):
        return httpx.Response(200, text=')]}\'\n[["q", []]]')

    q = SourceQuery(what="dentist", places=["Islington"], options={"pause": 0})
    rows = drive(q, handler)
    assert rows == []
    assert "gmaps/Islington" in (q.report.get("skipped_places") or {}), q.report
    print("  empty place is reported      ok")


def test_a_place_that_will_not_geocode_is_reported():
    _fresh_profile()
    handler = lambda r: httpx.Response(200, text=FIXTURE)        # noqa: E731
    q = SourceQuery(what="dentist", places=["Nowhere", "Islington"],
                    options={"pause": 0})
    rows = drive(q, handler)
    assert len(rows) == 4, "the good place must still be collected"
    assert "gmaps/Nowhere" in (q.report.get("skipped_places") or {})
    print("  ungeocodable place reported  ok")


def test_a_block_stops_the_whole_run():
    """A block is on the address, so every remaining place would hit it too.
    Kerb never tries to work around one."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, text="<html>sorry</html>")

    _fresh_profile()
    q = SourceQuery(what="dentist", places=["A", "B", "C", "D"],
                    options={"pause": 0, "retries": 1})
    rows = drive(q, handler)
    assert rows == []
    assert calls["n"] == 1, "a block must not be retried per place"
    assert "rate-limit" in q.report.get("fatal", "").lower(), q.report
    # The penalty must OUTLIVE the run, or the next one walks straight back in.
    assert q.report.get("cooldown_seconds", 0) > 0, q.report
    assert session.Profile.load(TEST_PROFILE).cooling() > 0
    print("  block stops the run          ok")


def test_a_live_cooldown_refuses_to_start():
    """Walking back into a live block is how a short penalty becomes a long one."""
    _fresh_profile()
    prof = session.Profile({}, TEST_PROFILE)
    prof.created = 1.0
    prof.record_block(base=600)
    try:
        drive(SourceQuery(what="dentist", places=["A"], options={"pause": 0}),
              lambda r: httpx.Response(200, text=FIXTURE))
    except gmaps.Blocked as exc:
        assert "cooling down" in str(exc).lower(), str(exc)
        _fresh_profile()
        print("  live cooldown refuses        ok")
        return
    raise AssertionError("a run must not start while cooling down")


def test_a_clean_run_clears_the_penalty():
    _fresh_profile()
    prof = session.Profile({}, TEST_PROFILE)
    prof.created = 1.0
    prof.blocks = 2
    prof.save()
    drive(SourceQuery(what="dentist", places=["Islington"], options={"pause": 0}),
          lambda r: httpx.Response(200, text=FIXTURE))
    assert session.Profile.load(TEST_PROFILE).blocks == 0, "success forgives"
    _fresh_profile()
    print("  clean run clears penalty     ok")


def test_a_shape_change_stops_the_run_loudly():
    _fresh_profile()
    handler = lambda r: httpx.Response(200, text=')]}\'\n[["q"]]')  # noqa: E731
    q = SourceQuery(what="dentist", places=["A", "B"], options={"pause": 0})
    rows = drive(q, handler)
    assert rows == []
    assert "fatal" in q.report and "shape" in q.report["fatal"].lower(), q.report
    print("  shape change stops loudly    ok")


def test_requests_are_counted_including_retries():
    _fresh_profile()
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text=FIXTURE)

    q = SourceQuery(what="dentist", places=["Islington"],
                    options={"pause": 0, "retries": 4, "backoff": 0})
    rows = drive(q, handler)
    assert len(rows) == 4
    assert q.report["requests"] == 3, q.report
    print("  retries count as spend       ok")


def test_no_trade_and_no_places_are_refused():
    _fresh_profile()
    for q, word in ((SourceQuery(places=["A"]), "trade"),
                    (SourceQuery(what="dentist"), "place")):
        try:
            drive(q, lambda r: httpx.Response(200, text=FIXTURE))
        except ValueError as exc:
            assert word in str(exc).lower(), str(exc)
            continue
        raise AssertionError("must refuse: missing %s" % word)
    print("  missing inputs refused       ok")


# ------------------------------------------------------------------ profile

def test_profile_round_trips_and_is_private(tmp=Path("/tmp/kerb-gmaps-test.json")):
    p = session.Profile({}, tmp)
    p.created = 1.0
    p.cookies = {"SOCS": "x"}
    p.locale = {"hl": "en", "gl": "uk"}
    p.save()
    import os
    assert oct(os.stat(tmp).st_mode)[-3:] == "600", "a cookie jar is a credential"
    back = session.Profile.load(tmp)
    assert back.cookies == {"SOCS": "x"} and back.locale["gl"] == "uk"
    assert back.exists
    tmp.write_text("{ not json")
    broken = session.Profile.load(tmp)
    assert not broken.exists, "a corrupt profile behaves like no profile"
    tmp.unlink()
    print("  profile round-trips          ok")


if __name__ == "__main__":
    print("gmaps -- the collector Kerb runs itself\n")
    for fn in (test_parses_a_real_response,
               test_review_count_is_absent_not_invented,
               test_a_moved_shape_raises_instead_of_returning_nothing,
               test_a_web_page_is_a_block_not_a_parse_error,
               test_a_record_without_identity_is_skipped,
               test_pagination_stops_on_a_short_page,
               test_limit_stops_early,
               test_an_unreadable_place_is_reported_not_swallowed,
               test_a_place_that_will_not_geocode_is_reported,
               test_a_block_stops_the_whole_run,
               test_a_shape_change_stops_the_run_loudly,
               test_requests_are_counted_including_retries,
               test_no_trade_and_no_places_are_refused,
               test_a_live_cooldown_refuses_to_start,
               test_a_clean_run_clears_the_penalty,
               test_profile_round_trips_and_is_private):
        fn()
    print("\nall gmaps checks passed")
