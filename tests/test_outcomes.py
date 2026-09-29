"""Never record a failure to measure as a verdict about the business.

This is the single most expensive lesson carried over from the pipeline this
project grew out of. A failed measurement was written down as
`status='rejected'` with the error as the reason, so a few minutes of
throttling permanently marked thousands of perfectly good businesses as
rejected — and nothing ever went back for them.

Two defences, both tested here:

  1. a broken measurement produces UNEVALUATED, never REJECTED
  2. when breakage becomes the norm, the run STOPS, so the businesses it has
     not reached yet stay untouched instead of being written off in bulk

    python3 tests/test_outcomes.py
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerb import scoring, signals                             # noqa: E402
from kerb.campaign import Campaign                            # noqa: E402
from kerb.health import DiscoveryHealth, FailureBreaker       # noqa: E402
from kerb.models import Business, Outcome, Signal, Verdict    # noqa: E402
from kerb.pipeline import Pipeline                            # noqa: E402
import kerb.signals.web_presence as web_presence              # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="kerb-out-"))
MANY = TMP / "many.csv"
MANY.write_text("cid,title,category,review_count,website\n" + "".join(
    "0xa:0x%02x,Dental %d,Dentist,%d,https://site%d.com\n" % (i, i, 50 + i, i)
    for i in range(40)))

CAMPAIGN = {
    "sources": [{"id": "csv", "options": {"path": str(MANY)}}],
    "what": {"packs": ["trades/dentist"]},
    "filters": [{"signal": "trade_match", "op": "!=", "value": False},
                {"signal": "web_presence", "op": "in",
                 "value": ["none", "booking_only", "social_only"]}],
}


def outage():
    """web_presence throws for every business, as during a network outage."""
    return mock.patch.object(web_presence, "_classify",
                             side_effect=ConnectionError("network down"))


# ------------------------------------------------------- the core distinction

def test_a_broken_measurement_is_not_a_rejection():
    biz = Business(cid="0x1:0x1", name="Perfectly Good Dentist",
                   category="Dentist", review_count=140, website="https://x.com")
    ctx = signals.Context(options={"trade_match": {"pack": "trades/dentist"}})
    v = Verdict(business=biz)
    with outage():
        v.add(signals.compute("web_presence", biz, ctx))
    v.add(signals.compute("trade_match", biz, ctx))

    ok, failed_by, reason = scoring.evaluate(v, CAMPAIGN["filters"])
    assert not ok
    assert failed_by.startswith("unmeasurable:"), failed_by
    assert "could not be measured" in reason and "not judged" in reason
    assert "ConnectionError" in reason, "the cause belongs in the record"
    print("  broken != rejected        ok")


def test_a_real_rejection_is_still_a_rejection():
    """The new state must not swallow genuine verdicts."""
    biz = Business(cid="0x1:0x2", name="Has A Website", category="Dentist",
                   review_count=140, website="https://theirown.com")
    ctx = signals.Context(options={"trade_match": {"pack": "trades/dentist"}})
    v = Verdict(business=biz)
    for n in ("web_presence", "trade_match"):
        v.add(signals.compute(n, biz, ctx))

    ok, failed_by, reason = scoring.evaluate(v, CAMPAIGN["filters"])
    assert not ok
    assert failed_by == "web_presence", failed_by
    assert not failed_by.startswith("unmeasurable:")
    assert "owned_domain" in reason
    print("  real rejection intact     ok")


def test_low_confidence_is_not_failure():
    """A signal that legitimately cannot tell has still measured correctly.

    `establishment_age` returns confidence 0.0 with a note when there are no
    dated reviews. That is an answer, not a breakage, and must not be treated
    as one — otherwise ordinary sparse data would trip the breaker.
    """
    s = signals.compute("establishment_age", Business(cid="0x1:0x3"),
                        signals.Context())
    assert s.confidence == 0.0
    assert not s.failed, "a confident 'I cannot tell' was mistaken for a crash"
    print("  low confidence != failure ok")


# ------------------------------------------------------------- the run itself

def test_an_outage_marks_nothing_as_rejected():
    pipe = Pipeline(Campaign.from_dict(CAMPAIGN))
    with outage():
        out = list(pipe.run())

    assert pipe.stats.rejected == 0, \
        "an outage produced %d rejections" % pipe.stats.rejected
    assert pipe.stats.unevaluated == len(out)
    assert all(v.outcome is Outcome.UNEVALUATED for v in out)
    assert all(v.failed_signals == ["web_presence"] for v in out)
    print("  outage rejects nothing    ok")


def test_the_breaker_stops_the_run_early():
    """Stopping is the point: the businesses never reached stay untouched."""
    pipe = Pipeline(Campaign.from_dict(CAMPAIGN))
    with outage():
        out = list(pipe.run())

    assert pipe.breaker.tripped
    assert len(out) < 40, "the whole dataset was consumed during an outage"
    assert len(out) <= 12, "stopped late: %d businesses burned" % len(out)
    assert pipe.stats.stopped_reason and "throttling" in pipe.stats.stopped_reason
    print("  breaker stops early       ok  (%d of 40 touched)" % len(out))


def test_a_healthy_run_is_untouched_by_any_of_this():
    pipe = Pipeline(Campaign.from_dict(CAMPAIGN))
    out = list(pipe.run())
    assert not pipe.breaker.tripped
    assert pipe.stats.unevaluated == 0
    assert len(out) == 40, len(out)
    assert all(v.outcome is not Outcome.UNEVALUATED for v in out)
    print("  healthy run unaffected    ok")


def test_qualify_rate_excludes_the_unmeasured():
    """Counting a business we never measured as a miss understates the rate by
    exactly the size of the outage."""
    pipe = Pipeline(Campaign.from_dict(CAMPAIGN))
    with outage():
        list(pipe.run())
    d = pipe.stats.to_dict()
    assert d["unevaluated"] > 0
    assert d["qualified"] + d["rejected"] == 0
    assert d["qualify_rate"] == 0.0, "a rate was invented from no measurements"
    print("  rate excludes unmeasured  ok")


def test_a_brief_blip_does_not_stop_the_run():
    """Only sustained failure means the window has closed."""
    calls = {"n": 0}
    real = web_presence._classify

    def flaky(host, ctx):
        calls["n"] += 1
        if calls["n"] in (2, 5):
            raise ConnectionError("blip")
        return real(host, ctx)

    pipe = Pipeline(Campaign.from_dict(CAMPAIGN))
    with mock.patch.object(web_presence, "_classify", flaky):
        out = list(pipe.run())

    assert not pipe.breaker.tripped, "two blips ended a healthy run"
    assert len(out) == 40
    assert pipe.stats.unevaluated == 2, pipe.stats.unevaluated
    assert pipe.stats.rejected == 38
    print("  brief blip tolerated      ok")


# --------------------------------------------------------------------- resume

def test_a_run_resumes_instead_of_starting_over():
    first = Pipeline(Campaign.from_dict(CAMPAIGN))
    done = [v.business.cid for v in list(first.run())[:25]]

    second = Pipeline(Campaign.from_dict(CAMPAIGN), skip=done)
    out = list(second.run())
    assert second.stats.resumed == 25
    assert len(out) == 15, "resumed run redid work: %d" % len(out)
    assert not ({v.business.cid for v in out} & set(done))
    print("  resume skips done work    ok")


def test_resume_is_case_insensitive_on_cid():
    """cids are normalised to lowercase on the Business; a checkpoint written
    by another tool may not be."""
    pipe = Pipeline(Campaign.from_dict(CAMPAIGN), skip=["0XA:0X00", "0Xa:0x01"])
    out = list(pipe.run())
    assert len(out) == 38, len(out)
    print("  resume normalises cids    ok")


def test_a_checkpoint_resume_measures_the_unevaluated_again():
    """Resume skipped every cid in the journal -- including the ones the
    breaker left unevaluated precisely so the next attempt could measure them.
    "Unevaluated" became permanent, and the CLI said they were "still queued"."""
    import json
    import subprocess
    root = Path(__file__).resolve().parent.parent
    data = TMP / "ckpt.csv"
    data.write_text("cid,title,category,review_count\n"
                    "0xc:0x1,Done Dental,Dentist,90\n"
                    "0xc:0x2,Blip Dental,Dentist,90\n"
                    "0xc:0x3,New Dental,Dentist,90\n")
    camp = TMP / "ckpt.yaml"
    camp.write_text("name: ckpt\nsources: [{id: csv, options: {path: %s}}]\n"
                    "what: {packs: [trades/dentist]}\n" % data)
    journal = TMP / "ckpt.jsonl"
    journal.write_text(
        json.dumps({"cid": "0xc:0x1", "name": "Done Dental", "outcome": "qualified",
                    "qualified": True, "score": 50.0}) + "\n" +
        json.dumps({"cid": "0xc:0x2", "name": "Blip Dental", "outcome": "unevaluated",
                    "qualified": False}) + "\n")
    out = TMP / "ckpt-out.json"
    proc = subprocess.run(
        [sys.executable, "-m", "kerb", "run", str(camp), "--checkpoint", str(journal),
         "--include-rejected", "--out", str(out), "-q"],
        cwd=str(root), capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "PYTHONPATH": str(root)})
    assert proc.returncode in (0, 3), proc.stderr[-600:]
    rows = {r["cid"]: r for r in json.loads(out.read_text())["results"]}
    assert set(rows) == {"0xc:0x1", "0xc:0x2", "0xc:0x3"}, sorted(rows)
    assert rows["0xc:0x2"]["outcome"] == "qualified", "the unevaluated row was skipped"
    assert rows["0xc:0x1"]["score"] == 50.0, "a decided row was re-measured"
    print("  resume re-measures unevaluated ok")


def test_a_malformed_campaign_is_explained_not_traced():
    """The UI's Copy-as-YAML wrote `op: >=`, and `kerb run` answered with a
    PyYAML traceback. Whatever the cause, a bad file gets a sentence."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    bad = TMP / "bad.yaml"
    bad.write_text("name: x\nfilters:\n  - {signal: reviews, op: >=, value: 3}\n")
    proc = subprocess.run([sys.executable, "-m", "kerb", "run", str(bad)],
                          cwd=str(root), capture_output=True, text=True, timeout=60,
                          env={**__import__("os").environ, "PYTHONPATH": str(root)})
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr, proc.stderr[-400:]
    assert "not valid YAML" in proc.stderr and "line" in proc.stderr, proc.stderr
    print("  bad YAML explained        ok")


