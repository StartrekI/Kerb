"""The UI's own logic, run for real where it can be.

"Copy as YAML" is how a brief graduates from the UI to a scheduled `kerb run`,
and it produced files `kerb run` refused: `op: >=` is a YAML block scalar and
`op: !=` is a tag. The function is extracted from index.html and executed with
Node when Node is installed, and its output is loaded with the same PyYAML the
CLI uses -- so the test checks the real round trip, not a re-implementation.

    python3 tests/test_ui.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HTML = (ROOT / "kerb" / "static" / "index.html").read_text()

# What the builder produces by default, plus every value shape that can trip a
# YAML writer: operators, reserved words, numbers-as-strings, punctuation,
# empty collections, unicode, and keys that YAML would read as booleans.
CAMPAIGNS = [
    {"name": "dentist",
     "sources": [{"id": "csv", "options": {"path": "tests/fixtures/gosom_export.csv"}}],
     "where": {}, "what": {"packs": ["trades/dentist"]},
     "filters": [{"signal": "liveness", "op": "==", "value": "open"},
                 {"signal": "reviews", "op": ">=", "value": 30},
                 {"signal": "trade_match", "op": "!=", "value": False},
                 {"signal": "chain_size", "op": "<=", "value": 3},
                 {"signal": "web_presence", "op": "in",
                  "value": ["none", "social_only", "booking_only"]},
                 {"signal": "web_presence", "op": "not in", "value": []}],
     "scoring": {"weights": {"liveness": {"open": 40, "temp_closed": 0},
                             "chain_size": {"weight": 40, "scale": "log", "cap": 10,
                                            "invert": True}},
                 "normalise": True},
     "limits": {"max_runtime_seconds": 600, "workers": {"discover": 3, "profile": 3}}},
    {"name": "yes",
     "sources": [{"id": "gmaps"}],
     "where": {"mode": "paste", "places": ["Islington, London", "2020", "No",
                                           "St. Albans #2", "- dash", "São Paulo",
                                           "key: value", "null", "~", "123abc",
                                           "a\nb", "quote\"s", "'single'"]},
     "what": {"trade": "tattoo studios", "keywords": ["tattoo", "ink: art"]},
     "filters": [],
     "scoring": {"weights": {"on": 1, "no": {"yes": 5}, "rating_band": {}}},
     "suppress": {"lists": ["contacted.csv"], "after": "90d"}},
]


def _node():
    return shutil.which("node")


def _extract_to_yaml() -> str:
    start = HTML.index("// @yaml-begin")
    end = HTML.index("// @yaml-end")
    return HTML[start:end]


def test_copy_as_yaml_round_trips_through_pyyaml():
    node = _node()
    if not node:
        print("  (node not installed; round trip skipped)")
        return
    script = _extract_to_yaml() + "\nconst cs = %s;\n" % json.dumps(CAMPAIGNS) + \
        "process.stdout.write(JSON.stringify(cs.map(c => toYaml(c))));\n"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    for original, text in zip(CAMPAIGNS, json.loads(out.stdout)):
        loaded = yaml.safe_load(text)
        assert loaded == original, "round trip changed the brief:\n%s" % text
    print("  copy-as-yaml round trips  ok")


def test_the_default_brief_yaml_is_a_valid_campaign():
    """Not just parseable: the CLI's validator accepts what the UI exports."""
    node = _node()
    if not node:
        print("  (node not installed; skipped)")
        return
    from kerb.campaign import validate
    script = _extract_to_yaml() + "\nprocess.stdout.write(toYaml(%s));\n" \
        % json.dumps(CAMPAIGNS[0])
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=30)
    assert validate(yaml.safe_load(out.stdout)) == [], out.stdout
    print("  exported brief validates  ok")


def test_no_values_are_pasted_into_inline_handlers():
    """Signal names were interpolated into an onclick attribute. Values reach
    handlers through data attributes now."""
    assert "swapCondition('${" not in HTML
    assert "data-swap-off" in HTML
    print("  no interpolated onclick   ok")


def test_ranking_is_read_from_the_registry():
    """The weight cap used to be guessed in the page; it must come from each
    signal's `rank` hint, or a lower-is-better number ranks backwards."""
    assert "s.rank" in HTML and "invert" in HTML
    assert "((s.suggest||{}).value || 30) * 10" not in HTML
    print("  ranking from registry     ok")


if __name__ == "__main__":
    print("ui -- the page's own logic\n")
    for fn in (test_copy_as_yaml_round_trips_through_pyyaml,
               test_the_default_brief_yaml_is_a_valid_campaign,
               test_no_values_are_pasted_into_inline_handlers,
               test_ranking_is_read_from_the_registry):
        fn()
    print("\nall ui checks passed")
