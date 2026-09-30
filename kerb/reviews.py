"""Review harvesting, opt-in, via the ListUgcPosts RPC.

Ported from the predecessor's `scarrper/app/rpc.py`, whose measurements are the
whole reason this is not a scroller:

    Scrolling makes Maps render every review as a DOM card. By page 100 the
    pane holds 1,000+ cards and every scroll relayouts the whole tree, so pages
    that arrive every ~0.9s early take ~6s late. Chaining the same cursor by
    hand in a fetch loop renders nothing, and pages then arrive at a flat
    ~220ms whether it is page 1 or page 60. On a 3,650-review profile that is
    ~80s of fetching against ~35-45min of scrolling.

WHY THIS NEEDS A BROWSER AT ALL
-------------------------------
The request cannot be synthesised. `x-maps-bgbind` and `x-maps-bgkey` are
session-scoped, so the only way to get a valid one is to let the page issue it
and take a copy. That is what RECORDER_JS does: it patches
XMLHttpRequest.prototype in the live document and keeps the latest review fetch
whole -- url, body and headers. Replaying without those headers gets a
valid-looking request rejected.

Everything after the capture is a plain fetch loop. The browser is a key-cutter,
not a scraper.

WHY IT IS A SEPARATE COMMAND
----------------------------
Normal Kerb never opens a browser. Reviews are the one thing that needs one, and
most runs do not want them -- so they are a second, deliberate step over a run
that already exists:

    kerb run brief.yaml            # no browser, no reviews
    kerb reviews <run-id>          # only if you actually want them

Which also means reviews are harvested for the businesses that QUALIFIED, not
for everything discovery happened to find.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

# Page size is server-capped at 10 -- asking for 50 or 200 returns an empty
# 166-byte response, so the only lever on wall time is round-trip latency.
PAGE_SIZE = 10

# Patch XHR on the live document and stash the first review fetch we see, whole.
# Headers matter: replaying without X-Same-Domain and the bg* pair gets a
# valid-looking request rejected.
RECORDER_JS = r"""
if (!window.__kerbRec) {
  window.__kerbRec = true;
  window.__cap = null;
  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  const oh = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function(m, u, ...r) {
    this.__u = u; this.__h = {}; return oo.call(this, m, u, ...r);
  };
  XMLHttpRequest.prototype.setRequestHeader = function(k, v) {
    try { this.__h[k] = v; } catch (e) {}
    return oh.call(this, k, v);
  };
  XMLHttpRequest.prototype.send = function(b) {
    try {
      const body = String(b || '');
      // Keep the LATEST page-issued fetch, not the first: re-sorting reissues
      // the request, and first-write-wins would replay the pre-sort template
      // and silently harvest the wrong order. Our own replays tag themselves
      // so the chain cannot clobber the template it is chaining from.
      // Match the TRANSPORT, not the rpc name. The original keyed on the
      // literal 'ListUgcPosts'; Google has since obfuscated the rpcid to
      // things like `qv9Egd`, at which point a name match captures nothing
      // and the harvest silently reports zero reviews. Any batchexecute the
      // page issues is a candidate; a named one is preferred if it appears,
      // and the caller decides by whether the payload actually chains.
      if (!this.__replay && String(this.__u).indexOf('batchexecute') !== -1) {
        const named = body.indexOf('ListUgcPosts') !== -1;
        if (named || !window.__capNamed) {
          window.__cap = {url: String(this.__u), body: body, headers: this.__h,
                          named: named, at: Date.now()};
          if (named) window.__capNamed = true;
        }
      }
    } catch (e) {}
    return os.call(this, b);
  };
}
return !!window.__cap;
"""

# Fetch up to `batch` pages, continuing from the cursor left by the previous
# call. Fields are picked in the page rather than shipping raw payloads back: a
# page is ~140KB, so hundreds of them is tens of MB of data we would discard.
CHAIN_JS = r"""
const batch = arguments[0], done = arguments[arguments.length - 1];
const cap = window.__cap;
if (!cap) { done({error: 'no request captured'}); return; }

