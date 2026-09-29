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
    # Read fine and empty: reported, but NOT as a failure to read.
    assert "Islington" in (q.report.get("empty_places") or {}), q.report
    assert "Islington" not in (q.report.get("failed_places") or {}), q.report
    print("  empty place is reported      ok")


def test_a_place_that_will_not_geocode_is_reported():
    """Only when geocoding is asked for. It is off by default now: the viewport
    was measured to have no effect on results, so Nominatim was a single point
    of failure doing no work -- one 429 from it zeroed an 80-place run while
    Google was answering perfectly."""
    _fresh_profile()
    handler = lambda r: httpx.Response(200, text=FIXTURE)        # noqa: E731
    q = SourceQuery(what="dentist", places=["Nowhere", "Islington"],
                    options={"pause": 0, "geocode": True})
    rows = drive(q, handler)
    assert len(rows) == 4, "the good place must still be collected"
    assert "geocoded" in (q.report.get("failed_places") or {}).get("Nowhere", "")
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


def test_cookie_import_accepts_what_browsers_actually_export():
    """Three formats, because people export from three different places and
    being told "wrong format" is a terrible first experience."""
    from kerb.session import parse_cookies
    header = parse_cookies("Cookie: SID=abc; HSID=def; SSID=ghi")
    assert header == {"SID": "abc", "HSID": "def", "SSID": "ghi"}, header

    js = parse_cookies('[{"name":"SID","value":"abc"},{"name":"HSID","value":"d"}]')
    assert js == {"SID": "abc", "HSID": "d"}, js

    nets = parse_cookies("# Netscape HTTP Cookie File\n"
                         ".google.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
                         ".google.com\tTRUE\t/\tTRUE\t0\tHSID\tdef\n")
    assert nets == {"SID": "abc", "HSID": "def"}, nets

    for junk in ("", "   ", "no cookies here at all"):
        try:
            got = parse_cookies(junk)
        except session.SetupError:
            continue
        assert got == {}, got
    print("  cookie formats parsed        ok")


def test_signed_out_is_detected_and_said_out_loud():
    """Google serves signed-out clients a reduced view of Maps. A run that
    quietly returns less is the failure this project keeps removing."""
    from kerb.session import looks_signed_in
    assert looks_signed_in('<div aria-label="Google Account: Sam"></div>')
    assert not looks_signed_in('<a href="/login" aria-label="Sign in">x</a>')
    assert not looks_signed_in("<span>Sign in</span>")
    # No marker either way -- a consent page, an error page, a redesign -- is
    # not evidence of a signed-in session. It used to read as signed in.
    assert not looks_signed_in("<html><body>Before you continue</body></html>")

    _fresh_profile()
    prof = session.Profile({}, TEST_PROFILE)
    prof.created = 1.0
    prof.signed_in = False
    prof.save()
    q = SourceQuery(what="dentist", places=["Islington"], options={"pause": 0})
    drive(q, lambda r: httpx.Response(200, text=FIXTURE))
    notes = " ".join(q.report.get("notes") or [])
    assert "signed out" in notes.lower(), q.report
    assert "review data" in notes.lower(), q.report
    _fresh_profile()
    print("  signed-out state reported    ok")


def test_parallel_places_collect_everything_exactly_once():
    """Sharding must not drop a place or double-count one. Round-robin over
    workers is easy to get subtly wrong in a way only volume exposes."""
    _fresh_profile()
    places = ["P%02d" % i for i in range(13)]      # prime-ish, uneven shards
    seen_places = []
    lock = __import__("threading").Lock()

    def handler(request):
        with lock:
            seen_places.append(request.url.params.get("q", ""))
        return httpx.Response(200, text=FIXTURE)

    q = SourceQuery(what="dentist", places=places,
                    options={"pause": 0, "workers": 4, "max_pages": 1})
    rows = drive(q, handler)
    assert len(rows) == 13 * 4, len(rows)          # 4 records per place
    got = {r.place_label for r in rows}
    assert got == set(places), sorted(set(places) - got)
    print("  parallel: every place once   ok")


def test_parallel_respects_the_limit_exactly():
    """Four workers racing on one counter is where an off-by-N appears."""
    _fresh_profile()
    q = SourceQuery(what="dentist", places=["A", "B", "C", "D", "E", "F"],
                    limit=9, options={"pause": 0, "workers": 4, "max_pages": 1})
    rows = drive(q, lambda r: httpx.Response(200, text=FIXTURE))
    assert len(rows) == 9, len(rows)
    print("  parallel: limit is exact     ok")