def test_a_missing_measurement_is_never_found_out_not_rejected():
    """Found by running real-shaped data: a business with no review count was
    REJECTED with "reviews is None, needs >= 30". The README, the gmaps source
    and the UI all promise "never found out" -- and Google's signed-out view
    has no counts at all, so this rejected every Maps business."""
    data = TMP / "nocount.csv"
    data.write_text("cid,title,category,review_count\n"
                    "0xe:0x1,Has Count,Dentist,80\n"
                    "0xe:0x2,No Count,Dentist,\n"
                    "0xe:0x3,Few Reviews,Dentist,4\n")
    pipe = Pipeline(Campaign.from_dict({
        "sources": [{"id": "csv", "options": {"path": str(data)}}],
        "what": {"packs": ["trades/dentist"]},
        "filters": [{"signal": "reviews", "op": ">=", "value": 30}]}))
    got = {v.business.name: v for v in pipe.run()}
    assert got["Has Count"].outcome is Outcome.QUALIFIED
    assert got["Few Reviews"].outcome is Outcome.REJECTED, "a real 4 is still a verdict"
    unknown = got["No Count"]
    assert unknown.outcome is Outcome.UNEVALUATED, unknown.reject_reason
    assert "None" not in unknown.reject_reason and "unknown" in unknown.reject_reason
    assert pipe.stats.rejected == 1 and pipe.stats.unevaluated == 1
    assert not pipe.breaker.tripped, "an unknown count is not an outage"
    print("  missing count not judged  ok")


