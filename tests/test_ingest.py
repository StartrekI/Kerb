"""Ingest and campaign validation.

Every failure here is silent by nature: the file reads, the run completes, the
numbers look plausible, and businesses have quietly gone missing. That is the
worst class of bug this project can ship, because the output gives the user no
reason to doubt it.

    python3 tests/test_ingest.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerb import sources                                     # noqa: E402
from kerb.campaign import parse_duration, validate           # noqa: E402
from kerb.models import SourceQuery                          # noqa: E402
from kerb.sources.csv_ingest import (                        # noqa: E402
    _num, build_mapping, detect_profile, extract_cid, inspect)

TMP = Path(tempfile.mkdtemp(prefix="kerb-ingest-"))


def write(name, text):
    p = TMP / name
    p.write_text(text)
    return p


def rows(path, **opts):
    return list(sources.fetch("csv", SourceQuery(path=str(path), **opts)))


# ------------------------------------------------------------ number parsing

def test_float_formatted_integers_survive():
    """`int("142.0")` raises, so review_count silently became None.

    gosom writes `142`; Apify and Outscraper commonly write `142.0`. The
    business then failed every `reviews >= N` filter and was thrown away over
    a formatting difference between two exporters.
    """
    assert _num("142", int) == 142
    assert _num("142.0", int) == 142
    assert _num("1,234.0", int) == 1234
    assert _num("4.9") == 4.9
    assert _num("", int) is None
    assert _num(None, int) is None
    assert _num("N/A", int) is None, "genuine junk must still be rejected"

    p = write("floats.csv", "cid,title,reviews,rating\n0x1:0x1,A,12.0,4.5\n")
    b = rows(p)[0]
    assert b.review_count == 12, "float-formatted count must survive ingest"
    assert b.rating == 4.5
    print("  float-formatted numbers  ok")


# ------------------------------------------------------------- file shapes

def test_every_json_shape_reads():
    """A wrapped document is not JSONL, and reported zero rows for a good file."""
    arr = write("a.json", json.dumps([{"cid": "0x1:0x1", "title": "A"}]))
    assert len(rows(arr)) == 1, "JSON array"

    jsonl = write("b.jsonl", '{"cid":"0x1:0x2","title":"B"}\n{"cid":"0x1:0x3","title":"C"}\n')
    assert len(rows(jsonl)) == 2, "JSONL"

    for key in ("results", "data", "items"):
        w = write("w-%s.json" % key,
                  json.dumps({key: [{"cid": "0x1:0x4", "title": "D"},
                                    {"cid": "0x1:0x5", "title": "E"}]}))
        assert len(rows(w)) == 2, "wrapped under %r" % key

    single = write("s.json", json.dumps({"cid": "0x1:0x6", "title": "F"}))
    assert len(rows(single)) == 1, "single-object document"

    csvp = write("c.csv", "cid,title\n0x1:0x7,G\n")
    assert len(rows(csvp)) == 1, "CSV"

    assert rows(write("empty.csv", "")) == []
    print("  all file shapes read      ok")


def test_spreadsheet_encodings():
    """Excel writes UTF-8 *with a BOM*, and "Unicode Text" is UTF-16.

    Decoded as plain utf-8, the BOM stayed glued to the first header: `cid`
    became `\\ufeffcid`, matched no alias, and every row in the file was
    dropped as having no identity. A total, silent failure on a good file.
    """
    bom = TMP / "bom.csv"
    bom.write_bytes(b"\xef\xbb\xbfcid,title,review_count\n0x1:0x1,Excel Export,55\n")
    got = rows(bom)
    assert len(got) == 1, "BOM'd file lost every row"
    assert got[0].name == "Excel Export" and got[0].review_count == 55

    u16 = TMP / "u16.csv"
    u16.write_bytes("cid,title\n0x1:0x2,Unicode Text Export\n".encode("utf-16"))
    got16 = rows(u16)
    assert len(got16) == 1 and got16[0].name == "Unicode Text Export"

    plain = TMP / "plain.csv"
    plain.write_text("cid,title\n0x1:0x3,No BOM\n")
    assert rows(plain)[0].name == "No BOM"
    print("  spreadsheet encodings     ok")


def test_newlines_inside_quoted_fields():
    """splitlines() removed the newline before csv could see it, so a
    two-line address arrived as '12 High StLondon SW1' -- words glued."""
    p = write("nl.csv", 'cid,title,address\n0x1:0x9,Multi,"12 High St\nLondon SW1"\n')
    got = rows(p)
    assert len(got) == 1, "a quoted newline must not split the row"
    assert got[0].address == "12 High St\nLondon SW1", repr(got[0].address)
    print("  quoted newlines           ok")


def test_one_malformed_row_does_not_lose_the_file():
    """Each of these aborted the entire run on a single bad row.

    All three are ordinary in real scraper exports: an unescaped comma makes a
    ragged row, mishandled encodings leave stray NULs, and a pasted description
    exceeds Python's 128KB csv field limit. Losing 199,999 good rows to one bad
    one is the failure this project exists to argue against.
    """
    ragged = TMP / "ragged.csv"
    ragged.write_bytes(b"cid,title,category\n"
                       b"0x1:0x1,A,Dentist,EXTRA,COLUMNS\n"   # too many
                       b"0x1:0x2\n"                            # too few
                       b"0x1:0x3,Recovered,Dentist\n")
    got = rows(ragged)
    assert len(got) == 3, "ragged rows lost the file: %d" % len(got)
    assert got[-1].name == "Recovered", "rows after the bad one were lost"

    nul = TMP / "nul.csv"
    nul.write_bytes(b"cid,title,category\n0x1:0x4,A\x00B,Dentist\n"
                    b"0x1:0x5,Recovered,Dentist\n")
    got = rows(nul)
    assert len(got) == 2 and got[-1].name == "Recovered"
    assert "\x00" not in got[0].name

    huge = TMP / "huge.csv"
    huge.write_bytes(b"cid,title,category\n0x1:0x6," + b"X" * 2_000_000 +
                     b",Dentist\n0x1:0x7,Recovered,Dentist\n")
    got = rows(huge)
    assert len(got) == 2 and got[-1].name == "Recovered", \
        "a field over the csv limit aborted the file"
    print("  malformed rows survived   ok")


def test_awkward_but_legal_files():
    for name, body in (
            ("crlf.csv", b"cid,title,category\r\n0x2:0x1,A,Dentist\r\n"),
            ("emoji.csv", "cid,title,category\n0x2:0x2,🦷 Zahnarzt Müller ЛЮКС,Dentist\n".encode()),
            ("dupcols.csv", b"cid,title,title,category\n0x2:0x3,A,B,Dentist\n"),
            ("unclosed.csv", b'cid,title,category\n0x2:0x4,"unterminated,Dentist\n')):
        p = TMP / name
        p.write_bytes(body)
        assert rows(p), name
    print("  awkward files read        ok")


def test_identity_is_never_invented():
    m = {"cid": "cid", "maps_url": "link"}
    assert extract_cid({"cid": "0xAB:0xCD"}, m) == "0xab:0xcd"
    assert extract_cid({"link": "https://x/maps/place/Q/data=!1s0x1a:0x2b"}, m) == "0x1a:0x2b"
    assert extract_cid({"link": "https://maps.google.com/?cid=12345"}, m) == "12345"
    for junk in ("None", "nan", "null", ""):
        assert extract_cid({"cid": junk}, m) is None, junk
    assert extract_cid({"name": "X"}, m) is None

    p = write("mixed.csv", "cid,title\n0x1:0x1,keeps\n,dropped\n")
    assert len(rows(p)) == 1, "a row with no derivable id must be dropped"
    assert inspect(str(p))["rows_without_identity"] == 1
    print("  identity never invented   ok")


def test_profiles_and_aliases():
    assert detect_profile(["title", "category", "review_count", "cid"]) == "gosom"
    assert detect_profile(["title", "categoryName", "totalScore", "placeId"]) == "apify"
    assert detect_profile(["name", "full_address", "reviews", "place_id"]) == "outscraper"
    assert detect_profile(["nope", "nada"]) is None
    assert build_mapping(["place_id", "cid"])["cid"] == "cid", "alias priority"

    p = write("extras.csv", "cid,title,weird_column\n0x1:0x1,A,keepme\n")
    assert rows(p)[0].extras.get("weird_column") == "keepme", \
        "unmapped columns must be preserved, not discarded"
    print("  profiles and aliases      ok")


def test_limit_is_honoured():
    p = write("many.csv", "cid,title\n" + "".join("0x1:0x%02x,B%d\n" % (i, i) for i in range(10)))
    assert len(rows(p)) == 10
    assert len(rows(p, limit=3)) == 3
    print("  limit honoured            ok")


# ------------------------------------------------------- campaign validation

def test_empty_sources_still_demands_places():
    """`any([])` is False, so an empty sources list validated clean and then
    found nothing -- while Campaign.from_dict defaults it to overpass, which
    does need places."""
    assert validate({"sources": []}), "empty sources must inherit the overpass default"
    assert validate({}), "no sources key at all is the same case"
    assert validate({"sources": [{"id": "overpass"}]})
    assert validate({"sources": [{"id": "csv"}]}) == [], "file sources need no places"
    # A place satisfies the PLACES rule. It does not satisfy the trade rule:
    # overpass builds its query from the trade and raises without one, so a
    # campaign naming neither must not be handed a clean bill of health.
    said = validate({"sources": [], "where": {"place": "Islington"}})
    assert not any("place" in p.lower() for p in said), said
    assert any("trade" in p.lower() for p in said), said
    assert validate({"sources": [], "where": {"place": "Islington"},
                     "what": {"trade": "hospitals"}}) == []
    print("  empty sources validated   ok")


def test_validate_reports_everything_at_once():
    problems = validate({
        "sources": [{"id": "nosuch"}],
        "what": {"packs": ["trades/nope"]},
        "filters": [{"signal": "notasignal", "op": "==", "value": 1}],
        "scoring": {"weights": {"reviews": {"weight": 5, "cap": True}}},
    })
    assert len(problems) >= 5, "a builder revealing one error per attempt is a bad tool"
    joined = " ".join(problems)
    for expected in ("nosuch", "trades/nope", "notasignal", "cap", "no places"):
        assert expected in joined, expected
    print("  all problems at once      ok")


def test_durations():
    assert parse_duration("30s") == 30
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration(90) == 90
    assert parse_duration("abc") is None
    assert parse_duration(None) is None
    print("  durations                 ok")


if __name__ == "__main__":
    print("ingest — silent-data-loss regressions\n")
    for fn in (test_float_formatted_integers_survive, test_every_json_shape_reads,
               test_spreadsheet_encodings, test_newlines_inside_quoted_fields,
               test_one_malformed_row_does_not_lose_the_file,
               test_awkward_but_legal_files,
               test_identity_is_never_invented, test_profiles_and_aliases,
               test_limit_is_honoured, test_empty_sources_still_demands_places,
               test_validate_reports_everything_at_once, test_durations):
        fn()
    print("\nall ingest checks passed")
