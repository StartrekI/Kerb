"""The browsing profile Kerb collects with.

`kerb setup` creates one of these; the gmaps collector uses it. It is the same
idea as the Chrome profile the predecessor scraper kept, minus the browser: a
persistent identity -- cookies, a user agent, a locale -- that makes a sequence
of requests look like one person rather than a thousand strangers.

WHY NOT A REAL BROWSER PROFILE
------------------------------
The predecessor drove Chrome through a driver and kept a real profile
directory. That is where its worst failure came from: every crashed run left a
chromedriver and its Chrome children behind, and enough of them filled a disk.
Nothing here launches a process, so nothing here can leak one.

WHAT IS AND IS NOT STORED
-------------------------
Cookies Google sets during setup, a user agent, and a locale. No password is
ever asked for, typed, or stored -- Kerb reads public business listings, which
needs no account. The file is written 0600 because a cookie jar is a credential
even when a password is not.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

# A current desktop Chrome. Kept in one place so it can be changed once when it
# ages out, rather than hunted through the collector.
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

DEFAULT_LOCALE = {"hl": "en", "gl": "us"}

# Google's EU consent interstitial. Accepting it is what a person does by
# clicking the button; this records the same choice so the collector receives
# listings instead of a consent page. It carries no identity.
CONSENT_COOKIE = "SOCS"
CONSENT_VALUE = "CAESHAgBEhIaAB"


def profile_path() -> Path:
    override = os.environ.get("KERB_PROFILE")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("KERB_HOME", "~/.kerb")).expanduser() / "session.json"


class Profile:
    """A saved browsing identity. Absent is a valid state, not an error."""

    def __init__(self, data: Optional[Dict[str, Any]] = None,
                 path: Optional[Path] = None):
        d = dict(data or {})
        self.path = path or profile_path()
        self.user_agent: str = d.get("user_agent") or DEFAULT_UA
        self.locale: Dict[str, str] = dict(DEFAULT_LOCALE, **(d.get("locale") or {}))
        self.cookies: Dict[str, str] = dict(d.get("cookies") or {})
        self.created: float = float(d.get("created") or 0.0)
        self.checked: float = float(d.get("checked") or 0.0)
        # When Google last blocked this address, and until when Kerb should stay
        # off. Persisted deliberately: a block survives the process, so a memory
        # that dies with the run would let the next one walk straight back into
        # it and deepen the penalty.
        self.blocked_at: float = float(d.get("blocked_at") or 0.0)
        self.cooldown_until: float = float(d.get("cooldown_until") or 0.0)
        self.blocks: int = int(d.get("blocks") or 0)
        # Whether the last check saw a SIGNED-IN Google. Google serves
        # signed-out clients a reduced view of Maps, so this is the difference
        # between a full listing and a partial one -- and the user has to be
        # told which one they are getting.
        self.signed_in: bool = bool(d.get("signed_in") or False)

    # -- persistence ------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Profile":
        p = path or profile_path()
        try:
            return cls(json.loads(p.read_text()), p)
        except (OSError, ValueError):
            # A corrupt or missing profile behaves like no profile. Refusing to
            # run because a cache file went bad would be the wrong trade.
            return cls({}, p)

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"user_agent": self.user_agent, "locale": self.locale,
                "cookies": self.cookies, "created": self.created or time.time(),
                "checked": self.checked, "blocked_at": self.blocked_at,
                "cooldown_until": self.cooldown_until, "blocks": self.blocks,
                "signed_in": self.signed_in}
        # A temp name UNIQUE to this writer. Every thread previously wrote to
        # the same `.tmp` and then renamed it, so with parallel workers the
        # first rename moved the file out from under the second and it died
        # with FileNotFoundError while recording a block -- the one moment the
        # profile most needs to be written.
        tmp = self.path.with_suffix(".tmp.%d.%d" % (os.getpid(), threading.get_ident()))
        try:
            tmp.write_text(json.dumps(body, indent=2))
            os.replace(tmp, self.path)          # atomic: never a half-written jar
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        try:
            os.chmod(self.path, 0o600)          # a cookie jar is a credential
        except OSError:
            pass
        return self.path

    @property
    def exists(self) -> bool:
        return bool(self.created)

    def headers(self) -> Dict[str, str]:
        return {"User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Language": "%s;q=0.9" % self.locale.get("hl", "en")}

    # -- cooling ----------------------------------------------------------

    def cooling(self) -> float:
        """Seconds still to wait before it is polite to collect again."""
        return max(0.0, self.cooldown_until - time.time())

    def record_block(self, base: float = 900.0, cap: float = 6 * 3600.0) -> float:
        """Note a block and back off, harder each time.

        Doubling per block within a day is the whole point: hitting a rate limit
        and then retrying at the same pace is how a temporary block becomes a
        long one. The counter decays after a quiet day so one bad afternoon does
        not punish next week.
        """
        now = time.time()
        if self.blocked_at and (now - self.blocked_at) > 86400:
            self.blocks = 0                    # a quiet day forgives
        self.blocks += 1
        wait = min(base * (2 ** (self.blocks - 1)), cap)
        self.blocked_at = now
        self.cooldown_until = now + wait
        self.save()
        return wait

    def record_success(self) -> None:
        """A clean run clears the penalty; nothing is owed after it works."""
        if self.cooldown_until or self.blocks:
            self.cooldown_until = 0.0
            self.blocks = 0
            self.save()

    def describe(self) -> str:
        if not self.exists:
            return "no profile yet -- run `kerb setup`"
        age = time.time() - self.created
        base = ("%d cookie(s), locale %s/%s, created %s ago"
                % (len(self.cookies), self.locale.get("hl"), self.locale.get("gl"),
                   _ago(age)))
        base += ("\n         signed in to Google: %s"
                 % ("yes" if self.signed_in
                    else "NO -- Google serves a reduced view of Maps"))
        cool = self.cooling()
        if cool:
            base += ("\n         COOLING DOWN for another %s after %d block(s)"
                     % (_ago(cool), self.blocks))
        return base


def _ago(sec: float) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= n:
            return "%d%s" % (sec // n, unit)
    return "%ds" % sec


def parse_cookies(text: str) -> Dict[str, str]:
    """Cookies out of whatever a browser gave you.

    Three formats, because people export from three different places and being
    told "wrong format" is a terrible first experience:

      * Netscape cookies.txt   -- what browser extensions export
      * a JSON array           -- what devtools and EditThisCookie export
      * a raw Cookie: header   -- what you get from Copy as cURL

    Values are never logged or printed anywhere; only names are ever shown.
    """
    text = (text or "").strip()
    if not text:
        return {}
    out: Dict[str, str] = {}

    if text.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SetupError("that file is not valid JSON: %s" % exc)
        items = data if isinstance(data, list) else [data]
        for c in items:
            if isinstance(c, dict) and c.get("name"):
                out[str(c["name"])] = str(c.get("value", ""))
        if out:
            return out

    if "\t" in text or text.lstrip().startswith("# Netscape"):
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            bits = line.split("\t")
            if len(bits) >= 7:
                out[bits[5].strip()] = bits[6].strip()
        if out:
            return out

    # A raw header: "SID=abc; HSID=def; SSID=ghi"
    for part in text.replace("Cookie:", "", 1).split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            k = k.strip()
            if k:
                out[k] = v.strip()
    if not out:
        raise SetupError(
            "no cookies found in that file. Expected a Netscape cookies.txt, a "
            "JSON export, or a line like `SID=...; HSID=...`.")
    return out


# Cookies Google sets only for a signed-in session. Presence is a strong hint;
# the live check below is what actually decides.
SIGNED_IN_HINTS = ("SID", "SSID", "HSID", "APISID", "SAPISID", "__Secure-1PSID")


class SetupError(RuntimeError):
    """Setup could not produce a working profile, and says which part failed."""


def looks_signed_in(body: str) -> bool:
    """Did Google answer as if somebody is signed in?

    Mirrors the check the predecessor scraper made in the browser. A signed-out
    client is served a reduced Maps -- their code called it the "limited view"
    -- with no reviews. Detecting it is the difference between a partial run
    and a partial run nobody was told about.
    """
    head = body[:200000]
    if 'aria-label="Google Account' in head or '"gaia_' in head:
        return True
    # A prominent Sign in affordance means we are not.
    return not (">Sign in<" in head or 'aria-label="Sign in"' in head)


def setup(hl: str = "en", gl: str = "us", timeout: float = 25.0,
          path: Optional[Path] = None,
          cookies: Optional[Dict[str, str]] = None) -> Profile:
    """Create a profile and prove it can actually reach listings.

    Proving it is the point. A setup that writes a file and declares success
    leaves the first real run to discover the problem, forty places in.
    """
    prof = Profile({}, path)
    prof.locale = {"hl": hl, "gl": gl}
    prof.created = time.time()
    # Record the consent choice before the first request, so an EU IP is not
    # handed an interstitial instead of results.
    prof.cookies[CONSENT_COOKIE] = CONSENT_VALUE
    if cookies:
        prof.cookies.update(cookies)

    with httpx.Client(follow_redirects=True, timeout=timeout,
                      headers=prof.headers(), cookies=prof.cookies) as client:
        try:
            r = client.get("https://www.google.com/", params={"hl": hl, "gl": gl})
        except httpx.HTTPError as exc:
            raise SetupError("could not reach google.com: %s. Check the network "
                             "and any proxy, then run `kerb setup` again." % exc)
        for k, v in client.cookies.items():
            prof.cookies[k] = v
        if _is_consent(str(r.url), r.text):
            raise SetupError(
                "Google answered with its consent page and would not set a "
                "session. This usually clears by running `kerb setup` again "
                "from the same network, or by choosing a different --gl region.")

        prof.signed_in = looks_signed_in(r.text)

    prof.checked = time.time()
    prof.save()
    return prof


def _is_consent(url: str, body: str) -> bool:
    return ("consent.google" in url or "/sorry/" in url
            or "CONSENT" in body[:4000] and "consent.google" in body[:4000])


def check(prof: Optional[Profile] = None, timeout: float = 25.0) -> Dict[str, Any]:
    """Is this profile still usable? Returns a report; never raises."""
    prof = prof or Profile.load()
    out: Dict[str, Any] = {"exists": prof.exists, "profile": str(prof.path),
                           "detail": prof.describe(), "ok": False, "problem": None}
    out["cooling"] = prof.cooling()
    if not prof.exists:
        out["problem"] = "no profile -- run `kerb setup`"
        return out
    if prof.cooling():
        out["problem"] = ("cooling down for another %s after %d block(s)"
                          % (_ago(prof.cooling()), prof.blocks))
        return out
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout,
                          headers=prof.headers(), cookies=prof.cookies) as client:
            r = client.get("https://www.google.com/",
                           params={"hl": prof.locale.get("hl", "en")})
        if _is_consent(str(r.url), r.text):
            out["problem"] = "Google is serving a consent or block page"
        elif r.status_code != 200:
            out["problem"] = "google.com answered %s" % r.status_code
        else:
            out["ok"] = True
            prof.signed_in = looks_signed_in(r.text)
            prof.save()
        out["signed_in"] = prof.signed_in
    except httpx.HTTPError as exc:
        out["problem"] = "could not reach google.com: %s" % exc
    return out
