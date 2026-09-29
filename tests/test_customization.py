"""Customisation that has to actually work.

Two of these were config that looked supported and silently was not, which is
worse than config that does not exist: `what.packs` accepted a list and used
only the first, and the whole `output:` block was parsed and discarded.

    python3 tests/test_customization.py
"""

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerb import signals                                      # noqa: E402
from kerb.campaign import Campaign                            # noqa: E402
from kerb.cli import (render_template, shape_output, split_key,   # noqa: E402
                      template_fields, write_results)
from kerb.campaign import check_output                        # noqa: E402
from kerb.scoring import band_for, check_bands                # noqa: E402
from kerb.models import Business                              # noqa: E402
from kerb.pipeline import Pipeline                            # noqa: E402
from kerb.signals import Context                              # noqa: E402
from kerb.signals.shape import chain_key, match_brand         # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="kerb-custom-"))


def biz(**kw):
    kw.setdefault("cid", "0x1:0x1")
    kw.setdefault("name", "Test")
    return Business(**kw)


def val(name, business, **opts):
    return signals.compute(name, business, Context(options=opts)).value


# ------------------------------------------------------------- multi-trade

def test_every_listed_trade_pack_is_used():
    """`packs[0]` silently dropped the rest, so a campaign asking for three
    trades rejected two of them with a confident 'not the trade'."""
    c = Campaign.from_dict({"what": {"packs": ["trades/dentist", "trades/medical",
                                               "trades/vet"]},
                            "where": {"place": "X"}})
    assert c.trades == ["dentist", "medical", "vet"]
    assert c.trade == "dentist", "single-trade sources still need one"
    ctx = Context(options=c.signal_options)

    for name, category, want in [
            ("Village Vets", "Veterinary care", "vet"),
            ("Smile Co", "Dentist", "dentist"),
            ("City Clinic", "Medical clinic", "medical"),
            ("Joe Pizza", "Pizza restaurant", False)]:
        got = signals.compute("trade_match", biz(name=name, category=category), ctx)
        assert got.value == want, "%s -> %r (wanted %r)" % (name, got.value, want)
    print("  multi-trade packs        ok")


def test_a_veto_still_wins_across_packs():
    """A dental laboratory is not rescued by also being checked against the
    medical pack."""
    c = Campaign.from_dict({"what": {"packs": ["trades/dentist", "trades/medical"]},
                            "where": {"place": "X"}})
    ctx = Context(options=c.signal_options)
    got = signals.compute("trade_match",
                          biz(name="Kensington Dental Lab",
                              category="Dental laboratory"), ctx)
    assert got.value is False, got.value
    assert got.evidence.get("reason") == "vetoed category"
    print("  veto wins across packs   ok")


def test_single_pack_campaigns_are_unchanged():
    c = Campaign.from_dict({"what": {"packs": ["trades/dentist"]},
                            "where": {"place": "X"}})
    ctx = Context(options=c.signal_options)
    assert signals.compute("trade_match", biz(category="Dentist"), ctx).value == "dentist"
    assert signals.compute("trade_match", biz(category="Cafe"), ctx).value is False
    print("  single pack unchanged    ok")


# ----------------------------------------------------------- free signals

def test_chain_key_merges_branches_without_merging_rivals():
    same = ["Specsavers Opticians - Islington", "Specsavers Opticians (Camden)",
            "Specsavers Opticians Ltd", "specsavers opticians"]
    keys = {chain_key(n) for n in same}
    assert len(keys) == 1, "branches of one brand did not merge: %s" % keys

    # Over-merging is the dangerous direction: it deletes real leads.
    assert chain_key("Village Dental") != chain_key("Village Vets")
    assert chain_key("Bright Smile Dental") != chain_key("Bright Eyes Optical")
    print("  chain key conservative   ok")


def test_chain_size_counts_the_dataset_and_knows_brands():
    """Two independent answers, and the pack wins when it has one.

    The pack knows how big a brand actually is; the tally only knows how many
    landed in this file. For "is this an independent?" the brand's real size is
    the better answer, so a pack hit takes precedence over a local count.
    """
    rows = ([biz(cid="0xc:0x%02x" % i, name="Specsavers Opticians - Town %d" % i,
                 category="Optician") for i in range(4)]
            # A regional brand the pack has never heard of: only the tally can
            # see this one, and it is the case the tally exists for.
            + [biz(cid="0xf:0x%02x" % i, name="Northside Dental Group - Branch %d" % i,
                   category="Dentist") for i in range(3)]
            + [biz(cid="0xd:0x01", name="Alma Dental Practice", category="Dentist"),
               biz(cid="0xe:0x01", name="Toni & Guy (Soho)", category="Hairdresser")])
    c = Campaign.from_dict({"what": {"packs": ["trades/dentist"]},
                            "where": {"place": "X"}, "filters": []})
    by_name = {v.business.name: v.signals["chain_size"]
               for v in Pipeline(c).qualify(rows)}

    known = by_name["Specsavers Opticians - Town 0"]
    assert known.value > 4 and known.evidence["source"] == "chains pack", \
        "the pack knows the brand's real size; the local count is not it"

    tallied = by_name["Northside Dental Group - Branch 0"]
    assert tallied.value == 3, tallied.value
    assert "source" not in tallied.evidence, "an unknown brand came from the pack"

    assert by_name["Alma Dental Practice"].value == 1
    # One branch in the area looks independent to a tally; the pack sees it.
    toni = by_name["Toni & Guy (Soho)"]
    assert toni.value > 1 and toni.evidence.get("source") == "chains pack"
    print("  chain_size               ok")