if (!window.__chain) {
  const parts = cap.body.split('&');
  let idx = -1, req = null;
  for (let i = 0; i < parts.length; i++)
    if (parts[i].indexOf('f.req=') === 0) { idx = i; req = decodeURIComponent(parts[i].slice(6)); }
  if (idx < 0) { done({error: 'no f.req in captured body'}); return; }
  // Page one sends [10,""] -- an empty cursor, so the token group must allow
  // zero characters or the very first request never matches.
  const pag = req.match(/\[(\d+),\\"([^\\"]*)\\"\]/);
  if (!pag) { done({error: 'no paging arg'}); return; }
  // Start from an EMPTY cursor, not the captured one. By the time a fetch is
  // observable the pane has already consumed the head of the feed, so replaying
  // the captured token would begin at offset 10-20 and quietly lose page one.
  window.__chain = {parts: parts, idx: idx, req: req, pag: pag, token: '', pages: 0};
}
const st = window.__chain;

function bodyFor(token) {
  const p = st.parts.slice();
  p[st.idx] = 'f.req=' + encodeURIComponent(
      st.req.replace(st.pag[0], '[' + st.pag[1] + ',\\"' + token + '\\"]'));
  return p.join('&');
}
function payload(text) {
  const body = text.slice(text.indexOf('\n') + 1);
  const lines = body.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();
    if (s.slice(0, 2) !== '[[') continue;
    let frame;
    try { frame = JSON.parse(s); } catch (e) { continue; }
    for (let j = 0; j < frame.length; j++) {
      const e = frame[j];
      if (Array.isArray(e) && typeof e[2] === 'string') {
        try { return JSON.parse(e[2]); } catch (err) {}
      }
    }
  }
  return null;
}
function pick(o, path) {
  for (let i = 0; i < path.length; i++) {
    if (o === null || o === undefined) return null;
    o = o[path[i]];
  }
  return (o === undefined) ? null : o;
}
function post(token) {
  return new Promise(function (res) {
    const x = new XMLHttpRequest();
    x.__replay = true;                     // so the recorder ignores it
    x.open('POST', cap.url, true);
    for (const k in cap.headers) { try { x.setRequestHeader(k, cap.headers[k]); } catch (e) {} }
    x.onload  = function () { res({status: x.status, text: x.responseText}); };
    x.onerror = function () { res({status: 0, text: ''}); };
    x.send(bodyFor(token));
  });
}

