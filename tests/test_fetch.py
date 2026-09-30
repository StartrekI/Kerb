"""The fetcher, and the signals that use it.

This is the only part of the project that talks to a stranger's server, so it
is the only part that can be rude, hang, or bring a run down. Every test here
is one of those.

    python3 tests/test_fetch.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx                                                  # noqa: E402

from kerb import signals                                      # noqa: E402
from kerb.campaign import Campaign                            # noqa: E402
from kerb.fetch import Fetcher, Response, normalise           # noqa: E402
from kerb.models import Business, Cost                        # noqa: E402
from kerb.pipeline import Pipeline                            # noqa: E402
from kerb.signals import Context                              # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="kerb-fetch-"))



class FakeNet:
    """A pretend internet. Records every request so politeness is measurable."""

    def __init__(self, pages, robots=None):
        self.pages = pages                 # url -> (status, body, headers)
        self.robots = robots or {}
        self.hits = []
        self.lock = threading.Lock()

    def install(self):
        net = self

        class FakeStream:
            def __init__(self, status, body, headers):
                self.status_code = status
                self._body = body.encode() if isinstance(body, str) else body
                self.headers = httpx.Headers(headers or {})
                self.encoding = "utf-8"
                self.text = body if isinstance(body, str) else body.decode()

            def iter_bytes(self):
                yield self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _resolve(url):
            with net.lock:
                net.hits.append(url)
            if url.endswith("/robots.txt"):
                host = httpx.URL(url).host
                if host in net.robots:
                    return FakeStream(200, net.robots[host], {})
                return FakeStream(404, "", {})
            if url in net.pages:
                status, body, headers = net.pages[url]
                if isinstance(body, Exception):
                    raise body
                return FakeStream(status, body, headers)
            return FakeStream(404, "not found", {})

        return mock.patch.multiple(
            httpx.Client,
            stream=lambda self, method, url, **kw: _resolve(str(url)),
            get=lambda self, url, **kw: _resolve(str(url)))


HTML = "<html><body>%s</body></html>"
LONG = HTML % ("Real dental practice in Islington. " * 40)


# --------------------------------------------------------------------- urls

def test_url_normalisation():
    assert normalise("acme.com").startswith("https://acme.com")
    assert normalise("https://acme.com/x?y=1") == "https://acme.com/x?y=1"
    assert normalise("https://acme.com/x#frag") == "https://acme.com/x"
    for junk in ("", "   ", "mailto:a@b.com", "tel:+441234", "javascript:void(0)",
                 "ftp://files.example.com"):
        assert normalise(junk) == "", junk
    print("  url normalisation         ok")


# ----------------------------------------------------------------- politeness

def test_robots_txt_is_obeyed():
    net = FakeNet(
        pages={"https://blocked.example/": (200, LONG, {}),
               "https://open.example/": (200, LONG, {})},
        robots={"blocked.example": "User-agent: *\nDisallow: /"})
    with net.install():
        f = Fetcher(cache_dir=TMP / "c1", per_host=1000)
        blocked = f.get("https://blocked.example/")
        allowed = f.get("https://open.example/")
    assert blocked.error and "robots" in blocked.error
    assert allowed.ok, allowed.error
    assert not any(h == "https://blocked.example/" for h in net.hits), \
        "a disallowed page was fetched anyway"
    print("  robots.txt obeyed         ok")


def test_a_failing_robots_does_not_block_work():
    """No robots.txt means allowed; a 5xx means the host is unwell, so back off."""
    net = FakeNet(pages={"https://nore.example/": (200, LONG, {}),
                         "https://sick.example/": (200, LONG, {})},
                  robots={})
    with net.install():
        f = Fetcher(cache_dir=TMP / "c2", per_host=1000)
        assert f.get("https://nore.example/").ok        # 404 robots -> allowed

    def sick(self, url, **kw):
        class R:
            status_code = 503
            text = ""
        return R()
    with net.install():
        f2 = Fetcher(cache_dir=TMP / "c3", per_host=1000)
        with mock.patch.object(httpx.Client, "get", sick):
            r = f2.get("https://sick.example/")
        assert r.error and "robots" in r.error, "kept crawling a host returning 5xx"
    print("  robots failure handled    ok")


def test_rate_limit_is_per_host_and_shared():
    """One limiter per host: independent hosts must not queue behind each other,
    and one host must not be hit by every worker at once."""
    pages = {"https://a.example/%d" % i: (200, LONG, {}) for i in range(6)}
    pages.update({"https://b.example/%d" % i: (200, LONG, {}) for i in range(6)})
    net = FakeNet(pages=pages)
    with net.install():
        f = Fetcher(cache_dir=None, per_host=10, respect_robots=False)
        t0 = time.monotonic()
        threads = [threading.Thread(target=f.get, args=(u,)) for u in pages]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0
    # 6 per host at 10/s is ~0.5s; both hosts run in parallel, so ~0.5s total.
    assert elapsed >= 0.4, "one host was hit faster than its limit (%.2fs)" % elapsed
    assert elapsed < 1.6, "independent hosts queued behind each other (%.2fs)" % elapsed
    print("  per-host rate limiting    ok  (12 urls, 2 hosts, %.2fs)" % elapsed)


# ---------------------------------------------------------------- resilience

def test_no_network_failure_ever_raises():
    boom = {
        "https://timeout.example/": (0, httpx.ReadTimeout("timed out"), {}),
        "https://reset.example/": (0, httpx.ConnectError("connection refused"), {}),
        "https://proto.example/": (0, httpx.RemoteProtocolError("bad chunk"), {}),
    }
    net = FakeNet(pages=boom)
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False)
        for url in boom:
            r = f.get(url)
            assert isinstance(r, Response), url
            assert r.error and not r.ok, url
    print("  network failures contained ok")


def test_redirect_chains_are_bounded_and_recorded():
    pages = {"https://hop.example/%d" % i:
             (302, "", {"location": "https://hop.example/%d" % (i + 1)})
             for i in range(12)}
    pages["https://loop.example/a"] = (302, "", {"location": "https://loop.example/b"})
    pages["https://loop.example/b"] = (302, "", {"location": "https://loop.example/a"})
    pages["https://ok.example/"] = (302, "", {"location": "https://ok.example/final"})
    pages["https://ok.example/final"] = (200, LONG, {})
    net = FakeNet(pages=pages)
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False)
        assert "redirect" in (f.get("https://hop.example/0").error or "")
        assert "loop" in (f.get("https://loop.example/a").error or "")
        good = f.get("https://ok.example/")
        assert good.ok and good.redirected
        assert good.final_url.endswith("/final"), "the final url was not kept"
    print("  redirects bounded         ok")


def test_a_huge_page_is_truncated_not_swallowed_whole():
    net = FakeNet(pages={"https://big.example/": (200, "x" * 5_000_000, {})})
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False,
                    max_bytes=100_000)
        r = f.get("https://big.example/")
    assert r.ok and r.truncated and len(r.text) <= 200_000
    print("  size cap enforced         ok")


def test_the_cache_stops_a_second_request():
    net = FakeNet(pages={"https://cached.example/": (200, LONG, {"etag": "abc"})})
    with net.install():
        f = Fetcher(cache_dir=TMP / "c4", per_host=1000, respect_robots=False)
        first = f.get("https://cached.example/")
        before = len(net.hits)
        second = f.get("https://cached.example/")
    assert first.ok and second.ok and second.from_cache
    assert len(net.hits) == before, "the cache did not prevent a second request"
    assert second.text == first.text
    print("  cache prevents refetch    ok")


# ------------------------------------------------------------------- signals

# site_contact asks DNS whether an email's domain has a mail server. The suite
# runs offline, so that answer is "unknown" unless a test supplies one.
NO_DNS = {"mail_check": lambda domain: None}


def sig(name, biz, fetcher):
    return signals.compute(name, biz, Context(options={name: {"fetcher": fetcher,
                                                               **NO_DNS}}))


def biz(website=None, **kw):
    kw.setdefault("cid", "0x1:0x1")
    kw.setdefault("name", "Test")
    return Business(website=website, **kw)


def test_site_status_tells_dead_from_never_had_one():
    pages = {
        "https://live.example/": (200, LONG, {}),
        "https://gone.example/": (404, "not found", {}),
        "https://soon.example/": (200, HTML % "Coming soon! Our website is under construction.", {}),
        "https://empty.example/": (200, HTML % "Hello", {}),
        "https://old.example/": (302, "", {"location": "https://sedoparking.com/x"}),
        "https://sedoparking.com/x": (200, HTML % "This domain may be for sale", {}),
    }
    net = FakeNet(pages=pages)
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False)
        cases = {
            "https://live.example/": "live",
            "https://gone.example/": "dead",
            "https://soon.example/": "placeholder",
            "https://empty.example/": "placeholder",
            "https://old.example/": "parked",
        }
        for url, want in cases.items():
            got = sig("site_status", biz(website=url), f)
            assert got.value == want, "%s -> %s (wanted %s)" % (url, got.value, want)
        assert sig("site_status", biz(), f).value == "no_site"
    print("  site_status classifies    ok")


def test_a_fetch_failure_is_a_failed_measurement_not_a_dead_site():
    """Our network being broken is not evidence about their business. It has to
    read as unmeasurable so the breaker sees it and nothing gets rejected."""
    net = FakeNet(pages={"https://x.example/": (0, httpx.ConnectError("down"), {})})
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False)
        s = sig("site_status", biz(website="https://x.example/"), f)
    assert s.value == "unknown" and s.confidence == 0.0
    assert s.failed, "a network error did not register as a failed measurement"
    print("  fetch failure != dead     ok")


def test_platform_is_read_from_html_not_the_url():
    """A custom domain in front of Wix is still Wix -- the URL cannot tell you."""
    pages = {
        "https://theirsalon.com/": (200, HTML % "<script src='https://static.wixstatic.com/x.js'></script>", {}),
        "https://plain.example/": (200, LONG, {}),
        "https://shop.example/": (200, HTML % "<link href='https://cdn.shopify.com/s/x.css'>", {}),
    }
    net = FakeNet(pages=pages)
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False)
        assert sig("site_platform", biz(website="https://theirsalon.com/"), f).value == "wix"
        assert sig("site_platform", biz(website="https://shop.example/"), f).value == "shopify"
        assert sig("site_platform", biz(website="https://plain.example/"), f).value == "custom"
    print("  platform from html        ok")


def test_contact_extraction_skips_toolchain_noise():
    page = HTML % ("Email us at hello@realdentist.co.uk or "
                   "<script>Sentry.init({dsn:'x@sentry.io'})</script> "
                   "<img src='logo@2x.png'>")
    net = FakeNet(pages={"https://c.example/": (200, page, {})})
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False)
        s = sig("site_contact", biz(website="https://c.example/"), f)
    assert s.value == "hello@realdentist.co.uk", s.value
    assert not any("sentry" in e for e in s.evidence["emails"])
    print("  contact extraction        ok")



def test_a_contact_page_is_read_when_the_home_page_has_none():
    """Many sites keep the address on a contact or imprint page. One more
    request -- charged to the budget -- beats reporting no email at all."""
    home = HTML % ('<a href="/about">About</a> <a href="/contact-us/">Contact</a> '
                   '<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a> '
                   '<a href="https://www.facebook.com/brightsmile">fb</a> '
                   '<a href="https://instagram.com/brightsmile/">ig</a> '
                   '<a href="tel:+44 20 7123 4567">call</a> ' + "words " * 50)
    pages = {"https://c.example/": (200, home, {}),
             "https://c.example/contact-us/": (200, HTML % "Write to reception@c.example", {})}
    net = FakeNet(pages=pages)
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False)
        ctx = Context(options={"site_contact": {"fetcher": f,
                                                "mail_check": lambda d: True}})
        s = signals.compute("site_contact", biz(website="https://c.example/"), ctx)
    assert s.value == "reception@c.example", s
    assert s.evidence["contact_page"] == "https://c.example/contact-us/"
    assert s.evidence["socials"] == {"facebook": "https://www.facebook.com/brightsmile",
                                     "instagram": "https://instagram.com/brightsmile/"}
    assert s.evidence["phones"] == ["+442071234567"]
    assert s.evidence["mail_server"] is True and s.confidence == 0.9
    assert net.hits.count("https://c.example/contact-us/") == 1
    assert ctx.requests == 2, ctx.requests            # home + contact page
    print("  contact page followed     ok")


def test_an_address_that_cannot_receive_mail_is_flagged():
    net = FakeNet(pages={"https://m.example/": (200, HTML % "mail hi@deadmail.example", {})})
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False)
        asked = []
        ctx = Context(options={"site_contact": {
            "fetcher": f, "mail_check": lambda d: asked.append(d) or False}})
        s = signals.compute("site_contact", biz(website="https://m.example/"), ctx)
    assert asked == ["deadmail.example"]
    assert s.value == "hi@deadmail.example" and s.confidence == 0.5
    assert "bounce" in s.evidence["note"] and s.evidence["mail_server"] is False
    print("  undeliverable flagged     ok")


def test_https_mobile_and_year_are_read_from_one_fetch():
    modern = ('<html><head><meta name="viewport" content="width=device-width">'
              '<meta name="generator" content="WordPress 5.2.3"></head><body>'
              + "Care. " * 50 + '<footer>© 2016–2019 Bright Smile</footer></body></html>')
    dated = HTML % ("Welcome. " * 50 + "Copyright 2012 Acme Dental Ltd. All rights reserved.")
    pages = {"https://new.example/": (200, modern, {}),
             "http://old.example/": (200, dated, {}),
             "https://bad.example/": (0, httpx.ConnectError(
                 "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), {})}
    net = FakeNet(pages=pages)
    with net.install():
        f = Fetcher(cache_dir=None, per_host=1000, respect_robots=False)
        new = biz(website="https://new.example/")
        assert sig("site_https", new, f).value is True
        assert sig("site_mobile", new, f).value is True
        year = sig("site_year", new, f)
        assert year.value == 2019 and year.evidence["years_seen"] == [2016, 2019], year
        assert sig("site_platform", new, f).evidence["generator"] == "WordPress 5.2.3"
        assert net.hits.count("https://new.example/") == 1, "four signals, one fetch"

        old = biz(website="http://old.example/")
        https = sig("site_https", old, f)
        assert https.value is False and "Not secure" in https.evidence["note"]
        assert sig("site_mobile", old, f).value is False
        assert sig("site_year", old, f).value == 2012

        bad = sig("site_https", biz(website="https://bad.example/"), f)
        assert bad.value is False and bad.confidence == 0.8, bad

        none = sig("site_year", biz(), f)
        assert none.value is None and none.confidence == 1.0
    print("  https/mobile/year         ok")


# --------------------------------------------------------------- the tiering

def test_the_cheap_tier_only_sees_what_survived_free():
    """The whole economic argument, finally measurable: businesses rejected on
    free data must never cost a request."""
    pages = {"https://site%d.example/" % i: (200, LONG, {}) for i in range(20)}
    net = FakeNet(pages=pages)
    fixture = TMP / "tier.csv"
    rows = ["cid,title,category,review_count,website"]
    for i in range(20):
        # Half fail the free review filter and must never be fetched.
        reviews = 100 if i % 2 == 0 else 2
        rows.append("0xt:0x%02x,Dental %d,Dentist,%d,https://site%d.example/"
                    % (i, i, reviews, i))
    fixture.write_text("\n".join(rows))

    with net.install():
        f = Fetcher(cache_dir=None, per_host=10000, respect_robots=False)
        campaign = Campaign.from_dict({
            "sources": [{"id": "csv", "options": {"path": str(fixture)}}],
            "what": {"packs": ["trades/dentist"]},
            "filters": [{"signal": "reviews", "op": ">=", "value": 50},
                        {"signal": "site_status", "op": "==", "value": "live"}],
            "gating": {"free": ["reviews", "trade_match"],
                       "cheap": ["site_status"]},
            "signal_options": {"site_status": {"fetcher": f}}})
        verdicts = list(Pipeline(campaign).run())

    fetched = [h for h in net.hits if "site" in h]
    assert len(verdicts) == 20
    assert len(fetched) == 10, \
        "%d requests for 20 businesses; half should have been rejected free" % len(fetched)
    print("  cheap tier gated by free  ok  (20 businesses, %d requests)" % len(fetched))


def test_website_fetches_count_against_the_budget():
    """`note_request` existed and nothing called it, so a budget of 3 let the
    cheap tier make 20 website fetches while reporting 0 requests."""
    pages = {"https://b%d.example/" % i: (200, LONG, {}) for i in range(20)}
    net = FakeNet(pages=pages)
    fixture = TMP / "budget.csv"
    fixture.write_text("cid,title,category,review_count,website\n" + "".join(
        "0xu:0x%02x,Dental %d,Dentist,90,https://b%d.example/\n" % (i, i, i)
        for i in range(20)))
    with net.install():
        f = Fetcher(cache_dir=None, per_host=10000, respect_robots=False)
        campaign = Campaign.from_dict({
            "sources": [{"id": "csv", "options": {"path": str(fixture)}}],
            "what": {"packs": ["trades/dentist"]},
            "filters": [{"signal": "site_status", "op": "==", "value": "live"}],
            "limits": {"max_requests": 3},
            "signal_options": {"site_status": {"fetcher": f}}})
        pipe = Pipeline(campaign)
        list(pipe.run())
    fetched = [h for h in net.hits if ".example/" in h]
    assert len(fetched) <= 3, "a budget of 3 made %d website fetches" % len(fetched)
    assert pipe.stats.requests == len(fetched), (pipe.stats.requests, len(fetched))
    assert "budget" in (pipe.stats.stopped_reason or "")
    print("  site fetches are budgeted  ok  (%d fetches)" % len(fetched))


def test_three_cheap_signals_share_one_fetch_even_when_the_site_is_dead():
    """The disk cache keeps successes only, so a dead site was fetched once
    per cheap signal: three requests -- and three timeouts -- for one fact."""
    net = FakeNet(pages={"https://dead.example/": (404, "gone", {}),
                         "https://slow.example/": (0, httpx.ConnectTimeout("t"), {})})
    with net.install():
        f = Fetcher(cache_dir=None, per_host=10000, respect_robots=False)
        ctx_opts = {n: {"fetcher": f, **NO_DNS} for n in ("site_status", "site_platform",
                                                          "site_contact")}
        for url in ("https://dead.example/", "https://slow.example/"):
            ctx = Context(options=ctx_opts)
            for n in ("site_status", "site_platform", "site_contact"):
                signals.compute(n, biz(website=url), ctx)
            assert ctx.requests == 1, "%s cost %d requests" % (url, ctx.requests)
    assert net.hits.count("https://dead.example/") == 1, net.hits
    assert net.hits.count("https://slow.example/") == 1, net.hits
    print("  cheap signals share fetch  ok")


def test_robots_redirects_are_followed():
    """Most robots.txt files are reached through http->https or a www hop.
    Unfollowed, the parser read the redirect's empty body and allowed all."""
    def handler(request):
        url = str(request.url)
        if url == "http://r.example/robots.txt":
            return httpx.Response(301, headers={"location": "https://r.example/robots.txt"})
        if url == "https://r.example/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        return httpx.Response(200, text=LONG)

    f = Fetcher(cache_dir=None, per_host=10000)
    f._client = httpx.Client(transport=httpx.MockTransport(handler))
    blocked = f.get("http://r.example/private/page")
    assert blocked.error and "robots" in blocked.error, blocked
    assert blocked.requests == 2, "robots + its redirect hop: %s" % blocked.requests
    assert f.get("http://r.example/public").ok
    print("  robots redirects followed  ok")


if __name__ == "__main__":
    print("fetch — the only part that talks to strangers\n")
    for fn in (test_url_normalisation,
               test_robots_txt_is_obeyed,
               test_a_failing_robots_does_not_block_work,
               test_rate_limit_is_per_host_and_shared,
               test_no_network_failure_ever_raises,
               test_redirect_chains_are_bounded_and_recorded,
               test_a_huge_page_is_truncated_not_swallowed_whole,
               test_the_cache_stops_a_second_request,
               test_site_status_tells_dead_from_never_had_one,
               test_a_fetch_failure_is_a_failed_measurement_not_a_dead_site,
               test_platform_is_read_from_html_not_the_url,
               test_contact_extraction_skips_toolchain_noise,
               test_a_contact_page_is_read_when_the_home_page_has_none,
               test_an_address_that_cannot_receive_mail_is_flagged,
               test_https_mobile_and_year_are_read_from_one_fetch,
               test_the_cheap_tier_only_sees_what_survived_free,
               test_website_fetches_count_against_the_budget,
               test_three_cheap_signals_share_one_fetch_even_when_the_site_is_dead,
               test_robots_redirects_are_followed):
        fn()
    print("\nall fetch checks passed")