def test_the_chains_pack_works_without_a_dataset_tally():
    """Recognising a brand from one row is the pack's entire purpose. It used
    to be reachable only when a tally had been seeded -- which is precisely the
    case the tally already answers -- so on the streaming path a national chain
    with one branch in the search area came back as an independent."""
    import tempfile as _t
    from kerb import sources                                   # noqa: F401
    f = TMP / "stream.csv"
    f.write_text("cid,title,category,review_count\n"
                 "0x1:0x1,Toni & Guy (Soho),Hairdresser,90\n"
                 "0x1:0x2,Specsavers Opticians Ltd,Optician,90\n"
                 "0x1:0x3,Alma Dental Practice,Dentist,90\n")
    c = Campaign.from_dict({"sources": [{"id": "csv", "options": {"path": str(f)}}],
                            "what": {"packs": ["trades/dentist"]}, "filters": []})
    got = {v.business.name: v.signals["chain_size"] for v in Pipeline(c).run()}
    assert got["Toni & Guy (Soho)"].value > 1, "streaming path missed a known chain"
    assert got["Specsavers Opticians Ltd"].value > 1, \
        "the pack entry is the brand; listings append the category"
    assert got["Alma Dental Practice"].value == 1
    print("  chains pack streaming    ok")


def test_brand_matching_never_swallows_an_independent():
    """A false positive deletes a real lead, so matching is on whole words."""
    known = {chain_key(k): v for k, v in
             {"Specsavers": 900, "Boots Opticians": 550,
              "Toni & Guy": 250, "PureGym": 400}.items()}
    for name in ("Specsavers Opticians Ltd", "Specsavers Hearing Centre",
                 "Toni & Guy (Soho)", "Boots Opticians - Camden", "PureGym Islington"):
        assert match_brand(chain_key(name), known), name
    for name in ("Bootsy's Diner", "Specsaver Solutions Ltd",
                 "Pure Skin Clinic", "Guy's Barbers"):
        assert match_brand(chain_key(name), known) is None, \
            "%s was swallowed by a brand prefix" % name
    # The longest brand wins, not the first.
    assert match_brand(chain_key("Boots Opticians - Camden"), known) == "boots opticians"
    print("  brand match whole words  ok")


def test_match_mode_is_honoured_and_validated():
    """`what.match` was parsed and ignored -- the same silent-config bug."""
    def verdict(mode, category):
        c = Campaign.from_dict({"what": {"packs": ["trades/dentist", "trades/medical"],
                                         "match": mode}, "where": {"place": "X"}})
        return signals.compute("trade_match", biz(category=category),
                               Context(options=c.signal_options))

    assert verdict("any", "Dentist").value == "dentist"
    assert verdict("all", "Dentist").value is False, "match: all was ignored"
    bad = verdict("sometimes", "Dentist")
    assert bad.value == "unknown" and "must be" in bad.evidence.get("error", "")
    print("  what.match honoured      ok")


def test_name_script_answers_the_english_only_question():
    cases = {"Bright Smile Dental": "latin", "Zahnarzt Müller": "latin",
             "Стоматология Люкс": "cyrillic", "牙科诊所": "han",
             "दंत चिकित्सक": "devanagari"}
    for name, want in cases.items():
        assert val("name_script", biz(name=name)) == want, name

    mixed = signals.compute("name_script", biz(name="Café Größe 東京"), Context())
    assert mixed.value == "latin" and mixed.evidence["mixed"] is True
    assert mixed.confidence < 1.0, "a mixed-script name claimed full confidence"

    blank = signals.compute("name_script", biz(name="+++ ???"), Context())
    assert blank.value == "unknown" and blank.confidence == 0.0
    print("  name_script              ok")