# --------------------------------------------------------------- health wiring

def test_discovery_health_is_reported():
    h = DiscoveryHealth()
    for i, n in enumerate([58, 59, 74, 20, 0, 20, 0, 36, 0, 20]):
        h.record("place%d" % i, n)
    s = h.summary()
    assert s["verdict"] == "throttled"
    assert s["baseline"] >= 55, "baseline contaminated: %s" % s["baseline"]
    assert s["zero_results"] == 3 and s["suspect"] >= 6
    assert h.advice()
    print("  discovery health          ok")


def test_breaker_defaults_are_configurable():
    c = Campaign.from_dict({**CAMPAIGN,
                            "limits": {"breaker": {"rate": 0.9, "min_sample": 30}}})
    pipe = Pipeline(c)
    assert pipe.breaker.rate == 0.9 and pipe.breaker.min_sample == 30
    with outage():
        out = list(pipe.run())
    assert len(out) >= 30, "a raised threshold was ignored"
    print("  breaker configurable      ok")


def test_a_shared_veto_never_excludes_the_trade_you_asked_for():
    """The shared list carries "cafe" so a rooftop cafe is not a DENTIST.
    Applied blindly it also rejected every result of a campaign whose trade IS
    cafes: the tool refused to find the thing it was asked to find, and blamed
    each business for it with a confident "vetoed category"."""
    from kerb.campaign import Campaign
    from kerb import signals as sig
    from kerb.models import Business

    typed = Campaign.from_dict({"what": {"trade": "cafes"},
                                "sources": [{"id": "csv", "options": {"path": "x"}}]})
    ctx = sig.Context(options=typed.signal_options)
    ctx.packs = ctx.packs.with_pack(typed.typed_pack)
    s = sig.compute("trade_match", Business(cid="0x1:0x1", name="Workers Cafe",
                                            category="Cafe"), ctx)
    assert s.value is not False, s.evidence
    assert s.evidence.get("reason") != "vetoed category", s.evidence

    # ...and the shipped pack must still veto it, or the fix broke the feature.
    dent = Campaign.from_dict({"what": {"packs": ["trades/dentist"]},
                               "sources": [{"id": "csv", "options": {"path": "x"}}]})
    d = sig.compute("trade_match", Business(cid="0x1:0x2", name="Rooftop Cafe",
                                            category="Cafe"),
                    sig.Context(options=dent.signal_options))
    assert d.value is False and d.evidence["reason"] == "vetoed category", d.evidence
    print("  shared veto defers to trade ok")