(async function () {
  const out = [];
  let finished = false, failed = null;
  for (let i = 0; i < batch; i++) {
    const r = await post(st.token);
    if (r.status !== 200 || !r.text) { failed = 'http ' + r.status; break; }
    const p = payload(r.text);
    if (!p) { failed = 'unparseable payload'; break; }
    const items = p[2] || [];
    for (let k = 0; k < items.length; k++) {
      const v = items[k][0];
      if (!v) continue;
      out.push({
        id: pick(v, [0]),
        reviewer: pick(v, [1, 4, 5, 0]),
        rating: pick(v, [2, 0, 0]),
        text: pick(v, [2, 15, 0, 0]),
        relative_date: pick(v, [1, 6]),
        timestamp_us: pick(v, [1, 2]),
        owner_reply: pick(v, [3, 14, 0, 0]),
        photos: (pick(v, [2, 2]) || []).length
      });
    }
    st.pages++;
    // p[1] is the next cursor; absent means the feed is genuinely exhausted --
    // unlike an idle scroll, which only means "nothing rendered yet".
    if (!p[1]) { finished = true; break; }
    st.token = p[1];
  }
  done({reviews: out, pages: st.pages, done: finished, failed: failed});
})();
"""


class HarvestError(RuntimeError):
    """No request captured, or the payload stopped parsing.

    Raised rather than returning a short list, because a truncated review set
    that looks complete is exactly the dishonesty this project keeps removing.
    """


def open_reviews(driver, url: str, settle: float = 2.0, timeout: float = 20.0) -> bool:
    """Open a place and get its review pane to issue one real fetch.

    Returns True once a request has been captured. The recorder is injected
    BEFORE the tab is clicked, or the fetch it is waiting for has already gone.
    """
    driver.get(url)
    time.sleep(settle)
    driver.execute_script(RECORDER_JS)
    # Forget anything captured during page load: Maps issues an unrelated
    # batchexecute before the reviews pane is ever opened, and chaining that
    # one returns a payload with no review items in it.
    driver.execute_script("window.__cap = null; window.__capNamed = false;")

    # Click whatever the Reviews tab is called in this locale. Matching on the
    # aria-label rather than the visible text keeps it working when the label is
    # "Reviews for X" or a translation.
    deadline = time.time() + timeout
    clicked = False
    while time.time() < deadline:
        if not clicked:
            try:
                for el in driver.find_elements("css selector", 'button[role="tab"]'):
                    label = (el.get_attribute("aria-label") or el.text or "").lower()
                    if "review" in label:
                        el.click()
                        clicked = True
                        break
            except Exception:                       # noqa: BLE001
                pass
        if driver.execute_script(RECORDER_JS):      # returns !!window.__cap
            return True
        time.sleep(0.4)
    return False


def harvest(driver, url: str, max_reviews: Optional[int] = None,
            batch: int = 25, max_pages: int = 5000,
            on_progress: Optional[Callable[[int], None]] = None) -> List[Dict[str, Any]]:
    """Every review for one business, by chaining the cursor to the end.

    Raises HarvestError if nothing was ever captured. A PARTIAL result is
    returned rather than raised -- some reviews with a warning beats none --
    but an empty one is always an error, never an empty feed.
    """
    if not open_reviews(driver, url):
        raise HarvestError(
            "no review request was captured. Usually the signed-out view: it "
            "has no Reviews tab at all. Sign the profile in once with "
            "`kerb setup --import-cookies FILE`.")

    driver.set_script_timeout(180)
    driver.execute_script("window.__chain = null;")
    rows: List[Dict[str, Any]] = []
    seen = set()
    pages = 0

    while pages < max_pages:
        before = len(rows)
        res = driver.execute_async_script(CHAIN_JS, batch)
        if res.get("error"):
            raise HarvestError("review harvest: %s" % res["error"])
        for row in res.get("reviews") or []:
            key = row.get("id")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(row)
        pages = res.get("pages") or pages
        if on_progress:
            on_progress(len(rows))
        if res.get("failed"):
            if not rows:
                raise HarvestError("review harvest failed: %s" % res["failed"])
            break                                   # partial is worth keeping
        if res.get("done"):
            break
        if len(rows) == before:
            # A whole batch yielded nothing new: the cursor is looping. Without
            # this the loop grinds to max_pages returning duplicates.
            break
        if max_reviews and len(rows) >= max_reviews:
            break

    return rows[:max_reviews] if max_reviews else rows

# Harvest MANY businesses from one captured template, without navigating to any
# of them. See harvest_many() for why this is the whole optimisation.
#
# Two scripts. START sets up a worker pool that runs inside the page and
# returns at once; POLL hands back whatever has finished since the last call.
# The pool used to run as one long async script per group of eight: every
# group waited for its slowest business before the next began, and a group
# longer than Selenium's 120-second client timeout crashed the whole command --
# one popular business with a few thousand reviews was enough.
MANY_START_JS = r"""
const cids = arguments[0], pages = arguments[1], limit = arguments[2];
const conc = arguments[3];
const cap = window.__cap;
if (!cap) return {error: 'no request captured'};
if (window.__many && window.__many.running > 0) return {error: 'a harvest is already running'};