def test_review_velocity_separates_sleepy_from_growing():
    sleepy = signals.compute("review_velocity",
                             biz(review_count=200, first_review_year=2010), Context())
    growing = signals.compute("review_velocity",
                              biz(review_count=200, first_review_year=2024), Context())
    assert growing.value > sleepy.value * 3, (sleepy.value, growing.value)
    assert "caveat" in growing.evidence, "a proxy shipped without its caveat"

    unknown = signals.compute("review_velocity", biz(review_count=200), Context())
    assert unknown.value is None and unknown.confidence == 0.0
    print("  review_velocity          ok")


def test_contactable():
    assert val("contactable", biz(phone="+44123")) is True
    assert val("contactable", biz(website="https://x.com")) is True
    assert val("contactable", biz(booking_url="https://vagaro.com/x")) is True
    assert val("contactable", biz()) is False
    print("  contactable              ok")


def test_rating_band_weighs_volume_not_just_stars():
    """A 5.0 from two reviews and a 4.6 from four hundred are not the same
    claim, so volume sets the confidence -- and below a handful of reviews the
    band is withheld rather than asserted from noise."""
    def band(rating, reviews, **opts):
        return signals.compute("rating_band",
                               biz(rating=rating, review_count=reviews),
                               Context(options={"rating_band": opts} if opts else {}))

    assert band(4.9, 400).value == "excellent"
    assert band(4.2, 80).value == "good"
    assert band(3.4, 60).value == "mixed"
    assert band(2.1, 45).value == "poor"

    assert band(5.0, 2).value == "unrated", "two reviews is not a reputation"
    assert band(None, 90).value == "unrated"

    # Confidence rises with volume.
    assert band(4.5, 400).confidence > band(4.5, 8).confidence

    # A source on another scale is refused, not silently marked excellent.
    off = band(8.6, 90)
    assert off.value == "unknown" and "scale" in off.evidence.get("error", "")
    assert band(8.6, 90, scale=10,
                bands=[("excellent", 9), ("good", 7), ("poor", 0)]).value == "good"
    print("  rating_band              ok")


def test_score_bands_are_order_independent():
    """A list a user reordered while editing must not re-label the dataset."""
    bands = [{"min": 0, "label": "backlog"},
             {"min": 85, "label": "call today"},
             {"min": 70, "label": "worth a look"}]
    assert band_for(92, bands) == "call today"
    assert band_for(74, bands) == "worth a look"
    assert band_for(12, bands) == "backlog"
    assert band_for(None, bands) is None
    assert band_for(50, []) is None

    reversed_order = list(reversed(bands))
    assert all(band_for(v, bands) == band_for(v, reversed_order)
               for v in (0, 12, 70, 84, 85, 100))
    print("  score bands              ok")


def test_bad_bands_are_refused():
    assert check_bands(None) == []
    assert check_bands([{"min": 80, "label": "hot"}]) == []
    assert check_bands("hot") , "a string was accepted as bands"
    assert check_bands([{"label": "no min"}])
    assert check_bands([{"min": "eighty", "label": "x"}])
    assert check_bands([{"min": 80}])
    print("  bad bands refused        ok")


def test_templates_build_mail_merge_columns():
    row = {"name": "Alma Dental", "phone": "+4420", "score": 91.2, "band": "call today",
           "review_count": 142, "website": None,
           "signals": {"rating_band": {"value": "excellent"}}}
    template = {"Company": "name", "Phone": "phone", "Priority": "band",
                "Rating": "rating_band",
                "Opening": "{name} has {review_count} reviews",
                "Site": "website"}
    got = render_template(row, {"name": "Alma Dental", "phone": "+4420",
                                "band": "call today", "review_count": 142,
                                "website": None}, template)
    assert got["Company"] == "Alma Dental"
    assert got["Priority"] == "call today"
    assert got["Rating"] == "excellent", "a signal was not reachable from a template"
    assert got["Opening"] == "Alma Dental has 142 reviews"
    assert got["Site"] == "", "a missing value must render empty, never 'None'"
    assert list(got) == list(template), "template order defines column order"
    print("  templates                ok")


def test_template_fields_are_checked_before_the_run():
    """A typo that blanks a column is bad in a spreadsheet and unforgivable in
    an email that went to a customer."""
    assert sorted(template_fields({"A": "name", "B": "{score} and {band}"})) == \
        ["band", "name", "score"]

    problems = check_output({"template": {"X": "{reveiw_count}"}})
    assert problems and "reveiw_count" in problems[0]
    assert check_output({"columns": ["name", "nosuchthing"]})
    assert check_output({"split_by": "nope"})
    assert check_output({"min_score": "high"})
    assert check_output({"columns": ["name"], "template": {"A": "name"}}), \
        "columns and template together is ambiguous and must be refused"
    assert check_output({"template": {"A": "name", "B": "{review_count}"},
                         "min_score": 70}) == []
    print("  output validated         ok")


