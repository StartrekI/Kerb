"""Kill it and prove nothing was lost.

Not a simulation. These tests start a real collector in a real subprocess,
`SIGKILL` it partway through -- no cleanup, no handlers, no chance to flush --
and then check the ledger from a fresh process.

Three properties, and they are the whole point of the store:

  nothing lost        every unit finished before the kill kept its results
  nothing duplicated  resuming cannot write a business twice
  nothing stranded    the units the dead process was holding come back

    python3 tests/test_durability.py
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kerb.collect import Fatal, RateLimiter, collect          # noqa: E402
from kerb.health import FailureBreaker                        # noqa: E402
from kerb.models import Business                              # noqa: E402
from kerb.store import DONE, FAILED, PENDING, RUNNING, Store  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="kerb-dur-"))

# A worker script, run for real and killed for real.
WORKER = r'''
import sys, time
sys.path.insert(0, %r)
from kerb.store import Store
from kerb.collect import collect, RateLimiter
from kerb.models import Business

store = Store(%r)
run_id = "killrun"
units = ["place-%%03d" %% i for i in range(200)]

def fetch(unit, ctx):
    time.sleep(0.08)                      # each unit takes real time
    for j in range(5):
        yield Business(cid="%%s:%%d" %% (unit, j), name="Biz %%s %%d" %% (unit, j),
                       category="Dentist", review_count=50 + j)

collect(store, run_id, "discover", units, fetch, workers=4,
        rate=RateLimiter(per_second=1000))
print("FINISHED", flush=True)
'''


def run_worker_and_kill(db: Path, after: float) -> subprocess.Popen:
    script = TMP / "worker.py"
    script.write_text(WORKER % (str(ROOT), str(db)))
    proc = subprocess.Popen([sys.executable, str(script)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(after)
    proc.send_signal(signal.SIGKILL)       # no handlers, no flush, no mercy
    proc.wait(timeout=10)
    if proc.returncode == 0:
        raise AssertionError("worker finished before the kill; lengthen the job")
    return proc


def test_kill_9_loses_nothing_and_duplicates_nothing():
    db = TMP / "kill.db"
    store = Store(db)
    run_id = "killrun"

    proc = run_worker_and_kill(db, after=1.1)
    assert proc.returncode in (-9, 137), proc.returncode

    mid = store.counts(run_id)
    assert mid[DONE] > 0, "the worker finished nothing before the kill; test is useless"
    assert mid[DONE] < 200, "the worker finished everything; kill it sooner"
    print("     killed with %d/%d units done, %d results banked"
          % (mid[DONE], 200, mid["results"]))

    # Every banked result belongs to a unit that was genuinely completed.
    assert mid["results"] == mid[DONE] * 5, \
        "results (%d) do not match completed units (%d x 5)" % (
            mid["results"], mid[DONE])

    # Units the dead process held are reclaimed, not stranded.
    stranded = mid[RUNNING]
    reclaimed = store.reclaim_all(run_id)
    assert store.counts(run_id)[RUNNING] == 0
    print("     %d unit(s) were mid-flight; %d reclaimed" % (stranded, reclaimed))

    # Resume in this process and finish the job.
    def fetch(unit, ctx):
        for j in range(5):
            yield Business(cid="%s:%d" % (unit, j), name="Biz %s %d" % (unit, j),
                           category="Dentist", review_count=50 + j)

    units = ["place-%03d" % i for i in range(200)]
    result = collect(store, run_id, "discover", units, fetch, workers=4,
                     rate=RateLimiter(per_second=10000))

    final = store.counts(run_id)
    assert final[DONE] == 200, final
    assert final["results"] == 1000, "expected 200 units x 5 = 1000, got %d" % final["results"]

    cids = [r["cid"] for r in store.results(run_id)]
    assert len(cids) == len(set(cids)) == 1000, "duplicates after resume"
    print("     resumed: %d units, %d unique results, 0 duplicates"
          % (final[DONE], len(set(cids))))
    print("  kill -9 loses nothing     ok")
    store.close()


def test_a_second_kill_mid_resume_also_loses_nothing():
    """Crashing during the recovery must be survivable too."""
    db = TMP / "kill2.db"
    store = Store(db)
    run_id = "killrun"

    for i, delay in enumerate((0.8, 0.8, 0.8)):
        proc = run_worker_and_kill(db, after=delay)
        assert proc.returncode in (-9, 137)
        counts = store.counts(run_id)
        assert counts["results"] == counts[DONE] * 5, \
            "pass %d: ledger inconsistent (%s)" % (i, counts)

    def fetch(unit, ctx):
        for j in range(5):
            yield Business(cid="%s:%d" % (unit, j), name="B", category="Dentist")

    collect(store, run_id, "discover", ["place-%03d" % i for i in range(200)],
            fetch, workers=4, rate=RateLimiter(per_second=10000))
    final = store.counts(run_id)
    assert final[DONE] == 200 and final["results"] == 1000, final
    cids = [r["cid"] for r in store.results(run_id)]
    assert len(set(cids)) == 1000
    print("  three kills, still exact   ok")
    store.close()


# ------------------------------------------------------------ the ledger

def test_results_and_completion_are_one_transaction():
    """The failure this design exists to prevent: results written, process
    dies, task still pending -> work repeated; or task marked done, process
    dies, results never written -> work silently lost."""
    store = Store(TMP / "tx.db")
    run_id = store.create_run({"name": "tx"})
    store.add_tasks(run_id, "k", ["u1"])
    task = store.claim(run_id)

    class Boom(Exception):
        pass

    real_conn = store.db

    class DiesBetweenTheWrites:
        """Lets the results INSERT through, then dies before the task UPDATE --
        precisely the window this design claims not to have."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **kw):
            if sql.strip().startswith("UPDATE tasks SET state=?, lease_until=NULL,"
                                      " error=NULL"):
                raise Boom("died between the two writes")
            return self._conn.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    store.db = DiesBetweenTheWrites(real_conn)
    try:
        store.complete(task["id"], [{"cid": "c1"}], run_id=run_id)
    except Boom:
        pass
    finally:
        store.db = real_conn

    assert store.result_count(run_id) == 0, \
        "results survived a rolled-back completion -- they would be orphaned"
    assert store.counts(run_id)[DONE] == 0
    store.complete(task["id"], [{"cid": "c1"}], run_id=run_id)
    assert store.result_count(run_id) == 1 and store.counts(run_id)[DONE] == 1
    print("  atomic completion         ok")
    store.close()