const parts = cap.body.split('&');
let idx = -1, tmpl = null;
for (let i = 0; i < parts.length; i++)
  if (parts[i].indexOf('f.req=') === 0) { idx = i; tmpl = decodeURIComponent(parts[i].slice(6)); }
if (idx < 0) return {error: 'no f.req in captured body'};
const CID_RE = /0x[0-9a-f]+:0x[0-9a-f]+/i;
if (!CID_RE.test(tmpl)) return {error: 'no cid in template'};

function bodyFor(cid, token) {
  let req = tmpl.replace(CID_RE, cid);
  const pag = req.match(/\[(\d+),\\"([^\\"]*)\\"\]/);
  if (!pag) return null;
  req = req.replace(pag[0], '[' + pag[1] + ',\\"' + token + '\\"]');
  const p = parts.slice();
  p[idx] = 'f.req=' + encodeURIComponent(req);
  return p.join('&');
}
function payload(text) {
  const body = text.slice(text.indexOf('\n') + 1);
  const lines = body.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();
    if (s.slice(0, 2) !== '[[') continue;
    let frame; try { frame = JSON.parse(s); } catch (e) { continue; }
    for (let j = 0; j < frame.length; j++) {
      const e = frame[j];
      if (Array.isArray(e) && typeof e[2] === 'string') {
        try { return JSON.parse(e[2]); } catch (err) {}
      }
    }
  }
  return null;
}
function pick(o, path) {
  for (let i = 0; i < path.length; i++) { if (o == null) return null; o = o[path[i]]; }
  return (o === undefined) ? null : o;
}
function post(cid, token) {
  return new Promise(function (res) {
    const b = bodyFor(cid, token);
    if (!b) { res({status: 0, text: null}); return; }
    const x = new XMLHttpRequest();
    x.__replay = true;                      // the recorder must ignore our own
    x.open('POST', cap.url, true);
    // A page that never answers must cost that business, not hang the pool.
    x.timeout = 60000;
    for (const k in cap.headers) { try { x.setRequestHeader(k, cap.headers[k]); } catch (e) {} }
    x.onload    = function () { res({status: x.status, text: x.status === 200 ? x.responseText : null}); };
    x.onerror   = function () { res({status: 0, text: null}); };
    x.ontimeout = function () { res({status: 0, text: null}); };
    x.send(b);
  });
}

const st = window.__many = {total: cids.length, next: 0, done: 0, pages: 0,
                            running: 0, ready: [], blocked: false};

async function one(cid) {
  const out = []; const seen = {};
  let token = '', failed = null;
  for (let i = 0; i < pages; i++) {
    const r = await post(cid, token);
    st.pages++;
    if (r.status === 429) { st.blocked = true; failed = 'rate limited (429)'; break; }
    if (!r.text) { failed = 'request failed' + (r.status ? ' (' + r.status + ')' : ''); break; }
    const p = payload(r.text);
    if (!p) { failed = 'unparseable payload'; break; }
    const items = p[2] || [];
    for (let k = 0; k < items.length; k++) {
      const v = items[k][0]; if (!v) continue;
      const id = pick(v, [0]);
      if (id && seen[id]) continue;
      if (id) seen[id] = 1;
      out.push({id: id,
                reviewer: pick(v, [1, 4, 5, 0]),
                rating: pick(v, [2, 0, 0]),
                text: pick(v, [2, 15, 0, 0]),
                relative_date: pick(v, [1, 6]),
                timestamp_us: pick(v, [1, 2]),
                owner_reply: pick(v, [3, 14, 0, 0]),
                photos: (pick(v, [2, 2]) || []).length});
    }
    if (!p[1]) break;                       // cursor exhausted: the real end
    if (limit && out.length >= limit) break;
    token = p[1];
  }
  return {cid: cid, reviews: limit ? out.slice(0, limit) : out, failed: failed};
}

