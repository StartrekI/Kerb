"""The README and the examples, held to the code.

Documentation drifts silently: a renamed key, a signal value added, a command
nobody listed. The README once told people to `pip install` a package name that
belongs to an unrelated project, and showed a campaign that filtered on a count
its own source never collects. These checks make that kind of drift a test
failure instead of a user's afternoon.

    python3 tests/test_docs.py
"""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kerb import signals                                     # noqa: E402
from kerb.campaign import Campaign, validate                 # noqa: E402
from kerb.cli import build_parser                            # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")


def _yaml_blocks(text):
    for block in re.findall(r"```yaml\n(.*?)```", text, flags=re.S):
        yield block, yaml.safe_load(block)


def test_every_example_campaign_is_valid():
    examples = sorted((ROOT / "examples").glob("*.yaml"))
    assert examples, "no example campaigns found"
    for path in examples:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert validate(raw) == [], (path.name, validate(raw))
        Campaign.from_dict(raw)
    print("  examples validate          ok")


def test_every_campaign_in_the_readme_is_valid():
    """Every YAML block that is a campaign, or a piece of one, must validate as
    written -- a snippet that `kerb run` would refuse teaches the wrong thing."""
    checked = 0
    for block, data in _yaml_blocks(README):
        if not isinstance(data, dict) or "id" in data and "sources" not in data:
            continue                                  # a pack, not a campaign
        campaign = dict(data)
        # A fragment such as `what: {trade: ...}` is shown on its own; give it
        # the rest of a minimal campaign so it is judged on its own keys.
        if "sources" not in campaign:
            campaign["sources"] = [{"id": "csv", "options": {"path": "x.csv"}}]
        if "what" not in campaign:
            campaign["what"] = {"packs": ["trades/dentist"]}
        needs_places = any(isinstance(s, dict) and s.get("id") in ("gmaps", "overpass")
                           for s in campaign["sources"])
        if needs_places and not (campaign.get("where") or {}).get("places"):
            campaign["where"] = {"places": ["Somewhere"]}
        problems = validate(campaign)
        assert problems == [], (block.strip().splitlines()[0], problems)
        checked += 1
    assert checked >= 4, "expected several campaign snippets, found %d" % checked
    print("  README campaigns validate  ok  (%d blocks)" % checked)


def test_the_signal_table_matches_the_registry():
    """Every signal is listed once, with its real tier and values."""
    rows = {}
    for line in README.splitlines():
        m = re.match(r"\| `([a-z_]+)` \| \**(free|cheap|expensive)\** \|(.*)\|\s*$", line)
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3))
    # Built-ins only: other test modules register throwaway signals in the
    # same process, and those are not the product.
    registered = {r.name: r for r in signals.all_signals() if signals.is_builtin(r.name)}
    assert set(rows) == set(registered), (
        "README lists %s; registry has %s" % (sorted(rows), sorted(registered)))
    for name, (tier, rest) in rows.items():
        reg = registered[name]
        assert tier == reg.cost.value, (name, tier, reg.cost.value)
        for value in reg.values or []:
            assert "`%s`" % value in rest, "%s: value %r missing from README" % (name, value)
    print("  signal table matches       ok  (%d signals)" % len(rows))


def test_every_command_is_documented():
    parser = build_parser()
    commands = next(a for a in parser._actions
                    if a.__class__.__name__ == "_SubParsersAction").choices
    table = README[README.index("## Command reference"):]
    for name in commands:
        assert re.search(r"`kerb %s\b" % re.escape(name), table), \
            "`kerb %s` is missing from the command reference" % name
    print("  command reference complete ok  (%d commands)" % len(commands))


def test_the_install_line_names_the_right_package():
    """`pip install kerb` fetches an unrelated project from PyPI."""
    for line in README.splitlines():
        if line.strip().startswith("pip install") and "kerb" in line and "-e" not in line:
            assert "git+https://github.com/StartrekI/Kerb" in line, line
    print("  install line is ours       ok")


if __name__ == "__main__":
    print("docs — the README and examples, held to the code\n")
    for fn in (test_every_example_campaign_is_valid,
               test_every_campaign_in_the_readme_is_valid,
               test_the_signal_table_matches_the_registry,
               test_every_command_is_documented,
               test_the_install_line_names_the_right_package):
        fn()
    print("\nall docs checks passed")
