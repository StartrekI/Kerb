"""Where this process may read from.

The guard used to live in the API's campaign validator, covering only the keys
someone had remembered to list there. Source paths were checked from the start;
suppression lists named by the same campaign were not, and
`suppress: {lists: [/etc/passwd]}` was read over HTTP.

Enumerating config keys is the wrong shape for this, because a new key that
names a file gets added by someone thinking about the feature rather than about
the guard. So the check lives where files are opened. **The test that matters
is the last one**: a brand-new, unheard-of config path is refused without
anyone adding it to a list.

    python3 tests/test_paths.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerb import paths, suppress                             # noqa: E402
from kerb.models import SourceQuery                          # noqa: E402
from kerb.sources import csv_ingest                          # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="kerb-paths-"))
INSIDE = TMP / "inside"
INSIDE.mkdir()
OUTSIDE = Path(tempfile.mkdtemp(prefix="kerb-outside-"))


def setup_files():
    (INSIDE / "ok.csv").write_text("cid,title\n0x1:0x1,Allowed\n")
    (OUTSIDE / "secret.csv").write_text("cid,title\n0x9:0x9,Secret\n")
    (OUTSIDE / "ids.txt").write_text("0x9:0x1\n0x9:0x2\n")
    (INSIDE / "list.txt").write_text("0x1:0x1\n0x1:0x2\n")


setup_files()


def test_unrestricted_is_the_default():
    """The CLI is the user's own shell and may read what they can. A tool that
    confines the command line would be unusable and would teach nothing."""
    paths.unrestrict()
    assert paths.confined() is False
    assert paths.check(OUTSIDE / "secret.csv").exists()
    print("  unrestricted by default  ok")


def test_confinement_covers_reads_not_just_validation():
    paths.confine([INSIDE])
    try:
        assert paths.confined() is True
        assert paths.check(INSIDE / "ok.csv")
        for outside in (OUTSIDE / "secret.csv", "/etc/passwd",
                        str(INSIDE) + "/../../../../etc/passwd"):
            try:
                paths.check(outside)
            except paths.PathNotAllowed:
                continue
            raise AssertionError("%s was allowed" % outside)
    finally:
        paths.unrestrict()
    print("  confinement + traversal  ok")


def test_the_ingest_layer_enforces_it():
    """Not the validator -- the reader. Anything that opens a file is covered,
    however the path reached it."""
    paths.confine([INSIDE])
    try:
        rows = list(csv_ingest.read_rows(INSIDE / "ok.csv"))
        assert len(rows) == 1

        for blocked in (OUTSIDE / "secret.csv", Path("/etc/passwd")):
            try:
                list(csv_ingest.read_rows(blocked))
            except paths.PathNotAllowed:
                continue
            raise AssertionError("read_rows opened %s" % blocked)

        # And through the source adapter, which is how a campaign reaches it.
        try:
            list(csv_ingest.csv_source(
                SourceQuery(path=str(OUTSIDE / "secret.csv"))))
        except paths.PathNotAllowed:
            pass
        else:
            raise AssertionError("the csv source read outside the roots")
    finally:
        paths.unrestrict()
    print("  ingest enforces          ok")


def test_suppression_lists_are_enforced_too():
    """The key that was missed. It is covered now because it reads a file, not
    because it was added to a list of keys."""
    paths.confine([INSIDE])
    try:
        assert len(suppress.load_list(INSIDE / "list.txt")) == 2
        for blocked in (OUTSIDE / "ids.txt", Path("/etc/passwd")):
            try:
                suppress.load_list(blocked)
            except suppress.SuppressionError as exc:
                assert "outside" in str(exc), str(exc)[:80]
                continue
            raise AssertionError("loaded a suppression list from %s" % blocked)
    finally:
        paths.unrestrict()
    print("  suppression enforced     ok")


def test_a_brand_new_config_key_is_covered_automatically():
    """The point of the whole refactor.

    This stands in for a campaign key that does not exist yet: some future
    option that hands a path to a reader. Nobody has added it to any allow-list,
    and nobody will remember to. It is refused anyway, because the check is at
    the open and not at the schema.
    """
    paths.confine([INSIDE])
    try:
        pretend_new_option = str(OUTSIDE / "secret.csv")
        try:
            list(csv_ingest.read_rows(Path(pretend_new_option)))
        except paths.PathNotAllowed as exc:
            assert "outside" in str(exc)
        else:
            raise AssertionError(
                "a path from an unknown config key was read; the guard is still "
                "a list of keys someone has to remember to update")
    finally:
        paths.unrestrict()
    print("  unknown key covered      ok")


def test_the_policy_is_restored_for_the_cli():
    """A confined server process must not leave the CLI confined if both run in
    one interpreter (tests do exactly this)."""
    paths.confine([INSIDE])
    paths.unrestrict()
    assert paths.check(OUTSIDE / "secret.csv")
    print("  policy resets            ok")


if __name__ == "__main__":
    print("paths — the guard lives where files are opened\n")
    for fn in (test_unrestricted_is_the_default,
               test_confinement_covers_reads_not_just_validation,
               test_the_ingest_layer_enforces_it,
               test_suppression_lists_are_enforced_too,
               test_a_brand_new_config_key_is_covered_automatically,
               test_the_policy_is_restored_for_the_cli):
        fn()
    print("\nall path checks passed")