def test_enqueueing_is_idempotent():
    store = Store(TMP / "idem.db")
    run_id = store.create_run({"name": "i"})
    units = ["a", "b", "c"]
    assert store.add_tasks(run_id, "k", units) == 3
    assert store.add_tasks(run_id, "k", units) == 0, "re-declaring the plan duplicated it"
    assert sum(store.counts(run_id)[s] for s in (PENDING,)) == 3
    print("  idempotent enqueue        ok")
    store.close()


def test_expired_leases_come_back_by_themselves():
    store = Store(TMP / "lease.db")
    run_id = store.create_run({"name": "l"})
    store.add_tasks(run_id, "k", ["u1"])
    task = store.claim(run_id, lease=0.2)
    assert task and store.counts(run_id)[RUNNING] == 1
    assert store.claim(run_id) is None, "a leased task was handed to a second worker"
    time.sleep(0.35)
    again = store.claim(run_id)
    assert again and again["id"] == task["id"], "expired lease was not reclaimed"
    assert again["attempts"] == 2
    print("  leases expire             ok")
    store.close()


def test_failures_retry_then_stick_with_a_reason():
    store = Store(TMP / "retry.db")
    run_id = store.create_run({"name": "r"})
    store.add_tasks(run_id, "k", ["u1"])
    states = []
    for _ in range(5):
        t = store.claim(run_id)
        if t is None:
            break
        states.append(store.fail(t["id"], "timeout", backoff=0))
    assert states[0] == "deferred" and states[-1] == FAILED, states
    failures = store.failures(run_id)
    assert len(failures) == 1 and "timeout" in failures[0]["error"]
    assert failures[0]["attempts"] >= 4
    print("  retry then stick          ok")
    store.close()


