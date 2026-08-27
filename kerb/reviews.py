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