def test_an_absent_review_count_is_not_zero_reviews():
    """`or 0` conflated "this business has no reviews" with "this SOURCE does
    not report review counts", and told the second one that a 4.8 was "too few
    reviews to mean anything" -- a confident claim about an unmeasured thing."""
    from kerb import signals as sig
    from kerb.models import Business
    ctx = sig.Context(options={})

    unknown = sig.compute("rating_band",
                          Business(cid="0x1:0x1", name="X", rating=4.8), ctx)
    assert unknown.value == "excellent", unknown.evidence
    assert unknown.confidence < 0.5, "an unweighed rating cannot be confident"
    assert unknown.evidence["reviews"] is None

    genuine = sig.compute("rating_band",
                          Business(cid="0x1:0x2", name="Y", rating=4.8,
                                   review_count=2), ctx)
    assert genuine.value == "unrated", genuine.evidence
    assert genuine.evidence["reviews"] == 2
    print("  absent != zero reviews     ok")


def test_exit_codes_say_whether_the_run_is_complete():
    """A script scheduling `kerb run` reads its exit code. A run cut short by
    a budget exited 0, exactly like a complete one, so a cron job took a capped
    run as the whole market."""
    import yaml
    from kerb import cli
    root = Path(__file__).resolve().parent.parent
    base = yaml.safe_load((root / "examples" / "dentists-from-csv.yaml").read_text())
    base["sources"][0]["options"]["path"] = str(root / "tests" / "fixtures"
                                                / "gosom_export.csv")

    def run(campaign):
        path = TMP / "exit.yaml"
        path.write_text(yaml.safe_dump(campaign))
        try:
            return cli.main(["run", str(path), "--out", str(TMP / "exit.csv"), "-q"])
        except SystemExit as exc:
            return exc.code

    assert run(base) == 0, "complete, with results"
    nothing = {**base, "filters": base["filters"] + [
        {"signal": "reviews", "op": ">=", "value": 100000}]}
    assert run(nothing) == 3, "complete, nothing qualified"
    capped = {**base, "limits": {"max_results": 2}}
    assert run(capped) == 4, "cut short by a budget is incomplete"
    broken = {**base, "filters": [{"signal": "reviewz", "op": ">=", "value": 1}]}
    assert run(broken) == 2, "an invalid campaign never runs"
    print("  exit codes                ok")


if __name__ == "__main__":
    print("outcomes — a failure to measure is not a verdict\n")
    for fn in (
               test_a_shared_veto_never_excludes_the_trade_you_asked_for,
               test_an_absent_review_count_is_not_zero_reviews,test_a_broken_measurement_is_not_a_rejection,
               test_a_real_rejection_is_still_a_rejection,
               test_low_confidence_is_not_failure,
               test_an_outage_marks_nothing_as_rejected,
               test_the_breaker_stops_the_run_early,
               test_a_healthy_run_is_untouched_by_any_of_this,
               test_qualify_rate_excludes_the_unmeasured,
               test_a_brief_blip_does_not_stop_the_run,
               test_a_run_resumes_instead_of_starting_over,
               test_resume_is_case_insensitive_on_cid,
               test_a_checkpoint_resume_measures_the_unevaluated_again,
               test_a_malformed_campaign_is_explained_not_traced,
               test_a_missing_measurement_is_never_found_out_not_rejected,
               test_discovery_health_is_reported,
               test_breaker_defaults_are_configurable,
               test_exit_codes_say_whether_the_run_is_complete):
        fn()
    print("\nall outcome checks passed")
