"""Memory across runs — suppression, delta, re-judging, ordering.

Without these, kerb is a search you re-run. With them it is a pipeline someone
runs every week. Each one existed as a hand-rolled script in the predecessor.

The subtle case, and the reason this file is long: **soft suppression must not
hide a business whose verdict changed.** A practice you passed on last month
because it had a website, whose site is now dead, is the best lead in the file.
Hiding it would be the worst thing this feature could do.

    python3 tests/test_memory.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerb import suppress as S                                # noqa: E402
from kerb.campaign import Campaign                            # noqa: E402
from kerb.models import Business, Signal, Verdict             # noqa: E402
from kerb.pipeline import Pipeline                            # noqa: E402
from kerb.scoring import score                                # noqa: E402
from kerb.store import Store                                  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="kerb-mem-"))


def write(name, text):
    p = TMP / name
    p.write_text(text)
    return p


# --------------------------------------------------------- reading the lists

def test_every_shape_of_do_not_contact_list():
    """It is whatever the user's CRM exported. Demanding a format is how a
    safety feature stops being used."""
    cases = {
        "cids.csv":    ("cid,name\n0x1:0x1,A\n0x1:0x2,B\n", 2),
        "placeid.csv": ("place_id,name\n0x1:0x3,A\n", 1),
        "plain.txt":   ("0x1:0x4\n0x1:0x5\n0x1:0x6\n", 3),
        "osm.txt":     ("osm:way/123\nosm:node/456\n", 2),
        "numeric.txt": ("12345678901234\n98765432109876\n", 2),
        "dated.csv":   ("cid,contacted_at\n0x1:0x7,2026-08-01\n", 1),
        "maps.csv":    ("link,name\nhttps://x/maps/place/Q/data=!1s0x1a:0x2b,A\n", 1),
        "recs.jsonl":  ('{"cid":"0x1:0x8"}\n{"cid":"0x1:0x9"}\n', 2),
    }
    for name, (body, want) in cases.items():
        got = S.load_list(write(name, body))
        assert len(got) == want, "%s -> %d (wanted %d)" % (name, len(got), want)
        assert all(k == k.lower() for k in got), "ids must be normalised"
    print("  every list shape         ok")


def test_an_unreadable_list_stops_everything():
    """A list that loads nothing would silently re-surface everyone on it.

    The dangerous near-miss: a plain-text fallback that accepted a header row
    loaded `name` and `phone` as businesses to hide, suppressed nothing real,
    and reported success.
    """
    # A single-column file of prose is the sneaky one: it parses cleanly and
    # loads "ids" that match no business, so the list suppresses nothing and
    # reports success. /etc/passwd did exactly this.
    for name, body in {"nocid.csv": "name,phone\nAcme,123\nBeta,456\n",
                       "empty.csv": "",
                       "crm.csv": "first_name,last_name,email\nA,B,c@d.com\n",
                       "notes.txt": "call the dentist\nand the vet\nbuy milk\n",
                       "names.txt": "Alma Dental\nBright Smile\n",
                       "passwd": "##\n# User Database\n#\n"
                                 "root:*:0:0:System Administrator:/var/root:/bin/sh\n",
                       }.items():
        try:
            got = S.load_list(write(name, body))
        except S.SuppressionError as exc:
            assert "Refusing to continue" in str(exc) or "no suppression" in str(exc)
            continue
        raise AssertionError("%s was accepted as %s" % (name, list(got)[:3]))

    try:
        S.load_list(TMP / "does-not-exist.csv")
    except S.SuppressionError:
        pass
    else:
        raise AssertionError("a missing list did not raise")
    print("  unreadable list refused  ok")


# ------------------------------------------------------------------- hard

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gosom_export.csv"

CAMPAIGN = {
    "sources": [{"id": "csv", "options": {"path": str(FIXTURE)}}],
    "what": {"packs": ["trades/dentist"]},
    "filters": [{"signal": "trade_match", "op": "!=", "value": False}],
    "scoring": {"weights": {"reviews": {"weight": 100, "scale": "log", "cap": 300}}},
}


def run_with(suppression=None, campaign=None):
    pipe = Pipeline(Campaign.from_dict(campaign or CAMPAIGN), suppression=suppression)
    return pipe, list(pipe.run())


def test_hard_suppression_hides_before_anything_is_measured():
    """Applied at discovery, so a business already contacted does not cost a
    single request to re-confirm."""
    base_pipe, base = run_with()
    lst = write("contacted.csv", "cid\n0x1a:0x00a\n0x1a:0x001\n")
    sup = S.build({"lists": [str(lst)]})
    pipe, got = run_with(sup)

    assert len(got) == len(base) - 2, (len(got), len(base))
    assert sup.hidden_hard == 2
    assert pipe.stats.suppressed == 2
    hidden = {"0x1a:0x00a", "0x1a:0x001"}
    assert not ({v.business.cid for v in got} & hidden)
    # It never reached the gate, so it was never judged either way.
    assert base_pipe.stats.qualified > pipe.stats.qualified
    print("  hard suppression         ok")


def test_suppression_expires_so_a_changed_business_can_return():
    """Permanent suppression is wrong for a dated entry: a business that had a
    website when you passed on it may not have one now."""
    old = time.strftime("%Y-%m-%d", time.localtime(time.time() - 400 * 86400))
    recent = time.strftime("%Y-%m-%d", time.localtime(time.time() - 5 * 86400))
    lst = write("dated.csv",
                "cid,contacted_at\n0x1a:0x00a,%s\n0x1a:0x001,%s\n" % (recent, old))

    forever = S.build({"lists": [str(lst)]})
    run_with(forever)
    assert forever.hidden_hard == 2, "without `after`, dates are ignored"

    expiring = S.build({"lists": [str(lst)], "after": "180d"})
    run_with(expiring)
    assert expiring.hidden_hard == 1, \
        "the 400-day-old entry should have expired, got %d" % expiring.hidden_hard
    print("  suppression expires      ok")


def test_inline_cids_are_normalised():
    sup = S.build({"cids": ["0X1A:0X00A"]})
    _, got = run_with(sup)
    assert sup.hidden_hard == 1, "an upper-case cid did not match"
    print("  inline cids              ok")


# ------------------------------------------------------------------- soft

def seed_run(store, rows_csv, campaign):
    run_id = store.create_run(campaign)
    pipe = Pipeline(Campaign.from_dict(campaign))
    verdicts = list(pipe.run())
    store.add_tasks(run_id, "seed", ["one"])
    task = store.claim(run_id, "seed")
    store.complete(task["id"], [v.to_dict() for v in verdicts], run_id=run_id)
    return run_id, verdicts


def test_soft_suppression_shows_only_what_changed():
    """The whole point. Two businesses unchanged since last week are noise; the
    one whose website died is the lead."""
    week1 = write("w1.csv",
                  "cid,title,category,review_count,website\n"
                  "0x9:0x1,Alpha Dental,Dentist,90,\n"
                  "0x9:0x2,Beta Dental,Dentist,80,https://beta.example\n"
                  "0x9:0x3,Gamma Dental,Dentist,70,\n")
    camp1 = {**CAMPAIGN, "sources": [{"id": "csv", "options": {"path": str(week1)}}],
             "filters": [{"signal": "trade_match", "op": "!=", "value": False},
                         {"signal": "web_presence", "op": "in",
                          "value": ["none", "booking_only"]}]}
    store = Store(TMP / "soft.db")
    run_id, first = seed_run(store, week1, camp1)
    assert {v.business.name: v.outcome.value for v in first} == {
        "Alpha Dental": "qualified", "Beta Dental": "rejected",
        "Gamma Dental": "qualified"}

    # Beta's website has since died. Nothing else moved.
    week2 = write("w2.csv",
                  "cid,title,category,review_count,website\n"
                  "0x9:0x1,Alpha Dental,Dentist,90,\n"
                  "0x9:0x2,Beta Dental,Dentist,80,\n"
                  "0x9:0x3,Gamma Dental,Dentist,70,\n")
    camp2 = {**camp1, "sources": [{"id": "csv", "options": {"path": str(week2)}}]}
    sup = S.build({"runs": [run_id]}, store=store)
    pipe, got = run_with(sup, camp2)

    assert [v.business.name for v in got] == ["Beta Dental"], \
        "expected only the changed verdict, got %s" % [v.business.name for v in got]
    assert sup.hidden_soft == 2 and sup.resurfaced == 1
    assert "verdict changed" in sup.summary()
    store.close()
    print("  soft shows only changes  ok")


def test_soft_suppression_needs_the_ledger():
    try:
        S.build({"runs": ["abc"]}, store=None)
    except S.SuppressionError as exc:
        assert "ledger" in str(exc)
    else:
        raise AssertionError("suppress.runs silently did nothing without a store")

    store = Store(TMP / "missing.db")
    try:
        S.build({"runs": ["nosuchrun"]}, store=store)
    except S.SuppressionError as exc:
        assert "nosuchrun" in str(exc)
    else:
        raise AssertionError("an unknown run id was accepted")
    store.close()
    print("  soft needs the ledger    ok")


def test_suppression_is_always_reported():
    """Fewer results with no explanation is the failure this project removes."""
    lst = write("rep.csv", "cid\n0x1a:0x00a\n")
    sup = S.build({"lists": [str(lst)]})
    pipe, _ = run_with(sup)
    assert sup.summary(), "nothing was reported about what was hidden"
    assert pipe.stats.to_dict()["suppressed"] == 1
    assert pipe.stats.to_dict()["suppression"]["hidden_hard"] == 1
    print("  suppression reported     ok")


# --------------------------------------------------------------- run order

def test_place_order():
    where = {"mode": "packs", "packs": ["geo/uk-affluent"]}
    as_listed = Campaign.from_dict({"where": where}).places
    assert as_listed[0] == "Kensington, London"

    alpha = Campaign.from_dict({"where": dict(where, order="alphabetical")}).places
    assert alpha == sorted(as_listed, key=str.lower)

    prio = Campaign.from_dict({"where": dict(where, order="priority",
                                             priority=["Cambridge", "Oxford"])}).places
    assert prio[:2] == ["Cambridge", "Oxford"], prio[:2]
    assert sorted(prio) == sorted(as_listed), "priority lost or duplicated a place"

    # Reproducible: a partial run must be explicable afterwards.
    a = Campaign.from_dict({"where": dict(where, order="random", seed=7)}).places
    b = Campaign.from_dict({"where": dict(where, order="random", seed=7)}).places
    assert a == b and sorted(a) == sorted(as_listed)

    try:
        Campaign.from_dict({"where": dict(where, order="sideways")}).places
    except ValueError as exc:
        assert "order" in str(exc)
    else:
        raise AssertionError("an unknown order was silently ignored")
    print("  place ordering           ok")


def test_exclude_and_dedupe_apply_to_every_mode():
    c = Campaign.from_dict({"where": {"mode": "packs", "packs": ["geo/uk-affluent"],
                                      "exclude": ["Bath", "oxford"]}})
    assert "Bath" not in c.places and "Oxford" not in c.places

    dupes = Campaign.from_dict({"where": {"mode": "paste",
                                          "places": ["Leeds", "leeds", "York"]}})
    assert dupes.places == ["Leeds", "York"], dupes.places
    print("  exclude and dedupe       ok")


def test_place_packs_work_without_a_mode():
    """`where: {packs: [...]}` -- the form CUSTOMIZATION.md shows -- validated
    clean and then searched zero places, because places were chosen by `mode`
    and validation looked at the keys."""
    from kerb.campaign import validate
    no_mode = Campaign.from_dict({"where": {"packs": ["geo/uk-affluent"]}})
    assert len(no_mode.places) == 20, len(no_mode.places)
    both = Campaign.from_dict({"where": {"places": ["Leeds"],
                                         "packs": ["geo/uk-affluent"],
                                         "place": "York"}})
    assert both.places[0] == "Leeds" and both.places[-1] == "York"
    assert len(both.places) == 22, "places, packs and place are a union"
    assert validate({"sources": [{"id": "overpass"}], "what": {"trade": "x"},
                     "where": {"packs": ["geo/uk-affluent"]}}) == []
    print("  place packs without mode ok")


def test_custom_trade_is_actually_checked():
    """`what.custom.label` was used as the search term and nowhere else, so
    trade_match had no pack, answered "unknown", and `!= false` passed every
    business -- pizza restaurants included."""
    c = Campaign.from_dict({"what": {"custom": {"label": "Bakery"}},
                            "where": {"place": "X"},
                            "filters": [{"signal": "trade_match", "op": "!=",
                                         "value": False}]})
    rows = [Business(cid="0x3:0x1", name="Crumbs", category="Bakery"),
            Business(cid="0x3:0x2", name="Luigi's", category="Pizza restaurant")]
    got = {v.business.name: v.outcome.value for v in Pipeline(c).qualify(rows)}
    assert got == {"Crumbs": "qualified", "Luigi's": "rejected"}, got
    print("  custom trade checked     ok")


def test_pack_osm_tags_reach_the_source():
    """`what.osm_tags` was parsed into the typed pack and never passed on, while
    the overpass error message told users to set exactly that."""
    from unittest import mock
    import httpx
    seen = []

    def request(self, method, url, **kw):
        if "nominatim" in str(url):
            return httpx.Response(200, json=[{"boundingbox": ["1", "2", "3", "4"]}],
                                  request=httpx.Request("GET", "x"))
        seen.append((kw.get("data") or {}).get("data", ""))
        return httpx.Response(200, json={"elements": []},
                              request=httpx.Request("POST", "x"))

    c = Campaign.from_dict({"sources": [{"id": "overpass", "options": {"pause": 0}}],
                            "where": {"places": ["Somewhere"]},
                            "what": {"trade": "bouldering gyms",
                                     "osm_tags": ["sport=climbing"]}})
    with mock.patch.object(httpx.Client, "request", request), \
            mock.patch("time.sleep", lambda *a: None):
        list(Pipeline(c).run())
    assert seen and '["sport"="climbing"]' in seen[0], seen
    assert "bouldering" not in seen[0], "the guessed tags were used instead"
    print("  pack osm_tags used       ok")


def test_rejudging_reuses_paid_measurements():
    """`requalify` promised no re-collection, then recomputed every signal --
    refetching every website it had already checked. FREE signals are
    recomputed (that is where a changed rule lands); paid ones are reused."""
    from kerb.fetch import Response
    from kerb.pipeline import stored_signals

    class Counting:
        n = 0

        def get(self, url):
            Counting.n += 1
            return Response(url=url, final_url=url, status=404, text="gone",
                            requests=1)

    data = write("rejudge.csv", "cid,title,category,review_count,website\n"
                                "0xd:0x1,Dead Site Dental,Dentist,90,https://d.example/\n"
                                "0xd:0x2,Tiny Dental,Dentist,4,https://t.example/\n")
    base = {"sources": [{"id": "csv", "options": {"path": str(data)}}],
            "what": {"packs": ["trades/dentist"]},
            "filters": [{"signal": "site_status", "op": "==", "value": "dead"}],
            "signal_options": {"site_status": {"fetcher": Counting()}}}
    first = [v.to_dict() for v in Pipeline(Campaign.from_dict(base)).run()]
    fetched = Counting.n
    assert fetched == 2

    tighter = {**base, "filters": base["filters"] +
               [{"signal": "reviews", "op": ">=", "value": 30}]}
    businesses = [Business(**{k: v for k, v in r.items()
                              if k in Business.__dataclass_fields__}) for r in first]
    again = {v.business.name: v.outcome.value for v in
             Pipeline(Campaign.from_dict(tighter), reuse=stored_signals(first))
             .qualify(businesses)}
    assert Counting.n == fetched, "re-judging fetched %d more pages" % (Counting.n - fetched)
    assert again == {"Dead Site Dental": "qualified", "Tiny Dental": "rejected"}, again
    print("  re-judging reuses paid   ok")


# ------------------------------------------------------- confidence weighting

def test_confidence_weighting_is_opt_in_and_real():
    def verdict(conf):
        v = Verdict(business=Business(cid="0x1:0x1", name="X"))
        v.add(Signal("web_presence", "none", conf, {}))
        return v

    weights = {"web_presence": {"none": 100}}
    assert score(verdict(1.0), weights, True).score == 100.0
    assert score(verdict(0.6), weights, True).score == 100.0, \
        "confidence weighting must be off by default"

    assert score(verdict(1.0), weights, True, confidence=True).score == 100.0
    assert score(verdict(0.6), weights, True, confidence=True).score == 60.0
    print("  confidence weighting     ok")


if __name__ == "__main__":
    print("memory — what kerb remembers between runs\n")
    for fn in (test_every_shape_of_do_not_contact_list,
               test_an_unreadable_list_stops_everything,
               test_hard_suppression_hides_before_anything_is_measured,
               test_suppression_expires_so_a_changed_business_can_return,
               test_inline_cids_are_normalised,
               test_soft_suppression_shows_only_what_changed,
               test_soft_suppression_needs_the_ledger,
               test_suppression_is_always_reported,
               test_place_order,
               test_exclude_and_dedupe_apply_to_every_mode,
               test_place_packs_work_without_a_mode,
               test_custom_trade_is_actually_checked,
               test_pack_osm_tags_reach_the_source,
               test_rejudging_reuses_paid_measurements,
               test_confidence_weighting_is_opt_in_and_real):
        fn()
    print("\nall memory checks passed")