def test_a_fatal_error_is_not_retried():
    """Retrying a 404 three more times wastes budget and, on a rate-limited
    source, makes things worse."""
    store = Store(TMP / "fatal.db")
    run_id = store.create_run({"name": "f"})
    attempts = {"n": 0}

    def fetch(unit, ctx):
        attempts["n"] += 1
        raise Fatal("no such place")
        yield                                       # pragma: no cover

    res = collect(store, run_id, "k", ["u1"], fetch, workers=1,
                  rate=RateLimiter(per_second=10000))
    assert attempts["n"] == 1, "a fatal error was retried %d times" % attempts["n"]
    assert res.units_failed == 1 and not res.complete
    print("  fatal not retried         ok")
    store.close()


def test_one_bad_unit_does_not_cost_the_others():
    store = Store(TMP / "iso.db")
    run_id = store.create_run({"name": "iso"})

    def fetch(unit, ctx):
        if unit == "bad":
            raise Fatal("this one is broken")
        yield Business(cid="%s:1" % unit, name=unit, category="Dentist")

    res = collect(store, run_id, "k", ["a", "bad", "b", "c"], fetch, workers=2,
                  rate=RateLimiter(per_second=10000))
    assert res.collected == 3, res.collected
    assert res.units_failed == 1
    assert store.result_count(run_id) == 3
    print("  bad unit isolated         ok")
    store.close()


def test_the_breaker_stops_a_collection_gone_bad():
    store = Store(TMP / "brk.db")
    run_id = store.create_run({"name": "b"})
    seen = {"n": 0}

    def fetch(unit, ctx):
        seen["n"] += 1
        raise RuntimeError("429 too many requests")
        yield                                       # pragma: no cover

    res = collect(store, run_id, "k", ["u%03d" % i for i in range(200)], fetch,
                  workers=2, rate=RateLimiter(per_second=10000),
                  breaker=FailureBreaker(min_sample=10, window=20))
    assert res.stopped and "throttl" in res.stopped.lower()
    assert seen["n"] < 200, "burned the whole queue during an outage: %d" % seen["n"]
    left = store.pending_count(run_id)
    assert left > 0, "no units were left for the next attempt"
    print("  breaker stops collection  ok  (%d units attempted, %d still queued)"
          % (seen["n"], left))
    store.close()


def test_throttling_slows_the_shared_limiter():
    limiter = RateLimiter(per_second=100)
    before = limiter.interval
    store = Store(TMP / "slow.db")
    run_id = store.create_run({"name": "s"})

    def fetch(unit, ctx):
        raise RuntimeError("HTTP 429 rate limit")
        yield                                       # pragma: no cover

    collect(store, run_id, "k", ["u%d" % i for i in range(6)], fetch,
            workers=1, rate=limiter, breaker=FailureBreaker(min_sample=999))
    assert limiter.interval > before, "a 429 did not slow anything down"
    slowed = limiter.interval
    for _ in range(500):
        limiter.ease()
    assert limiter.interval == limiter.base_interval, \
        "easing did not return to the configured rate"
    assert limiter.interval >= before, \
        "easing sped past the configured limit (%.4f < %.4f)" % (
            limiter.interval, before)
    print("  429 slows the limiter     ok  (%.3fs -> %.3fs -> back to %.3fs)"
          % (before, slowed, limiter.interval))
    store.close()