def test_a_block_in_one_worker_stops_them_all():
    """A block is on the address. Five more workers proving it costs five more
    strikes against the same IP."""
    _fresh_profile()
    calls = {"n": 0}
    lock = __import__("threading").Lock()

    def handler(request):
        with lock:
            calls["n"] += 1
        return httpx.Response(429, text="<html>sorry</html>")

    q = SourceQuery(what="dentist", places=["A", "B", "C", "D", "E", "F", "G", "H"],
                    options={"pause": 0, "workers": 4, "retries": 1})
    rows = drive(q, handler)
    assert rows == []
    assert calls["n"] <= 4, "one strike per live worker, not per place: %s" % calls
    assert "fatal" in q.report
    assert session.Profile.load(TEST_PROFILE).cooling() > 0
    _fresh_profile()
    print("  parallel: block stops all    ok")


def test_one_block_is_recorded_once_not_once_per_worker():
    """Four workers hitting the same 429 recorded four blocks, compounding a
    15-minute cooldown into two hours for a single event.

    A BARRIER is the whole test. Without it every worker checks `stop` before
    its request, so the first one to fail short-circuits the rest and the bug
    never appears -- measured at 0 catches in 10 runs. The real failure had all
    four already IN FLIGHT when the 429s came back, which is what this forces.
    """
    import threading as _t
    _fresh_profile()
    N = 4
    gate = _t.Barrier(N, timeout=10)

    def handler(request):
        try:
            gate.wait()          # nobody gets a 429 until all four are waiting
        except _t.BrokenBarrierError:
            pass
        return httpx.Response(429, text="<html>sorry</html>")

    q = SourceQuery(what="d", places=list("ABCD"),
                    options={"pause": 0, "workers": N, "retries": 1})
    drive(q, handler)
    prof = session.Profile.load(TEST_PROFILE)
    assert prof.blocks == 1, (
        "one block event recorded %d times -- the cooldown compounds to %ds "
        "instead of 900s" % (prof.blocks, prof.cooling()))
    assert prof.cooling() <= 900 + 5, prof.cooling()
    _fresh_profile()
    print("  one block counted once       ok")


def test_concurrent_profile_saves_do_not_race():
    """Every thread wrote to the same `.tmp` and renamed it, so the first
    rename moved the file out from under the second -- and it died at exactly
    the moment the profile most needs writing, recording a block."""
    import threading as _t
    _fresh_profile()
    p0 = session.Profile({}, TEST_PROFILE); p0.created = 1.0; p0.save()
    errs = []

    def hammer():
        try:
            for _ in range(30):
                session.Profile.load(TEST_PROFILE).save()
        except Exception as exc:            # noqa: BLE001
            errs.append(exc)

    ts = [_t.Thread(target=hammer) for _ in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errs, errs[:2]
    assert not list(TEST_PROFILE.parent.glob(TEST_PROFILE.name + ".tmp*")), "temp files left"
    _fresh_profile()
    print("  concurrent saves are safe    ok")


def test_geocoding_is_off_by_default():
    """The collector must not call Nominatim unless asked. It is a volunteer
    service, and depending on it for something measured to be irrelevant is how
    an 80-place run returned zero with Google working fine."""
    _fresh_profile()
    called = []
    real = gmaps.geocode
    gmaps.geocode = lambda p, c, **k: (called.append(p) or
                                       {"north": 1, "south": 0, "east": 1, "west": 0})
    try:
        httpx.Client = lambda **kw: _REAL_CLIENT(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=FIXTURE)))
        rows = list(gmaps.gmaps_source(
            SourceQuery(what="d", places=["A", "B"], options={"pause": 0})))
    finally:
        httpx.Client = _REAL_CLIENT
        gmaps.geocode = real
    assert called == [], "geocoded without being asked: %s" % called
    assert len(rows) == 8, len(rows)
    _fresh_profile()
    print("  no geocoding by default      ok")


# ----------------------------------------------- through the whole pipeline
#
# Every test above reads q.report straight off the source. That is exactly
# how a block went unnoticed: the source reported it perfectly, and the
# pipeline -- the only thing a user ever sees -- never read the key. These
# go the whole way.

def _pipeline_with(handler, **camp):
    from kerb.campaign import Campaign
    from kerb.pipeline import Pipeline
    c = Campaign.from_dict({
        "sources": [{"id": "gmaps", "options": {"pause": 0, "retries": 1,
                                                **camp.pop("options", {})}}],
        "where": {"mode": "paste", "places": camp.pop("places", ["A", "B", "C"])},
        "what": {"packs": ["trades/dentist"]}, "filters": [], **camp})
    httpx.Client = lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler))
    try:
        pipe = Pipeline(c)
        out = list(pipe.run())
    finally:
        httpx.Client = _REAL_CLIENT
    return pipe, out


def test_a_block_reaches_the_run_not_just_the_source():
    """The run finished `done`, empty, with no error anywhere -- while the
    source had written 'Google is rate-limiting this address' into a key
    nothing read."""
    from kerb.pipeline import terminal_status
    _fresh_profile()
    pipe, out = _pipeline_with(lambda r: httpx.Response(429, text="<html>x</html>"))
    st = pipe.stats.to_dict()
    assert out == []
    assert "rate-limit" in st["source_errors"].get("gmaps", "").lower(), st
    assert terminal_status(st) == "partial", "an empty blocked run called itself done"
    _fresh_profile()
    print("  block reaches the run        ok")


