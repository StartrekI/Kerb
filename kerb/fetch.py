"""A polite HTTP fetcher — the part that actually goes out and looks.

Everything above this file decides *whether* to fetch. This decides *how*, and
it is the only place in the project that talks to a stranger's server. That
concentration is the point: the rules for being a good citizen are written once
here rather than remembered in every signal.

What it does, and why each one is not optional:

  robots.txt      checked before the first request to a host, cached per host.
                  A tool that ignores robots is not a tool anyone can run at
                  work.
  per-host rate   one limiter per host, not one globally. Hosts are independent
                  -- a global limiter makes 200 sites take 200 seconds for no
                  reason, and a per-worker one hammers a single host with N
                  workers at once.
  caching         on disk, revalidated with ETag/If-Modified-Since. A business
                  website does not change between two runs an hour apart, and
                  re-downloading it is rude as well as slow.
  size cap        streamed and abandoned past a limit. No signal needs 50MB,
                  and one pathological URL should not exhaust memory.
  redirect cap    followed to a limit, and the FINAL url is kept -- a domain
                  that now redirects to a registrar parking page is one of the
                  most useful things you can learn about a business.
  never raises    every failure comes back as a Response with `error` set.
                  A signal that crashes because a server hung up is a signal
                  that takes the run down with it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from .collect import RateLimiter

UA = ("kerb/0.1 (+https://github.com/kerb-tool/kerb) "
      "local-business research; contact via repository")

DEFAULT_PER_HOST = 0.5          # requests per second, per host
MAX_BYTES = 2 * 1024 * 1024     # plenty for any page we read markers out of
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 20.0
CACHE_TTL = 7 * 24 * 3600


@dataclass
class Response:
    """What came back, or why nothing did. Never an exception."""
    url: str
    final_url: str = ""
    status: int = 0
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None
    from_cache: bool = False
    elapsed: float = 0.0
    redirected: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def host(self) -> str:
        return urlsplit(self.final_url or self.url).netloc.lower()

    def to_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "final_url": self.final_url, "status": self.status,
                "error": self.error, "redirected": self.redirected,
                "from_cache": self.from_cache, "bytes": len(self.text)}


_SCHEME_RE = __import__("re").compile(r"^([a-z][a-z0-9+.-]*):", __import__("re").I)


def normalise(url: str) -> str:
    """A usable absolute http(s) URL, or "" if it is not one."""
    raw = (url or "").strip()
    if not raw:
        return ""
    # Check for a scheme BEFORE assuming there isn't one. `mailto:a@b.com` has
    # no "://", so prepending https:// turned it into `https://mailto:a@b.com`
    # -- a syntactically valid URL that we would then have gone and requested.
    m = _SCHEME_RE.match(raw)
    if m:
        if m.group(1).lower() not in ("http", "https"):
            return ""
    else:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                       parts.query, ""))


class Fetcher:
    """Thread-safe. Share one instance across workers so the limits are shared."""

    def __init__(self, cache_dir: Optional[Path] = None,
                 per_host: float = DEFAULT_PER_HOST,
                 respect_robots: bool = True,
                 user_agent: str = UA,
                 max_bytes: int = MAX_BYTES,
                 cache_ttl: float = CACHE_TTL):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.per_host = per_host
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.cache_ttl = cache_ttl
        self._limiters: Dict[str, RateLimiter] = {}
        self._robots: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "cached": 0, "blocked": 0, "errors": 0}
        self._client = httpx.Client(
            follow_redirects=False,             # followed by hand, to count them
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            headers={"User-Agent": user_agent,
                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:                       # noqa: BLE001
            pass

    # -- politeness -------------------------------------------------------

    def _limiter(self, host: str) -> RateLimiter:
        with self._lock:
            if host not in self._limiters:
                self._limiters[host] = RateLimiter(per_second=self.per_host)
            return self._limiters[host]

    def allowed(self, url: str) -> bool:
        """robots.txt, fetched once per host and cached.

        An unreachable robots.txt is treated as permission (that is what the
        standard says for 4xx) but a server error is treated as refusal: if the
        host is having trouble, adding crawl traffic is the wrong response.
        """
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        host = parts.netloc.lower()
        with self._lock:
            cached = self._robots.get(host, "missing")
        if cached == "missing":
            rp = urllib.robotparser.RobotFileParser()
            robots_url = "%s://%s/robots.txt" % (parts.scheme, host)
            try:
                self._limiter(host).acquire()
                r = self._client.get(robots_url)
                if r.status_code >= 500:
                    rp = None                    # refuse while the host is unwell
                elif r.status_code >= 400:
                    rp.parse([])                 # no robots.txt = allowed
                else:
                    rp.parse(r.text.splitlines())
            except Exception:                    # noqa: BLE001
                rp.parse([])                     # unreachable: do not block work
            with self._lock:
                self._robots[host] = rp
            cached = rp

        if cached is None:
            return False
        try:
            ok = cached.can_fetch(self.user_agent, url)
        except Exception:                        # noqa: BLE001
            return True
        if not ok:
            self.stats["blocked"] += 1
        # Honour Crawl-delay if the host asked for one.
        try:
            delay = cached.crawl_delay(self.user_agent)
        except Exception:                        # noqa: BLE001
            delay = None
        if delay:
            lim = self._limiter(host)
            if lim.interval < float(delay):
                lim.interval = lim.base_interval = float(delay)
        return ok

    # -- cache ------------------------------------------------------------

    def _cache_path(self, url: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def _cached(self, url: str) -> Optional[Dict[str, Any]]:
        path = self._cache_path(url)
        if not path or not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _store(self, url: str, data: Dict[str, Any]) -> None:
        path = self._cache_path(url)
        if not path:
            return
        try:
            # Written beside then renamed: a cache entry is either the whole
            # thing or absent, never a half-written file a later run trusts.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(path)
        except OSError:
            pass                                 # a cache that cannot write is fine

    # -- the fetch --------------------------------------------------------

    def get(self, url: str, force: bool = False) -> Response:
        started = time.time()
        target = normalise(url)
        if not target:
            return Response(url=url, error="not an http(s) url")

        entry = None if force else self._cached(target)
        if entry and (time.time() - entry.get("at", 0)) < self.cache_ttl:
            self.stats["cached"] += 1
            return Response(url=target, final_url=entry.get("final_url", target),
                            status=entry.get("status", 0), text=entry.get("text", ""),
                            headers=entry.get("headers", {}), from_cache=True,
                            redirected=entry.get("redirected", False))

        if not self.allowed(target):
            return Response(url=target, error="disallowed by robots.txt")

        seen = set()
        current = target
        redirected = False
        try:
            for _ in range(MAX_REDIRECTS + 1):
                if current in seen:
                    return Response(url=target, final_url=current,
                                    error="redirect loop")
                seen.add(current)
                host = urlsplit(current).netloc.lower()
                self._limiter(host).acquire()
                self.stats["requests"] += 1

                headers = {}
                if entry and entry.get("final_url") == current:
                    if entry.get("etag"):
                        headers["If-None-Match"] = entry["etag"]
                    if entry.get("last_modified"):
                        headers["If-Modified-Since"] = entry["last_modified"]

                with self._client.stream("GET", current, headers=headers) as r:
                    if r.status_code == 304 and entry:
                        self.stats["cached"] += 1
                        entry["at"] = time.time()
                        self._store(target, entry)
                        return Response(url=target, final_url=current,
                                        status=entry.get("status", 200),
                                        text=entry.get("text", ""),
                                        headers=dict(r.headers), from_cache=True,
                                        redirected=redirected,
                                        elapsed=time.time() - started)

                    if r.status_code in (301, 302, 303, 307, 308):
                        nxt = r.headers.get("location")
                        if not nxt:
                            return Response(url=target, final_url=current,
                                            status=r.status_code,
                                            error="redirect without a location")
                        current = str(httpx.URL(current).join(nxt))
                        redirected = True
                        continue

                    body, truncated = b"", False
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) >= self.max_bytes:
                            truncated = True
                            break               # a page we only read markers from
                    if truncated:
                        # Bound the result as well as the loop. A server that
                        # sends one enormous chunk hands it over whole, so
                        # stopping early is not by itself a memory guarantee.
                        body = body[:self.max_bytes]
                    text = body.decode(r.encoding or "utf-8", errors="replace")

                    resp = Response(url=target, final_url=current,
                                    status=r.status_code, text=text,
                                    headers=dict(r.headers), redirected=redirected,
                                    truncated=truncated,
                                    elapsed=time.time() - started)
                    if resp.ok:
                        self._store(target, {
                            "at": time.time(), "final_url": current,
                            "status": r.status_code, "text": text,
                            "headers": dict(r.headers), "redirected": redirected,
                            "etag": r.headers.get("etag"),
                            "last_modified": r.headers.get("last-modified")})
                    return resp

            return Response(url=target, final_url=current, redirected=True,
                            error="too many redirects (>%d)" % MAX_REDIRECTS)

        except Exception as exc:                 # noqa: BLE001
            # Every network failure ends here rather than in a signal. A server
            # that hangs up mid-body must cost this one URL and nothing else.
            self.stats["errors"] += 1
            return Response(url=target, final_url=current,
                            error="%s: %s" % (type(exc).__name__, exc),
                            elapsed=time.time() - started)


_SHARED: Optional[Fetcher] = None
_SHARED_LOCK = threading.Lock()


def shared(**kw) -> Fetcher:
    """One fetcher for the process, so rate limits and cache are actually shared."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            from .store import default_dir
            kw.setdefault("cache_dir", default_dir() / "http-cache")
            _SHARED = Fetcher(**kw)
        return _SHARED
