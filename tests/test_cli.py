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
    # chromedriver carries no tag of its own; it is found through the browser
    # registry -- see test_registered_browsers_and_their_children_are_found.
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


def test_the_review_browser_is_launched_with_the_tag():
    """The patterns above were tested against command lines Kerb never
    produced: the review browser launched with Chrome's anonymous temp profile,
    so `kerb stop` could not see the one process this project must never leak."""
    import shutil
    from kerb.session import Profile
    from kerb.signals.detail import _profile_dir, chrome_args
    saved = os.environ.pop("KERB_CHROME_PROFILE", None)
    try:
        udd, owned = _profile_dir()
        try:
            assert owned and "kerb-worker" in os.path.basename(udd), udd
            cmd = "/opt/google/chrome/chrome " + " ".join(chrome_args(Profile({}), udd))
            assert matches(cmd), "kerb stop cannot see the browser it launches"
        finally:
            shutil.rmtree(udd, ignore_errors=True)
    finally:
        if saved is not None:
            os.environ["KERB_CHROME_PROFILE"] = saved
    print("  review browser tagged     ok")


def test_registered_browsers_and_their_children_are_found():
    """chromedriver has no tag, and a user-chosen profile has none either, so
    launches are recorded by pid -- with the whole tree beneath them, since a
    renderer's command line does not reliably say "chrome". A recorded pid
    that no longer looks like a browser (pids are reused) is never touched."""
    import subprocess
    import tempfile
    import time
    from kerb import cli, procs
    saved = os.environ.get("KERB_STATE_DIR")
    os.environ["KERB_STATE_DIR"] = tempfile.mkdtemp(prefix="kerb-procs-")
    parent = stranger = None
    try:
        child_code = "import time; time.sleep(60)"
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys, time; "
             "subprocess.Popen([sys.executable, '-c', %r, 'renderer']); "
             "time.sleep(60)" % child_code, "chromedriver-standin"])
        stranger = subprocess.Popen([sys.executable, "-c", child_code, "not-a-browser"])
        time.sleep(0.8)                     # let the child start
        procs.register([parent.pid, stranger.pid], None)

        found = {p["pid"] for p in cli.find_managed()}
        rows = cli._ps()
        child = [r["pid"] for r in rows if r["ppid"] == parent.pid]
        assert parent.pid in found, "a registered browser was not found"
        assert child and set(child) <= found, "its child was left behind"
        assert stranger.pid not in found, "a reused pid would have been killed"

        for pid in [parent.pid] + child:
            os.kill(pid, 9)
        parent.wait(timeout=5)
        time.sleep(0.3)
        cli._forget_dead_browsers()
        assert parent.pid not in procs.registered_pids(), "a dead entry was kept"
    finally:
        for p in (parent, stranger):
            if p is not None and p.poll() is None:
                p.kill()
        if saved is None:
            os.environ.pop("KERB_STATE_DIR", None)
        else:
            os.environ["KERB_STATE_DIR"] = saved
    print("  registered browsers found ok")


if __name__ == "__main__":
    print("cli — process discovery safety\n")
    for fn in (test_finds_everything_it_started, test_never_touches_anything_else,
               test_case_insensitivity_is_the_point, test_never_returns_its_own_ancestors,
               test_kerberos_is_not_kerb,
               test_the_review_browser_is_launched_with_the_tag,
               test_registered_browsers_and_their_children_are_found):
        fn()
    print("\nall cli checks passed")