def test_the_limiter_is_shared_not_per_worker():
    """Six workers each 'waiting a second' still make six requests a second."""
    limiter = RateLimiter(per_second=20)
    stamps: list = []
    lock = __import__("threading").Lock()
    store = Store(TMP / "share.db")
    run_id = store.create_run({"name": "sh"})

    def fetch(unit, ctx):
        with lock:
            stamps.append(time.monotonic())
        return []

    t0 = time.monotonic()
    collect(store, run_id, "k", ["u%d" % i for i in range(20)], fetch,
            workers=8, rate=limiter)
    elapsed = time.monotonic() - t0
    assert len(stamps) == 20
    assert elapsed >= 0.85, \
        "20 requests at 20/s finished in %.2fs -- the limiter is per-worker" % elapsed
    print("  limiter shared            ok  (20 units in %.2fs at 20/s)" % elapsed)
    store.close()


def test_two_connections_never_claim_the_same_task():
    """claim() is now select-then-update (RETURNING needs SQLite 3.35, older
    than Python 3.9 ships on common systems). Two connections -- as two
    processes would have -- must still never take the same task."""
    import threading
    db = TMP / "twoconn.db"
    a, b = Store(db), Store(db)
    run_id = a.create_run({"name": "two"})
    a.add_tasks(run_id, "k", ["u%03d" % i for i in range(300)])
    got = {"a": [], "b": []}
    # Both start together, and each yields after every claim: without that one
    # thread can drain the queue before the other is scheduled, and nothing
    # was contended -- so nothing was tested.
    gate = threading.Barrier(2)

    def drain(store, key):
        gate.wait()
        while True:
            t = store.claim(run_id)
            if t is None:
                return
            got[key].append(t["key"])
            time.sleep(0.0005)

    ts = [threading.Thread(target=drain, args=(a, "a")),
          threading.Thread(target=drain, args=(b, "b"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    claimed = got["a"] + got["b"]
    assert len(claimed) == 300 and len(set(claimed)) == 300, \
        "%d claims, %d distinct" % (len(claimed), len(set(claimed)))
    assert got["a"] and got["b"], "one connection never got a turn"
    a.close()
    b.close()
    print("  claims are exclusive      ok  (%d/%d split)" % (len(got["a"]), len(got["b"])))


def test_a_durable_cli_run_keeps_its_counts():
    """The CLI saved the qualified/rejected counts, then overwrote them a few
    lines later with the collector's own stats."""
    import json as _json
    from unittest import mock
    import httpx
    from kerb import cli

    def request(self, method, url, **kw):
        if "nominatim" in str(url):
            return httpx.Response(200, json=[{"boundingbox": ["1", "2", "3", "4"]}],
                                  request=httpx.Request("GET", "x"))
        return httpx.Response(200, json={"elements": [
            {"type": "node", "id": i, "lat": 1.5, "lon": 3.5,
             "tags": {"name": "Roofer %d" % i, "craft": "roofer"}} for i in range(3)]},
            request=httpx.Request("POST", "x"))

    camp = TMP / "durable.yaml"
    camp.write_text("name: dur\nsources: [{id: overpass, options: {pause: 0, per_second: 1000}}]\n"
                    "where: {places: [Somewhere]}\nwhat: {packs: [trades/roofing]}\n")
    db = TMP / "durable-cli.db"
    out = TMP / "durable-out.json"
    with mock.patch.object(httpx.Client, "request", request), \
            mock.patch("time.sleep", lambda *a: None):
        code = cli.main(["run", str(camp), "--durable", "--state-db", str(db),
                         "--out", str(out), "--include-rejected", "-q"])
    assert code == 0, code
    store = Store(db)
    run = store.list_runs()[0]
    stats = _json.loads(run["stats"])
    store.close()
    assert stats.get("qualified") == 3 and stats.get("total") == 3, stats
    assert len(_json.loads(out.read_text())["results"]) == 3
    print("  durable counts survive    ok")


def test_reading_a_page_decodes_only_the_page():
    """Listing a run decoded every stored row to return 200 of them -- 17s on a
    100,000-row run. The outline lets SQLite do the filtering; it must agree
    field for field with decoding, and fall back when SQLite cannot."""
    import gc
    import sqlite3
    from kerb import store as store_mod

    store = Store(TMP / "outline.db")
    run_id = store.create_run({"name": "o"})
    rows = [{"cid": "0xB:1", "outcome": "qualified", "score": 71.5, "review_count": 40,
             "name": "Zahnarzt Müller", "category": "Dentist", "address": "1 High St",
             "signals": {"reviews": {"value": 40}}},
            {"cid": "0xa:2", "outcome": "rejected", "score": None, "review_count": None,
             "name": None, "category": "Cafe", "address": "", "signals": {}},
            {"cid": "osm:way/7", "outcome": "unevaluated", "score": 0,
             "review_count": 1204, "name": "牙科诊所", "category": None,
             "address": 'Unit 4, "The Yard"\n2 High St', "signals": {}},
            {"cid": "ÄBC:9", "outcome": "qualified", "score": 88,
             "name": "Crown Dental"}]
    store.save_results(run_id, rows)
    decoded = store.results(run_id)

    outline = store.outline(run_id)
    assert [r["cid"] for r in outline] == [r["cid"] for r in decoded]
    for o, full in zip(outline, decoded):
        for key in Store.OUTLINE:
            assert o[key] == full.get(key), (o["cid"], key, o[key], full.get(key))

    # Whole rows, in the order asked for; a vanished cid is simply absent.
    got = store.results_for(run_id, ["osm:way/7", "gone", "0xB:1"])
    assert [r["cid"] for r in got] == ["osm:way/7", "0xB:1"]
    assert got[1] == next(r for r in decoded if r["cid"] == "0xB:1")
    many = ["x%d" % i for i in range(1200)] + ["0xa:2"]     # more than one chunk
    assert [r["cid"] for r in store.results_for(run_id, many)] == ["0xa:2"]

    # One row by cid: exact, then ignoring case -- Unicode case too, which
    # SQLite's own lower() does not fold.
    assert store.find_result(run_id, "0xB:1")["score"] == 71.5
    assert store.find_result(run_id, "0XA:2")["category"] == "Cafe"
    assert store.find_result(run_id, "äbc:9")["name"] == "Crown Dental"
    assert store.find_result(run_id, "nope") is None

    # A SQLite without JSON functions: None, so the caller decodes instead.
    class NoJson:
        def __init__(self, real):
            self.real = real

        def execute(self, sql, *a):
            if "json_extract" in sql:
                raise sqlite3.OperationalError("no such function: json_extract")
            return self.real.execute(sql, *a)

    real = store.db
    store.db = NoJson(real)
    try:
        assert store.outline(run_id) is None
    finally:
        store.db = real

    # Decoding pauses the collector and always gives it back as it was.
    assert gc.isenabled()
    store.results(run_id)
    assert gc.isenabled(), "a bulk read left the garbage collector off"
    gc.disable()
    try:
        store.results(run_id)
        assert not gc.isenabled(), "a bulk read turned a disabled collector on"
    finally:
        gc.enable()
    try:
        with store_mod._collector_paused():
            raise ValueError("boom")
    except ValueError:
        pass
    assert gc.isenabled(), "an error while decoding left the collector off"
    store.close()
    print("  a page decodes a page     ok")


if __name__ == "__main__":
    print("durability — kill it and prove nothing was lost\n")
    for fn in (test_kill_9_loses_nothing_and_duplicates_nothing,
               test_a_second_kill_mid_resume_also_loses_nothing,
               test_results_and_completion_are_one_transaction,
               test_enqueueing_is_idempotent,
               test_expired_leases_come_back_by_themselves,
               test_failures_retry_then_stick_with_a_reason,
               test_a_fatal_error_is_not_retried,
               test_one_bad_unit_does_not_cost_the_others,
               test_the_breaker_stops_a_collection_gone_bad,
               test_throttling_slows_the_shared_limiter,
               test_the_limiter_is_shared_not_per_worker,
               test_two_connections_never_claim_the_same_task,
               test_a_durable_cli_run_keeps_its_counts,
               test_reading_a_page_decodes_only_the_page):
        fn()
    print("\nall durability checks passed")