// Each business has its OWN cursor, so they are genuinely independent -- a
// worker that finishes takes the next business at once, with no batch to
// wait for. Concurrency is still capped: the point is to stop wasting time on
// page loads, not to flood Google. A 429 stops every worker taking new work.
async function worker() {
  st.running++;
  try {
    while (st.next < cids.length && !st.blocked) {
      const mine = st.next++;
      let r;
      try { r = await one(cids[mine]); }
      catch (e) { r = {cid: cids[mine], reviews: [], failed: 'script error: ' + e}; }
      st.ready.push(r);
      st.done++;
    }
  } finally { st.running--; }
}
const n = Math.min(Math.max(1, conc), cids.length);
for (let i = 0; i < n; i++) worker();
return {started: n, total: cids.length};
"""

MANY_POLL_JS = r"""
const st = window.__many;
if (!st) return {error: 'no harvest is running'};
const out = st.ready.splice(0, st.ready.length);
return {results: out, done: st.done, total: st.total, pages: st.pages,
        running: st.running, blocked: st.blocked};
"""


def harvest_many(driver, cids, capture_url, max_reviews=None,
                 pages=500, concurrency=4, group=None,
                 on_business=None, poll=0.5, stall=180.0):
    """Reviews for MANY businesses from a single captured request.

    The optimisation, and why it is worth the extra code: the captured `f.req`
    carries the business's own cid inline, so swapping it addresses a different
    business entirely. Nothing needs to be navigated to.

    Measured on one business, 30 reviews:

        navigate to it, then harvest   6.2s
        swap the cid, no navigation    0.5s     -- same reviews, same order

    Page load, tab click and capture were ~4.4s of fixed cost PER BUSINESS.
    Paying it once instead of a hundred times is where the time goes.

    A pool of `concurrency` workers runs inside the page; each takes the next
    business the moment it finishes one. Python polls every `poll` seconds
    and reports each business as it completes, so a crash keeps everything
    already written. `group` is accepted for compatibility and ignored: there
    are no groups any more to wait on.

    Raises HarvestError when nothing was captured, when the pages stop
    advancing for `stall` seconds, or when Google rate-limits the requests --
    after reporting every business that finished first.
    """
    if not open_reviews(driver, capture_url):
        raise HarvestError(
            "no review request was captured. Usually the signed-out view: it "
            "has no Reviews tab at all. Sign the profile in once with "
            "`kerb setup --import-cookies FILE`.")

    cids = list(cids)
    out: Dict[str, Any] = {}
    if not cids:
        return out
    started = driver.execute_script(MANY_START_JS, cids, pages, max_reviews or 0,
                                    max(1, int(concurrency)))
    if not isinstance(started, dict) or started.get("error"):
        raise HarvestError("review harvest: %s"
                           % ((started or {}).get("error") or "the pool did not start"))

    last_pages, last_move = -1, time.monotonic()
    while True:
        res = driver.execute_script(MANY_POLL_JS) or {}
        if res.get("error"):
            raise HarvestError("review harvest: %s" % res["error"])
        for row in res.get("results") or []:
            if not row:
                continue
            out[row["cid"]] = row
            if on_business:
                on_business(row["cid"], row.get("reviews") or [], row.get("failed"))
        if res.get("blocked") and not res.get("running"):
            raise HarvestError(
                "Google rate-limited the review requests after %d of %d "
                "business(es); stopped rather than push on. Wait, then re-run "
                "with fewer --workers." % (res.get("done", 0), len(cids)))
        if res.get("done", 0) >= res.get("total", len(cids)):
            return out
        if res.get("pages", 0) != last_pages:
            last_pages, last_move = res.get("pages", 0), time.monotonic()
        elif time.monotonic() - last_move > stall:
            raise HarvestError(
                "the review harvest stopped advancing for %ds with %d of %d "
                "business(es) done" % (stall, res.get("done", 0), len(cids)))
        time.sleep(poll)
