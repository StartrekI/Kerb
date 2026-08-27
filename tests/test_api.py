"""HTTP layer — routing and query validation.

Both regressions here returned HTTP 200 with wrong data rather than failing,
which is the only kind of API bug that reaches a user's spreadsheet.

Spawns a real server on a free port because the bugs live in routing and
parameter coercion, which cannot be reached by calling the handlers directly.

    python3 tests/test_api.py
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="kerb-api-"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """Spawn, wait, and always tear down -- verified, not assumed."""

    def __init__(self):
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = None

    def __enter__(self):
        # The server confines file reads to its working directory. The suite's
        # fixtures live in a temp dir, so grant that root explicitly -- which
        # is also the documented way a user widens it.
        env = {**os.environ, "PYTHONPATH": str(ROOT),
               "KERB_ALLOWED_PATHS": os.pathsep.join([str(ROOT), str(TMP)]),
               # Its own ledger. Without this the suite wrote runs into the
               # user's real ~/.kerb/state database.
               "KERB_STATE_DB": str(TMP / "test-runs.db")}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "kerb", "serve", "--port", str(self.port),
             "--no-browser", "--log-level", "error"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            if self.proc.poll() is not None:
                raise RuntimeError("server exited during startup")
            try:
                self.get("/api/health")
                return self
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("server never became ready on port %d" % self.port)

    def __exit__(self, *exc):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        assert self.proc.poll() is not None, "test server survived teardown"

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"content-type": "application/json"})
        def decode(status, raw):
            try:
                return status, json.loads(raw or "null")
            except ValueError:
                return status, raw          # a served file, not an API response
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return decode(r.status, r.read().decode())
        except urllib.error.HTTPError as e:
            return decode(e.code, e.read().decode())

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body):
        return self.call("POST", path, body)

    def run_campaign(self, campaign, wait=6.0, durable=False):
        st, run = self.post("/api/runs", {"campaign": campaign,
                                          "durable": durable})
        assert st == 202, (st, run)
        deadline = time.time() + wait
        while time.time() < deadline:
            _, s = self.get("/api/runs/" + run["id"])
            if s["status"] in ("done", "partial", "failed", "stopped",
                               "interrupted"):
                return run["id"], s
            time.sleep(0.15)
        raise AssertionError("run did not finish")


# The tests below take the server as an argument so the __main__ block can
# share one across all of them. pytest reads that argument as a fixture request,
# so provide the fixture -- without it `python -m pytest`, which the README and
# pyproject both document, errors on every test in this file.
try:
    import pytest

    @pytest.fixture(scope="module")
    def sv():
        with Server() as server:
            yield server
except ImportError:                             # pytest is an optional extra
    pass


def campaign_for(path, **over):
    c = {"name": "t",
         "sources": [{"id": "csv", "options": {"path": str(path)}}],
         "what": {"packs": ["trades/dentist"]},
         "filters": [{"signal": "trade_match", "op": "!=", "value": False}],
         "scoring": {"weights": {"trade_match": 100}}}
    c.update(over)
    return c


FIXTURE = ROOT / "tests" / "fixtures" / "gosom_export.csv"


def test_evidence_reachable_for_slash_cids(sv):
    """Overpass -- the DEFAULT source -- emits `osm:way/456`.

    A plain `{cid}` path segment cannot match a value containing a slash in any
    encoding, so the evidence drawer 404'd for every business found the default
    way. The flagship "show me why" feature was broken on the default path.
    """
    p = TMP / "osm.csv"
    p.write_text("cid,title,category,review_count\n"
                 "osm:way/456,Way Dental,Dentist,80\n"
                 "osm:node/9,Node Dental,Dentist,90\n")
    rid, _ = sv.run_campaign(campaign_for(p))

    for cid in ("osm:way/456", "osm:node/9"):
        for label, enc in (("raw", cid),
                           ("encoded", urllib.parse.quote(cid, safe=""))):
            st, body = sv.get("/api/runs/%s/businesses/%s" % (rid, enc))
            assert st == 200, "%s cid %r -> %s" % (label, cid, st)
            assert "explain" in body

    st, _ = sv.get("/api/runs/%s/businesses/osm:way/999999" % rid)
    assert st == 404, "an unknown slash cid must still 404"
    print("  slash cids reachable      ok")


def test_pagination_rejects_nonsense(sv):
    """`limit=-1` sliced rows[0:-1] and quietly dropped the last business;
    `offset=-5` wrapped around to the end. Both returned 200."""
    rid, _ = sv.run_campaign(campaign_for(FIXTURE, filters=[]))
    total = sv.get("/api/runs/%s/businesses" % rid)[1]["total"]
    assert total == 10

    for bad in ("limit=-1", "offset=-5", "limit=-1&offset=-5",
                "limit=10001", "sort=bogus", "offset=abc", "limit=abc"):
        st, _ = sv.get("/api/runs/%s/businesses?%s" % (rid, bad))
        assert st == 422, "%s should be rejected, got %s" % (bad, st)

    for good, n in (("limit=2", 2), ("limit=0", 0), ("offset=8", 2),
                    ("offset=9999", 0), ("sort=name", 10), ("sort=reviews", 10)):
        st, body = sv.get("/api/runs/%s/businesses?%s" % (rid, good))
        assert st == 200 and len(body["rows"]) == n, (good, st, body)
    print("  pagination validated      ok")


def test_rejected_rows_carry_reasons(sv):
    rid, _ = sv.run_campaign(campaign_for(
        FIXTURE, filters=[{"signal": "trade_match", "op": "!=", "value": False},
                          {"signal": "reviews", "op": ">=", "value": 30}]))
    _, body = sv.get("/api/runs/%s/businesses?qualified=false" % rid)
    assert body["total"] > 0
    for row in body["rows"]:
        assert row["reject_reason"], row["name"]
        assert row["rejected_by"], row["name"]
    print("  reject reasons present    ok")


def test_bad_weights_never_start_a_run(sv):
    st, _ = sv.post("/api/runs", {"campaign": campaign_for(
        FIXTURE, scoring={"weights": {"reviews": {"weight": 80, "cap": True}}})})
    assert st == 422, "a spec that ranks everything identically must not run"

    rid, _ = sv.run_campaign(campaign_for(FIXTURE))
    st, _ = sv.post("/api/runs/%s/rescore" % rid,
                    {"weights": {"reviews": {"weight": 80, "cap": True}}})
    assert st == 422, "rescore must apply the same check"
    print("  bad weights rejected      ok")


def test_missing_things_404(sv):
    assert sv.get("/api/runs/nosuchrun")[0] == 404
    assert sv.get("/api/runs/nosuchrun/businesses")[0] == 404
    assert sv.get("/api/runs/nosuchrun/events")[0] == 404
    assert sv.post("/api/runs/nosuchrun/stop", {})[0] == 404
    assert sv.get("/api/packs/trades/nosuchpack")[0] == 404
    # Inside an allowed root but absent -> 404. Outside it -> 403, which is
    # also why it must not be 404: a 404/403 split on out-of-root paths would
    # answer "does this file exist?" for the whole filesystem.
    assert sv.post("/api/import/inspect", {"path": str(TMP / "absent.csv")})[0] == 404
    assert sv.post("/api/import/inspect", {"path": "/nope/x.csv"})[0] == 403
    print("  404s where expected       ok")


def test_file_reads_are_confined(sv):
    """`kerb serve --host 0.0.0.0` puts the campaign builder on the network,
    and a campaign names files to read. Unconstrained, `{"path":"/etc/passwd"}`
    made the importer a file reader for anyone who could reach the port."""
    for outside in ("/etc/passwd", "/etc/hosts"):
        st, body = sv.post("/api/import/inspect", {"path": outside})
        assert st == 403, "%s was readable (%s)" % (outside, st)
        assert "outside" in json.dumps(body)

    # `..` and symlinks are resolved before the check, so neither escapes.
    st, _ = sv.post("/api/import/inspect",
                    {"path": str(ROOT) + "/../../../../etc/passwd"})
    assert st == 403, "path traversal escaped the allowed roots"

    # Starting a run is guarded too, not just the inspector.
    st, _ = sv.post("/api/runs", {"campaign": campaign_for("/etc/passwd")})
    assert st == 403, "a campaign could read outside the allowed roots"

    # Every path a campaign names, not only the source. Suppression lists were
    # unguarded: `suppress: {lists: [/etc/passwd]}` was read over HTTP, and it
    # reported success -- a file whose first line has no comma parses as a
    # single-column list of ids, so the read was silent as well as unauthorised.
    for outside in ("/etc/passwd", "/etc/hosts", "../../../../etc/passwd"):
        st, body = sv.post("/api/runs", {"campaign": dict(
            campaign_for(FIXTURE), suppress={"lists": [outside]})})
        assert st == 403, "suppression list %s was accepted (%s)" % (outside, st)
        assert "outside" in json.dumps(body)

    inside = TMP / "allowed-list.csv"
    inside.write_text("cid\n0x1a:0x00a\n")
    st, _ = sv.post("/api/runs", {"campaign": dict(
        campaign_for(FIXTURE), suppress={"lists": [str(inside)]})})
    assert st == 202, "a list inside an allowed root was refused (%s)" % st

    # Everything inside a granted root still works.
    st, body = sv.post("/api/import/inspect", {"path": str(FIXTURE)})
    assert st == 200 and body["rows"] == 10
    print("  file reads confined       ok")


def test_source_failure_does_not_lose_the_run(sv):
    """One dead source used to abort everything, discarding results already
    collected from the sources that worked."""
    p = TMP / "ok.csv"
    p.write_text("cid,title,category,review_count\n0x5:0x1,Real Dental,Dentist,90\n")
    c = campaign_for(p)
    # Point overpass at a closed port with a minimal retry budget, so the
    # failure is immediate and the test needs no network.
    c["sources"] = [{"id": "overpass",
                     "options": {"endpoint": "http://127.0.0.1:1/interpreter",
                                 "retries": 1, "backoff": 0, "pause": 0}}] + c["sources"]
    c["where"] = {"mode": "paste", "places": ["Nowhere-That-Exists-XYZQ"]}
    rid, summary = sv.run_campaign(c, wait=60)

    assert summary["status"] == "done", summary
    stats = summary["stats"]
    trouble = stats.get("source_errors") or stats.get("skipped_places")
    assert trouble, "a source that could not deliver must be reported"
    _, body = sv.get("/api/runs/%s/businesses" % rid)
    assert body["total"] >= 1, "results from the working source must survive"
    print("  source failure contained  ok")


def test_runs_survive_a_restart(sv):
    """The API used to hold runs in an in-memory dict, so restarting the
    server lost every result it had ever produced."""
    rid, summary = sv.run_campaign(campaign_for(FIXTURE))
    before = sv.get("/api/runs/%s/businesses?limit=500" % rid)[1]
    assert before["total"] > 0
    # A finished run reads from the ledger even in the process that ran it.
    # Memory is only ahead of disk while a run is still in flight, and serving
    # a retained in-memory copy is what made a requalify look like a no-op.
    assert summary["live"] is False, "a finished run must read from the ledger"

    # A brand-new process, reading the same ledger.
    from kerb.store import Store
    db = Store(TMP / "test-runs.db")
    try:
        row = db.get_run(rid)
        assert row is not None, "the run was never written to the ledger"
        assert row["state"] == "done"
        stored = list(db.results(rid))
        assert len(stored) == before["total"], \
            "%d rows in the ledger, %d over HTTP" % (len(stored), before["total"])
        assert all(r.get("cid") and "signals" in r for r in stored), \
            "stored rows must carry their evidence, or rescoring needs a refetch"
    finally:
        db.close()
    print("  runs persisted            ok")


def test_a_finished_run_still_answers_everything(sv):
    """Every read endpoint must work for a run this process did not execute."""
    rid, _ = sv.run_campaign(campaign_for(FIXTURE))
    listed = sv.get("/api/runs")[1]
    assert any(r["id"] == rid for r in listed)

    st, body = sv.get("/api/runs/%s/businesses/%s"
                      % (rid, sv.get("/api/runs/%s/businesses?limit=1" % rid)[1]
                         ["rows"][0]["cid"]))
    assert st == 200 and body["explain"], "evidence was not rebuilt from storage"

    # Rescoring reads and writes the ledger, so the new ranking outlives it too.
    st, res = sv.post("/api/runs/%s/rescore" % rid,
                      {"weights": {"reviews": {"weight": 100, "scale": "linear",
                                               "cap": 200}}})
    assert st == 200 and res["rescored"] > 0
    after = sv.get("/api/runs/%s/businesses?qualified=true&sort=score" % rid)[1]
    assert after["rows"][0]["score"] is not None

    from kerb.store import Store
    db = Store(TMP / "test-runs.db")
    try:
        persisted = {r["cid"]: r.get("score") for r in db.results(rid)}
        for row in after["rows"]:
            assert persisted[row["cid"]] == row["score"], \
                "a rescore was not written back to the ledger"
    finally:
        db.close()
    print("  finished run answers      ok")


def test_the_api_honours_a_suppress_block(sv):
    """It used to accept `suppress:`, validate it, and ignore it -- config that
    looks supported and is not."""
    lst = TMP / "contacted.csv"
    lst.write_text("cid\n0x1a:0x00a\n")

    base = campaign_for(FIXTURE)
    _, plain = sv.run_campaign(base)
    _, hidden = sv.run_campaign({**base, "suppress": {"lists": [str(lst)]}})

    assert hidden["total"] == plain["total"] - 1, \
        "suppression was ignored (%d vs %d)" % (hidden["total"], plain["total"])
    assert hidden["stats"].get("suppressed") == 1
    print("  suppression honoured      ok")


def test_an_unreadable_suppression_list_fails_the_run(sv):
    bad = TMP / "no-ids.csv"
    bad.write_text("name,phone\nAcme,123\n")
    _, summary = sv.run_campaign({**campaign_for(FIXTURE),
                                  "suppress": {"lists": [str(bad)]}})
    assert summary["status"] == "failed", summary["status"]
    assert "Refusing to continue" in (summary.get("error") or "")
    print("  bad list fails the run    ok")


def test_export_applies_the_output_block(sv):
    """`output:` had nowhere to mean anything over HTTP, so a campaign that
    specified columns or a mail-merge template got none of them from the UI."""
    rid, _ = sv.run_campaign({**campaign_for(FIXTURE, filters=[]),
                              "scoring": {"weights": {"reviews": {
                                  "weight": 100, "scale": "log", "cap": 300}}},
                              "output": {"template": {"Company": "name",
                                                      "Phone": "phone"},
                                         "min_score": 60}})
    st, body = sv.get("/api/runs/%s/export?format=csv" % rid)
    assert st == 200, st
    lines = [l for l in str(body).strip().splitlines() if l]
    assert lines[0] == "Company,Phone", lines[0]
    assert len(lines) > 1, "min_score removed everything"

    st, js = sv.get("/api/runs/%s/export?format=json" % rid)
    assert st == 200 and len(js["results"]) == len(lines) - 1
    print("  export shapes output      ok")


def test_requalify_over_http(sv):
    """Changes nothing until asked, then changes exactly what it said."""
    base = campaign_for(FIXTURE)
    rid, before = sv.run_campaign(base)
    tight = {**base, "filters": base["filters"] +
             [{"signal": "reviews", "op": ">=", "value": 100}]}

    st, diff = sv.post("/api/runs/%s/requalify" % rid, {"campaign": tight})
    assert st == 200 and diff["changed"] > 0 and diff["applied"] is False
    assert sv.get("/api/runs/%s" % rid)[1]["qualified"] == before["qualified"], \
        "a diff-only requalify changed the ledger"
    assert all(c["was"] and c["now"] and c["cid"] for c in diff["changes"])

    st, applied = sv.post("/api/runs/%s/requalify" % rid,
                          {"campaign": tight, "apply": True})
    assert st == 200 and applied["applied"] is True
    after = sv.get("/api/runs/%s" % rid)[1]
    assert after["qualified"] == before["qualified"] - diff["changed"]
    rows = sv.get("/api/runs/%s/businesses?qualified=true" % rid)[1]
    assert rows["total"] == after["qualified"], "summary and rows disagree"

    st, _ = sv.post("/api/runs/nosuchrun/requalify", {})
    assert st == 404
    print("  requalify over http       ok")


def test_tasks_endpoint(sv):
    rid, _ = sv.run_campaign(campaign_for(FIXTURE))
    st, body = sv.get("/api/runs/%s/tasks" % rid)
    assert st == 200 and body["counts"]["results"] > 0
    assert sv.get("/api/runs/nosuchrun/tasks")[0] == 404
    print("  tasks endpoint            ok")


DURABLE = {"name": "durable",
           "sources": [{"id": "overpass",
                        "options": {"endpoint": "http://127.0.0.1:1/x",
                                    "retries": 1, "backoff": 0, "pause": 0,
                                    "per_second": 1000}}],
           "where": {"mode": "paste", "places": ["Nowhere-A", "Nowhere-B"]},
           "what": {"packs": ["trades/roofing"]},
           "filters": [], "scoring": {"weights": {"trade_match": 100}},
           # One attempt per unit, so an unreachable place is recorded as
           # failed rather than deferred for a retry the test will not wait for.
           # `limits.attempts` is the UNIT budget; the source's own `retries`
           # is how many times one HTTP call is retried inside one attempt.
           "limits": {"attempts": 1}}


def test_a_durable_run_needs_places(sv):
    """A file source is a single unit of work; making it 'durable' would be a
    queue of one and imply a resumability it cannot offer."""
    st, body = sv.post("/api/runs", {"campaign": campaign_for(FIXTURE),
                                     "durable": True})
    assert st == 422, st
    assert "places" in json.dumps(body)
    print("  durable needs places      ok")


def test_durable_runs_record_unit_state(sv):
    """Every place becomes a task, and its outcome is inspectable."""
    rid, summary = sv.run_campaign(DURABLE, wait=60, durable=True)
    st, tasks = sv.get("/api/runs/%s/tasks" % rid)
    assert st == 200
    counted = sum(tasks["counts"][k] for k in ("done", "failed", "pending",
                                               "deferred", "running"))
    assert counted == 2, "two places should be two units, got %s" % tasks["counts"]
    # Unreachable places must be recorded with a reason, never silently dropped
    # so the run merely returns less.
    assert tasks["counts"]["failed"] == 2, tasks["counts"]
    assert all(f["error"] and f["key"] for f in tasks["failures"])
    # And the run must not call itself done when it collected nothing.
    assert summary["status"] != "done", summary["status"]
    print("  durable unit state        ok")


def test_resume_is_idempotent_and_guarded(sv):
    rid, _ = sv.run_campaign(DURABLE, wait=60, durable=True)

    # Resuming a finished run is a no-op, not an error and not a repeat.
    before = sv.get("/api/runs/%s" % rid)[1]["total"]
    st, body = sv.post("/api/runs/%s/resume" % rid, {})
    assert st == 202, st
    deadline = time.time() + 60
    while time.time() < deadline:
        if sv.get("/api/runs/%s" % rid)[1]["status"] != "running":
            break
        time.sleep(0.2)
    assert sv.get("/api/runs/%s" % rid)[1]["total"] == before, \
        "resuming duplicated results"

    assert sv.post("/api/runs/nosuchrun/resume", {})[0] == 404
    # A non-durable run has no places, so there is nothing to resume.
    plain, _ = sv.run_campaign(campaign_for(FIXTURE))
    assert sv.post("/api/runs/%s/resume" % plain, {})[0] == 422
    print("  resume guarded            ok")


def test_durable_runs_store_verdicts_not_just_businesses(sv):
    """Collection stores businesses; qualification must upgrade those same rows
    in place. Otherwise a durable run leaves the ledger holding rows with no
    outcome and no score -- rows that export, requalify and the qualified count
    cannot use."""
    rid, _ = sv.run_campaign({**DURABLE,
                              "sources": [{"id": "csv",
                                           "options": {"path": str(FIXTURE)}}],
                              "where": {}, "what": {"packs": ["trades/dentist"]}},
                             wait=30, durable=False)
    _, rows = sv.get("/api/runs/%s/businesses?limit=50" % rid)
    assert rows["total"] > 0
    for row in rows["rows"]:
        assert row.get("outcome"), "a stored row has no verdict"
        assert "signals" in row, "a stored row carries no evidence"
    print("  verdicts stored           ok")


def test_one_unreadable_run_does_not_empty_the_list(sv):
    """A truncated write or a hand-edited row must cost that run its
    statistics, not take the whole listing down with it."""
    from kerb.store import Store
    rid, _ = sv.run_campaign(campaign_for(FIXTURE))
    db = Store(TMP / "test-runs.db")
    try:
        db.db.execute("UPDATE runs SET stats='{not json' WHERE id=?", (rid,))
    finally:
        db.close()

    st, body = sv.get("/api/runs/%s" % rid)
    assert st == 200, "a corrupt stats blob made the run unreadable (%s)" % st
    assert "unreadable" in json.dumps(body["stats"])

    st, listed = sv.get("/api/runs")
    assert st == 200 and isinstance(listed, list) and listed, \
        "one bad row emptied the whole listing"
    assert any(r["id"] == rid for r in listed), \
        "the damaged run was hidden rather than shown as damaged"
    print("  corrupt row contained     ok")


def test_stopping_keeps_what_was_collected(sv):
    # Big enough that the stop reliably lands mid-run rather than racing the
    # finish -- a test whose meaning depends on timing tests nothing.
    n = 40_000
    big = TMP / "stopme.csv"
    big.write_text("cid,title,category,review_count\n" + "".join(
        "0xs:0x%05x,Biz %d,Dentist,%d\n" % (i, i, 40 + i) for i in range(n)))
    st, run = sv.post("/api/runs", {"campaign": dict(
        campaign_for(big), sources=[{"id": "csv", "options": {"path": str(big)}}])})
    assert st == 202
    time.sleep(0.5)
    sv.post("/api/runs/%s/stop" % run["id"], {})

    deadline = time.time() + 90
    summary = None
    while time.time() < deadline:
        summary = sv.get("/api/runs/%s" % run["id"])[1]
        if summary["status"] != "running":
            break
        time.sleep(0.2)
    assert summary and summary["status"] in ("stopped", "done"), summary["status"]

    kept = sv.get("/api/runs/%s/businesses?limit=1" % run["id"])[1]["total"]
    assert kept > 0, "stopping discarded every partial result"
    if summary["status"] == "stopped":
        assert kept < n, "reported stopped but processed the whole file"
    print("  stop keeps partials       ok  (%s at %d rows)"
          % (summary["status"], kept))


def test_ui_is_self_contained(sv):
    """A strict-CSP-free local page still must not depend on the network."""
    import urllib.request as u
    with u.urlopen(sv.base + "/", timeout=10) as r:
        html = r.read().decode()
    for host in ("cdn.", "unpkg.com", "jsdelivr", "fonts.googleapis.com",
                 "fonts.gstatic.com", "//ajax."):
        assert host not in html, "UI reaches out to %s" % host

    # The dev harness lives in static/ so it can be served same-origin, so the
    # guarantee that matters is that packaging cannot pick it up. A glob like
    # static/*.html would ship it to every user.
    pyproject = (ROOT / "pyproject.toml").read_text()
    data_line = next(l for l in pyproject.splitlines() if l.strip().startswith("kerb = ["))
    assert "static/*.html" not in data_line, \
        "package-data glob would ship the dev harness: %s" % data_line
    assert "static/index.html" in data_line
    print("  UI self-contained         ok")


if __name__ == "__main__":
    try:
        import fastapi, uvicorn  # noqa: F401
    except ImportError:
        print("skipped — server extras not installed "
              "(pip install 'kerb[server]')")
        raise SystemExit(0)

    print("api — routing and validation regressions\n")
    with Server() as sv:
        test_evidence_reachable_for_slash_cids(sv)
        test_pagination_rejects_nonsense(sv)
        test_rejected_rows_carry_reasons(sv)
        test_bad_weights_never_start_a_run(sv)
        test_missing_things_404(sv)
        test_file_reads_are_confined(sv)
        test_source_failure_does_not_lose_the_run(sv)
        test_runs_survive_a_restart(sv)
        test_a_finished_run_still_answers_everything(sv)
        test_the_api_honours_a_suppress_block(sv)
        test_an_unreadable_suppression_list_fails_the_run(sv)
        test_export_applies_the_output_block(sv)
        test_requalify_over_http(sv)
        test_tasks_endpoint(sv)
        test_a_durable_run_needs_places(sv)
        test_durable_runs_record_unit_state(sv)
        test_resume_is_idempotent_and_guarded(sv)
        test_durable_runs_store_verdicts_not_just_businesses(sv)
        test_one_unreadable_run_does_not_empty_the_list(sv)
        test_stopping_keeps_what_was_collected(sv)
        test_ui_is_self_contained(sv)
    print("\nall api checks passed")