def test_a_layout_change_reaches_the_run():
    from kerb.pipeline import terminal_status
    _fresh_profile()
    pipe, _ = _pipeline_with(lambda r: httpx.Response(200, text=')]}\'\n[["q"]]'))
    st = pipe.stats.to_dict()
    assert "shape" in st["source_errors"].get("gmaps", "").lower(), st
    assert terminal_status(st) == "partial"
    _fresh_profile()
    print("  shape change reaches run     ok")


def test_empty_places_and_notes_reach_the_run():
    """An empty place is reported but is not a failure; a signed-out note is
    said to the user instead of being left in a dict."""
    from kerb.pipeline import terminal_status
    _fresh_profile()
    prof = session.Profile({}, TEST_PROFILE)
    prof.created = 1.0
    prof.save()
    pipe, _ = _pipeline_with(lambda r: httpx.Response(200, text=')]}\'\n[["q", []]]'),
                             places=["Village"])
    st = pipe.stats.to_dict()
    assert "gmaps/Village" in st["empty_places"], st
    assert not st["skipped_places"], "an empty place is not an unreadable one"
    assert any("signed out" in n for n in st["notes"]), st["notes"]
    assert terminal_status(st) == "done", "nothing found is a complete answer"
    _fresh_profile()
    print("  empty places + notes surface ok")


def test_workers_stop_when_the_run_stops():
    """Closing the source must stop its threads. A run capped at 4 requests
    used to return after 6 while its workers went on to make 40 in the
    background -- which is how an address gets blocked."""
    import threading as _t
    import time as _time
    _fresh_profile()
    calls = {"n": 0}
    lock = _t.Lock()

    def slow(request):
        with lock:
            calls["n"] += 1
        _time.sleep(0.05)
        return httpx.Response(200, text=FIXTURE)

    pipe, _ = _pipeline_with(slow, places=["P%02d" % i for i in range(40)],
                             options={"max_pages": 1},
                             limits={"max_requests": 4, "workers": {"discover": 4}})
    at_return = calls["n"]
    _time.sleep(1.0)
    assert calls["n"] == at_return, \
        "workers kept fetching after the run returned (%d -> %d)" % (at_return, calls["n"])
    assert at_return <= 4 + 4, "one in-flight request per worker at most: %d" % at_return
    assert "budget" in (pipe.stats.stopped_reason or "")
    _fresh_profile()
    print("  workers stop with the run    ok  (%d requests)" % at_return)


def test_a_durable_block_halts_and_keeps_the_place():
    """In durable mode a blocked place was recorded DONE with zero rows, so a
    resume never went back for it. It must stay queued, and the run stop."""
    import tempfile as _tf
    from kerb.store import DONE, PENDING, Store
    from kerb.sources.durable import collect_places
    _fresh_profile()
    db = Store(Path(_tf.mkdtemp()) / "halt.db")
    run_id = db.create_run({"name": "halt"})
    httpx.Client = lambda **kw: _REAL_CLIENT(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, text="<html>x</html>")))
    try:
        res = collect_places(db, run_id, "gmaps", ["A", "B", "C"], trade="dentist",
                             options={"retries": 1, "per_second": 1000}, workers=1)
    finally:
        httpx.Client = _REAL_CLIENT
    counts = db.counts(run_id)
    assert counts[DONE] == 0, "a blocked place was banked as done: %s" % counts
    assert counts[PENDING] == 3, counts
    assert res.stopped and "rate-limit" in res.stopped.lower(), res.stopped
    assert not db.failures(run_id), "a block is not the place's fault"
    db.close()
    _fresh_profile()
    print("  durable block halts          ok")


if __name__ == "__main__":
    print("gmaps -- the collector Kerb runs itself\n")
    for fn in (test_parses_a_real_response,
               test_cookie_import_accepts_what_browsers_actually_export,
               test_signed_out_is_detected_and_said_out_loud,
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
               test_parallel_places_collect_everything_exactly_once,
               test_parallel_respects_the_limit_exactly,
               test_a_block_in_one_worker_stops_them_all,
               test_one_block_is_recorded_once_not_once_per_worker,
               test_concurrent_profile_saves_do_not_race,
               test_geocoding_is_off_by_default,
               test_profile_round_trips_and_is_private,
               test_a_block_reaches_the_run_not_just_the_source,
               test_a_layout_change_reaches_the_run,
               test_empty_places_and_notes_reach_the_run,
               test_workers_stop_when_the_run_stops,
               test_a_durable_block_halts_and_keeps_the_place):
        fn()
    print("\nall gmaps checks passed")