def test_free_signals_cost_nothing():
    """If any of these ever start fetching, the tier promise is broken."""
    from kerb.models import Cost
    for name in ("chain_size", "name_script", "review_velocity", "contactable",
                 "rating_band"):
        assert signals.get(name).cost is Cost.FREE, name
    print("  still free               ok")


# --------------------------------------------------------------- output block

ROWS = [
    {"name": "A", "score": 91.0, "category": "Dentist", "phone": "1",
     "signals": {"chain_size": {"value": 1}, "name_script": {"value": "latin"}}},
    {"name": "B", "score": 74.0, "category": "Dentist", "phone": "2", "signals": {}},
    {"name": "C", "score": 55.0, "category": "Orthodontist", "phone": "3", "signals": {}},
    {"name": "D", "score": 88.0, "category": "Orthodontist", "phone": "4", "signals": {}},
]


def test_output_block_is_not_ignored():
    assert shape_output(ROWS, {}) == ROWS, "no block must change nothing"
    assert [r["name"] for r in shape_output(ROWS, {"min_score": 70})] == ["A", "D", "B"]
    assert [r["name"] for r in shape_output(ROWS, {"top": 2})] == ["A", "D"]
    assert [r["name"] for r in shape_output(ROWS, {"min_score": 70, "top": 2})] == ["A", "D"]
    print("  min_score and top        ok")


def test_columns_can_name_any_signal():
    """`columns: [chain_size]` used to produce a column of blanks -- the worst
    of both, since nothing said the column was unsupported."""
    target = TMP / "cols.csv"
    write_results(target, ROWS[:1], "csv",
                  {"columns": ["name", "score", "chain_size", "name_script"]})
    got = list(csv.DictReader(target.open()))[0]
    assert list(got) == ["name", "score", "chain_size", "name_script"]
    assert got["chain_size"] == "1" and got["name_script"] == "latin"
    print("  signal columns           ok")


def test_split_by_writes_one_file_per_group():
    target = TMP / "split.csv"
    written = write_results(target, ROWS, "csv",
                            {"split_by": "category", "columns": ["name", "category"]})
    assert len(written) == 2, written
    names = sorted(p.name for p in written)
    assert names == ["split-dentist.csv", "split-orthodontist.csv"], names
    for path in written:
        rows = list(csv.DictReader(path.open()))
        assert rows and len({r["category"] for r in rows}) == 1
    print("  split_by                 ok")


def test_split_key_is_filename_safe():
    assert split_key({"place": "Islington, London"}, "place") == "islington-london"
    assert split_key({"place": "São Paulo / Zona Sul"}, "place") == "são-paulo-zona-sul"
    assert split_key({}, "place") == "unsorted"
    assert split_key({"place": "///"}, "place") == "unsorted"
    print("  split key safe           ok")


def test_results_are_best_first_with_or_without_the_rejected():
    """With --include-rejected the file came out in discovery order, qualified
    and rejected interleaved, so adding the flag reordered the leads."""
    from kerb.cli import best_first
    rows = [{"name": "rej", "qualified": False, "score": None},
            {"name": "low", "qualified": True, "score": 40.0},
            {"name": "unev", "qualified": False, "score": None},
            {"name": "zero", "qualified": True, "score": 0.0},
            {"name": "top", "qualified": True, "score": 92.5}]
    assert [r["name"] for r in best_first(rows)] == ["top", "low", "zero"]
    assert [r["name"] for r in best_first(rows, include_rejected=True)] == \
        ["top", "low", "zero", "rej", "unev"], "qualified first, then as found"
    print("  best first, rejected last ok")


if __name__ == "__main__":
    print("customisation — config that has to actually do something\n")
    for fn in (test_every_listed_trade_pack_is_used,
               test_a_veto_still_wins_across_packs,
               test_single_pack_campaigns_are_unchanged,
               test_chain_key_merges_branches_without_merging_rivals,
               test_chain_size_counts_the_dataset_and_knows_brands,
               test_the_chains_pack_works_without_a_dataset_tally,
               test_brand_matching_never_swallows_an_independent,
               test_match_mode_is_honoured_and_validated,
               test_rating_band_weighs_volume_not_just_stars,
               test_score_bands_are_order_independent,
               test_bad_bands_are_refused,
               test_templates_build_mail_merge_columns,
               test_template_fields_are_checked_before_the_run,
               test_name_script_answers_the_english_only_question,
               test_review_velocity_separates_sleepy_from_growing,
               test_contactable,
               test_free_signals_cost_nothing,
               test_output_block_is_not_ignored,
               test_columns_can_name_any_signal,
               test_split_by_writes_one_file_per_group,
               test_split_key_is_filename_safe,
               test_results_are_best_first_with_or_without_the_rejected):
        fn()
    print("\nall customisation checks passed")
