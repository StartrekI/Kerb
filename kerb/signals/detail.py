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

import re
import threading
from typing import Any, Dict, Optional

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
# on locale and surface, so all three are read rather than one guessed.
_PATTERNS = (
    re.compile(r'([\d][\d,\.]*)\s*(?:Google\s+)?reviews?', re.I),
    re.compile(r'\(\s*([\d][\d,\.]*)\s*\)'),
    re.compile(r'"([\d][\d,\.]*)\s*reviews?"', re.I),
)

_LOCK = threading.Lock()
_DRIVER = None                                  # one browser, reused across calls


def _count(text: str) -> Optional[int]:
    """First plausible review count in a blob of page text."""
    for pat in _PATTERNS:
        for raw in pat.findall(text or ""):
            cleaned = raw.replace(",", "").replace(".", "")
            if cleaned.isdigit():
                n = int(cleaned)
                if 0 < n < 10_000_000:
                    return n
    return None


def _driver(profile):
    """One headless Chrome, created on first use and shared.

    Reused deliberately. A driver per business is what made the predecessor
    leak: every launch is a Chrome plus its helper children, and a crash
    between launch and quit orphans all of them.
    """
    global _DRIVER
    with _LOCK:
        if _DRIVER is not None:
            return _DRIVER
        webdriver, Options = _selenium()
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1400,1000")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--lang=%s" % profile.locale.get("hl", "en"))
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        drv = webdriver.Chrome(options=opts)
        drv.set_page_load_timeout(45)

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


def close() -> None:
    """Shut the browser. Called by `kerb stop` and at the end of a run.

    The predecessor's worst failure was a driver that outlived its run, so
    closing is a named, callable thing rather than a hope.
    """
    global _DRIVER
    with _LOCK:
        if _DRIVER is not None:
            try:
                _DRIVER.quit()
            except Exception:                   # noqa: BLE001
                pass
            _DRIVER = None


@signal(name="reviews_live", cost=Cost.EXPENSIVE, version=1,
        label="Review count (fetched)",
        description="Opens the business's own Maps page to read its review "
                    "count. One browser page per business -- so it only runs on "
                    "what already survived every cheaper condition.",
        kind="number")
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
