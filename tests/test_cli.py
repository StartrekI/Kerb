"""Process discovery — the part of the CLI that can do real damage.

`kerb stop` sends SIGTERM and then SIGKILL. Two ways it can be wrong, and both
have been real:

  too narrow   it misses a process it started, reports "clean", and leaves an
               orphan holding memory and a port. This is the failure that once
               left enough browsers running to consume 88GB.
  too wide     it kills something it did not start.

The matcher is therefore tested directly on command-line strings, without
spawning anything.

    python3 tests/test_cli.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerb.cli import EXE_OK, STRONG_PATTERNS, WEAK_PATTERNS, find_managed  # noqa: E402


def matches(cmd: str) -> bool:
    """Same decision find_managed() makes, minus the ps call and ancestry."""
    if any(p.search(cmd) for p in STRONG_PATTERNS):
        return True
    exe = os.path.basename(cmd.split()[0]) if cmd.split() else ""
    return bool(EXE_OK.match(exe)) and any(p.search(cmd) for p in WEAK_PATTERNS)


MANAGED = [
    # macOS ships the framework interpreter as `Python`, capital P. A
    # case-sensitive matcher missed this entirely: `kerb stop` said "nothing to
    # stop" while the server it had started was still holding the port.
    "/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/"
    "Contents/MacOS/Python -m kerb serve --port 8000",
    "/usr/bin/python3 -m kerb serve --port 8000",
    "/usr/bin/python3 -m kerb run campaign.yaml",
    "/usr/bin/python3 -m uvicorn kerb.api:app --port 8000",
    "/usr/local/bin/uvicorn kerb.api:app",
    "/usr/local/bin/kerb serve --port 8000",
    "/usr/local/bin/kerb run campaign.yaml",
    # Browsers kerb launched are identified by the profile dir it gave them,
    # never by the browser's name -- killing every Chrome would take the
    # user's own windows with it.
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
    "--user-data-dir=/tmp/kerb-worker-1234 --headless",
    "/usr/bin/chromedriver --kerb --port=9515",
]

UNTOUCHABLE = [
    # Each of these CONTAINS a pattern but is not the thing.
    'grep -rn "kerb.api:app" .',
    "rg --files-with-matches 'python -m kerb serve' /Users/sam/code",
    "vim /Users/sam/kerb/kerb/api.py",
    "tail -f /var/log/kerb.api.log",
    "less kerb.api:app.notes",
    "/bin/zsh -c echo python3 -m kerb serve",
    # A user's own browser, with their own profile.
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
    "--user-data-dir=/Users/sam/Library/Application Support/Google/Chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    # Unrelated software that merely shares a prefix.
    "/usr/bin/python3 -m kerberos_helper --daemon",
    "/usr/bin/python3 -m pytest tests/",
    "kerbal-space-program",
]


def test_finds_everything_it_started():
    missed = [c for c in MANAGED if not matches(c)]
    assert not missed, "kerb stop would leave these orphaned:\n  " + "\n  ".join(missed)
    print("  finds all managed procs   ok  (%d)" % len(MANAGED))


def test_never_touches_anything_else():
    hit = [c for c in UNTOUCHABLE if matches(c)]
    assert not hit, "kerb stop would kill these:\n  " + "\n  ".join(hit)
    print("  spares everything else    ok  (%d)" % len(UNTOUCHABLE))


def test_case_insensitivity_is_the_point():
    """The exact regression: capital-P Python."""
    assert matches("/Frameworks/Python.app/Contents/MacOS/Python -m kerb serve")
    assert matches("/usr/bin/PYTHON3 -M KERB SERVE")
    print("  case-insensitive          ok")


def test_never_returns_its_own_ancestors():
    """`kerb stop` typed into a shell must not kill that shell -- and the
    shell's command line can easily contain the pattern being searched for."""
    pids = {p["pid"] for p in find_managed()}
    assert os.getpid() not in pids
    assert os.getppid() not in pids
    print("  ancestors protected       ok")


def test_kerberos_is_not_kerb():
    assert not matches("/usr/bin/python3 -m kerberos_helper serve")
    assert matches("/usr/bin/python3 -m kerb serve")
    print("  word boundaries hold      ok")


if __name__ == "__main__":
    print("cli — process discovery safety\n")
    for fn in (test_finds_everything_it_started, test_never_touches_anything_else,
               test_case_insensitivity_is_the_point, test_never_returns_its_own_ancestors,
               test_kerberos_is_not_kerb):
        fn()
    print("\nall cli checks passed")
