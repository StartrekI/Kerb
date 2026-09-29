"""The one thing a browser is still needed for: review counts.

Google's Maps search endpoint returns a rating but not a review count, and the
place page loads its detail through an XHR whose `x-maps-bgbind` / `x-maps-bgkey`
headers are session-scoped and cannot be synthesised. A browserless client
provably cannot mint them. So this is where -- and only where -- Kerb opens a
browser.

WHY THIS IS AN EXPENSIVE SIGNAL AND NOT A COLLECTOR STAGE
---------------------------------------------------------
The obvious build is a second pass over everything discovery found. Measured on
1,386 businesses that is 1,386 x 1.57s = 36 minutes.

But a signal in the EXPENSIVE tier only runs on businesses that already passed
every free and cheap condition. On the same run that is roughly forty of them,
which is about a minute. Same data, same browser, one-thirty-fifth of the cost,
because the ordering does the work rather than the concurrency.

That is the entire argument for cost tiers, so it would be strange to build the
one genuinely expensive measurement any other way.

REQUIRES AN EXTRA
-----------------
    pip install "kerb[browser]"

Absent, the signal reports itself unmeasurable and says how to fix it. It never
guesses a count: a wrong review count silently changes every score that uses it.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple

from ..models import Business, Cost, Signal
from . import Context, signal

def _selenium():
    """Import selenium only when a browser is actually about to be opened.

    It was imported at module load, and because the signal registry imports
    every signal module, that meant a pure-HTTP run pulled the whole browser
    stack into memory to do nothing with it. The core has two dependencies; it
    should not load a third to collect over HTTP.
    """
    from selenium import webdriver                          # noqa: PLC0415
    from selenium.webdriver.chrome.options import Options   # noqa: PLC0415
    return webdriver, Options


def _have_selenium() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("selenium") is not None
    except Exception:                           # noqa: BLE001
        return False


class _Lazy:
    """`detail.HAVE_SELENIUM` stays a truthy attribute without importing."""
    def __bool__(self):
        return _have_selenium()
    def __repr__(self):
        return repr(_have_selenium())


HAVE_SELENIUM = _Lazy()

INSTALL_HINT = ('needs a browser: pip install "kerb[browser]"')

# "1,234 reviews", "(1,234)", "1.2K reviews" -- Maps renders all three depending
# on locale and surface, so all three are read rather than one guessed. The
# number token may carry a K/M suffix; _number decides what it means.
_NUM = r'(\d[\d,\.   ]*\d|\d)\s*([KkMm](?![a-z]))?'
_PATTERNS = (
    re.compile(_NUM + r'\s*(?:Google\s+)?reviews?\b', re.I),
    re.compile(r'\(\s*' + _NUM + r'\s*\)'),
)

# Thousands grouped with a comma, dot or (narrow) space: 1,234  1.234  1 234.
_GROUPED = re.compile(r'^\d{1,3}(?:[,\.   ]\d{3})+$')


def _number(digits: str, suffix: Optional[str]) -> Optional[int]:
    """A review count out of one token, or None when it is not one.

    Stripping every dot and comma read a rating in brackets, "(4.8)", as 48
    reviews, and never read "1.2K reviews" at all.
    """
    token = digits.strip()
    if suffix:
        try:
            value = float(token.replace(",", "."))
        except ValueError:
            return None
        return int(round(value * (1000 if suffix.lower() == "k" else 1_000_000)))
    if token.isdigit():
        return int(token)
    if _GROUPED.match(token):
        return int(re.sub(r"\D", "", token))
    return None                       # a decimal: a rating, not a count

_LOCK = threading.Lock()
_DRIVER = None                                  # one browser, reused across calls
_PROFILE_DIR: Optional[str] = None              # the throwaway profile, if ours
_ATEXIT = False


def _count(text: str) -> Optional[int]:
    """First plausible review count in a blob of page text."""
    for pat in _PATTERNS:
        for digits, suffix in pat.findall(text or ""):
            n = _number(digits, suffix)
            if n is not None and 0 < n < 10_000_000:
                return n
    return None


def chrome_args(profile, user_data_dir: str) -> List[str]:
    """Every command-line argument the review browser is launched with.

    Separate from _driver so it can be checked without a browser -- in
    particular that the profile directory carries the `kerb-worker` tag
    `kerb stop` looks for.
    """
    return [
        "--user-data-dir=" + user_data_dir,
        "--headless=new",
        # A tall viewport loads more per screen; carried over from the
        # predecessor, which measured it as meaningfully fewer rounds.
        "--window-size=1500,2400",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--lang=en-US",
        "--disable-notifications",
        "--mute-audio",
        # An explicit desktop UA. Without one Chrome sends a HeadlessChrome
        # token and Google serves the reduced view -- rating, no Reviews tab.
        "--user-agent=" + profile.user_agent,
        "--disable-blink-features=AutomationControlled",
    ]


def _profile_dir() -> Tuple[str, bool]:
    """(user-data-dir, whether Kerb owns it and must delete it on close).

    An existing Chrome profile directory, if one is offered: cookies in kerb's
    own jar cover most of it, but a real profile carries the whole signed-in
    session -- which is what the review RPC needs. Otherwise a throwaway one
    named `kerb-worker-*`, which is what lets `kerb stop` recognise this
    browser even after its parent has died. Chrome's own default is an
    anonymous temp directory that no cleanup command can tell from the user's.
    """
    given = os.environ.get("KERB_CHROME_PROFILE")
    if given:
        return os.path.abspath(os.path.expanduser(given)), False
    return tempfile.mkdtemp(prefix="kerb-worker-"), True


def _driver(profile):
    """One headless Chrome, created on first use and shared.

    Reused deliberately. A driver per business is what made the predecessor
    leak: every launch is a Chrome plus its helper children, and a crash
    between launch and quit orphans all of them.
    """
    global _DRIVER, _PROFILE_DIR
    with _LOCK:
        if _DRIVER is not None:
            return _DRIVER
        webdriver, Options = _selenium()
        opts = Options()
        udd, owned = _profile_dir()
        for arg in chrome_args(profile, udd):
            opts.add_argument(arg)
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_experimental_option("prefs", {"intl.accept_languages": "en,en_US"})
        try:
            drv = webdriver.Chrome(options=opts)
        except Exception:
            if owned:
                shutil.rmtree(udd, ignore_errors=True)
            raise
        _PROFILE_DIR = udd if owned else None
        _register(drv, udd)
        # Nothing else closes the browser when a `kerb run` simply finishes:
        # the process exits, and chromedriver and Chrome are left behind as
        # orphans. An exit hook closes it on every normal exit; a kill -9 is
        # what the tag and the registry above are for.
        global _ATEXIT
        if not _ATEXIT:
            atexit.register(close)
            _ATEXIT = True
        drv.set_page_load_timeout(60)
        try:
            drv.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',"
                           "{get:()=>undefined});"})
        except Exception:                       # noqa: BLE001
            pass                                # cosmetic only
        try:
            # Install the review recorder BEFORE the document exists. Patching
            # the live page after load is a race we lose: Maps takes its own
            # references to XMLHttpRequest during startup, so a late patch
            # observes zero fetches and strands the harvest.
            from ..reviews import RECORDER_JS
            drv.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "(function(){%s})();" % RECORDER_JS})
        except Exception:                       # noqa: BLE001
            pass

        # Carry the profile's session in. Cookies must be added on the domain
        # they belong to, so a cheap page is opened first.
        if profile.cookies:
            try:
                drv.get("https://www.google.com/robots.txt")
                for name, value in profile.cookies.items():
                    try:
                        drv.add_cookie({"name": name, "value": value,
                                        "domain": ".google.com"})
                    except Exception:           # noqa: BLE001
                        continue                # one bad cookie is not fatal
            except Exception:                   # noqa: BLE001
                pass
        _DRIVER = drv
        return _DRIVER


def _register(drv, udd: str) -> None:
    """Write the launched chromedriver and its browser tree to the registry,
    so `kerb stop` from any other process can find them. Best effort: a
    registry that cannot be written must not stop the browser working."""
    try:
        from .. import procs
        pid = drv.service.process.pid
        pids = {pid}
        try:
            from ..cli import _ps
            pids |= procs.descendants(_ps(), [pid])
        except Exception:                       # noqa: BLE001
            pass
        procs.register(sorted(pids), udd)
    except Exception:                           # noqa: BLE001
        pass


def in_use() -> bool:
    return _DRIVER is not None


def close() -> None:
    """Shut the browser. Called by `kerb stop` and at the end of a run.

    The predecessor's worst failure was a driver that outlived its run, so
    closing is a named, callable thing rather than a hope. It also deletes the
    throwaway profile and forgets the registry entry, so neither piles up.
    """
    global _DRIVER, _PROFILE_DIR
    with _LOCK:
        if _DRIVER is not None:
            try:
                _DRIVER.quit()
            except Exception:                   # noqa: BLE001
                pass
            _DRIVER = None
            try:
                from .. import procs
                procs.forget()
            except Exception:                   # noqa: BLE001
                pass
        if _PROFILE_DIR:
            shutil.rmtree(_PROFILE_DIR, ignore_errors=True)
            _PROFILE_DIR = None


@signal(name="reviews_live", cost=Cost.EXPENSIVE, version=1,
        label="Review count (fetched)",
        description="Opens the business's own Maps page to read its review "
                    "count. One browser page per business -- so it only runs on "
                    "what already survived every cheaper condition.",
        kind="number", rank={"scale": "log", "cap": 300})
def reviews_live(biz: Business, ctx: Context) -> Signal:
    if not HAVE_SELENIUM:
        return Signal("reviews_live", None, 0.0, {"error": INSTALL_HINT})

    url = (biz.extras or {}).get("maps_url")
    if not url:
        # Only businesses carrying a Maps identity can be looked up. Saying so
        # beats opening a browser to fail.
        return Signal("reviews_live", None, 0.0,
                      {"error": "no maps_url on this business -- only the gmaps "
                                "source supplies one"})

    from ..session import Profile
    try:
        drv = _driver(Profile.load())
        # One page load is at least one request to Google, and the run's
        # request budget has to see it like any other.
        ctx.note_request(1)
        drv.get(url)
        text = drv.find_element("tag name", "body").text
    except Exception as exc:                    # noqa: BLE001
        # A failed fetch is UNMEASURED, never zero. Zero reviews and "we could
        # not look" are different facts and must not collapse into one number.
        return Signal("reviews_live", None, 0.0,
                      {"error": "%s: %s" % (type(exc).__name__, str(exc)[:120])})

    n = _count(text)
    if n is None:
        # Distinguish "no reviews tab" from "count not found". The predecessor
        # called the first one the LIMITED VIEW and it has a specific cause and
        # a specific fix; reporting a vague maybe sends people hunting.
        limited = "Reviews" not in text
        return Signal("reviews_live", None, 0.0,
                      {"error": ("Google served the signed-out view of this "
                                 "place: rating shown, no Reviews tab, no count. "
                                 "Sign the profile in once with `kerb setup "
                                 "--import-cookies FILE`."
                                 if limited else
                                 "a Reviews tab is present but no count could be "
                                 "read from it -- the page layout may have moved"),
                       "limited_view": limited,
                       "tabs_seen": [t for t in ("Overview", "Reviews", "About",
                                                 "Photos") if t in text]})
    return Signal("reviews_live", n, 1.0,
                  {"source": "maps place page", "url": url})
