"""Review harvesting via the ListUgcPosts RPC.

Offline. The driver is faked, because the point under test is the CHAIN
logic -- cursor advance, dedupe, exhaustion, partial results -- not whether
Chrome starts. Getting that logic wrong is how a harvest returns 40 reviews
from a 3,650-review business and looks finished.

    python3 tests/test_reviews.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerb.reviews import HarvestError, harvest        # noqa: E402


class FakeDriver:
    """Stands in for Selenium. `pages` is what the chained fetch returns."""

    def __init__(self, pages, captured=True):
        self.pages = list(pages)      # each: dict returned by CHAIN_JS
        self.captured = captured
        self.visited = []
        self.clicks = 0
        self.script_timeout = None

    # -- the bits harvest()/open_reviews() actually call -------------------
    def get(self, url):
        self.visited.append(url)

    def execute_script(self, js, *a):
        if "__chain = null" in js:
            return None
        return self.captured          # RECORDER_JS returns !!window.__cap

    def execute_async_script(self, js, batch):
        return self.pages.pop(0) if self.pages else {"reviews": [], "done": True}

    def find_elements(self, how, what):
        self.clicks += 1
        return []                     # no tab found; capture flag decides

    def set_script_timeout(self, n):
        self.script_timeout = n


def rev(i):
    return {"id": "r%d" % i, "reviewer": "P%d" % i, "rating": 5,
            "text": "good", "relative_date": "a week ago"}


def test_chains_pages_to_the_end():
    d = FakeDriver([
        {"reviews": [rev(i) for i in range(10)], "pages": 1, "done": False},
        {"reviews": [rev(i) for i in range(10, 18)], "pages": 2, "done": True},
    ])
    got = harvest(d, "https://maps/x")
    assert len(got) == 18, len(got)
    assert got[0]["id"] == "r0" and got[-1]["id"] == "r17"
    assert d.script_timeout == 180
    print("  chains to the end            ok")


def test_duplicate_ids_are_dropped():
    """The cursor can hand back an overlapping page. Counting those twice
    inflates a review total, which is a number people act on."""
    d = FakeDriver([
        {"reviews": [rev(0), rev(1), rev(2)], "pages": 1, "done": False},
        {"reviews": [rev(2), rev(3)], "pages": 2, "done": True},
    ])
    got = harvest(d, "u")
    assert [r["id"] for r in got] == ["r0", "r1", "r2", "r3"], got
    print("  duplicates dropped           ok")


def test_a_looping_cursor_stops_instead_of_grinding():
    """A batch that yields nothing new means the cursor stopped advancing.
    Without the guard this runs to max_pages returning the same page forever."""
    same = {"reviews": [rev(0)], "pages": 1, "done": False}
    d = FakeDriver([same] * 200)
    got = harvest(d, "u", batch=1)
    assert len(got) == 1, len(got)
    assert len(d.pages) > 190, "should have stopped almost immediately"
    print("  looping cursor stops         ok")


def test_partial_is_kept_but_empty_is_an_error():
    """Some reviews with a warning beats none. An EMPTY result, though, must
    never be reported as a finished harvest -- that is indistinguishable from
    a business with no reviews."""
    d = FakeDriver([
        {"reviews": [rev(0), rev(1)], "pages": 1, "done": False},
        {"reviews": [], "pages": 2, "failed": "http 429"},
    ])
    got = harvest(d, "u")
    assert len(got) == 2, "a partial harvest is worth keeping"

    d2 = FakeDriver([{"reviews": [], "pages": 0, "failed": "http 429"}])
    try:
        harvest(d2, "u")
    except HarvestError as exc:
        assert "429" in str(exc)
        print("  partial kept, empty raises   ok")
        return
    raise AssertionError("an empty failed harvest must raise, not return []")


def test_no_capture_names_the_signed_out_cause():
    """The overwhelmingly common failure. Saying 'no request captured' sends
    people debugging; naming the limited view sends them to the fix."""
    d = FakeDriver([], captured=False)
    try:
        harvest(d, "u")
    except HarvestError as exc:
        assert "signed-out" in str(exc) or "Reviews tab" in str(exc), str(exc)
        assert "import-cookies" in str(exc)
        print("  no capture -> signed out     ok")
        return
    raise AssertionError("must raise when nothing was captured")


def test_max_reviews_is_respected():
    # Distinct ids per page. Repeating the same ten would trip the
    # looping-cursor guard first and test the wrong thing.
    d = FakeDriver([{"reviews": [rev(p * 10 + i) for i in range(10)],
                     "pages": p + 1, "done": False} for p in range(5)])
    got = harvest(d, "u", max_reviews=25)
    assert len(got) == 25, len(got)
    print("  max_reviews honoured         ok")


def test_a_chain_error_raises_rather_than_returning_short():
    d = FakeDriver([{"error": "no f.req in captured body"}])
    try:
        harvest(d, "u")
    except HarvestError as exc:
        assert "f.req" in str(exc)
        print("  chain error raises           ok")
        return
    raise AssertionError("a chain error must not look like an empty feed")


def test_progress_is_reported_as_it_goes():
    seen = []
    d = FakeDriver([
        {"reviews": [rev(i) for i in range(10)], "pages": 1, "done": False},
        {"reviews": [rev(i) for i in range(10, 15)], "pages": 2, "done": True},
    ])
    harvest(d, "u", on_progress=seen.append)
    assert seen == [10, 15], seen
    print("  progress reported            ok")


def test_harvest_many_batches_and_reports_each_business():
    """The optimisation: one capture, then every business by swapping the cid
    in the template. Measured 7.4s -> 1.28s per business, same reviews."""
    from kerb.reviews import harvest_many

    class ManyDriver(FakeDriver):
        def __init__(self):
            super().__init__([], captured=True)
            self.groups = []
        def execute_async_script(self, js, cids, pages, limit, conc):
            self.groups.append(list(cids))
            return {"results": [{"cid": c, "reviews": [rev(i) for i in range(3)],
                                 "failed": None} for c in cids]}

    d = ManyDriver()
    seen = []
    cids = ["0x1:0x%d" % i for i in range(20)]
    out = harvest_many(d, cids, "https://maps/x", group=8,
                       on_business=lambda c, r, f: seen.append((c, len(r))))
    assert len(out) == 20, len(out)
    assert [len(g) for g in d.groups] == [8, 8, 4], d.groups
    assert len(seen) == 20 and seen[0][1] == 3
    print("  many: batches + reports      ok")


def test_harvest_many_without_a_capture_names_the_cause():
    from kerb.reviews import harvest_many
    d = FakeDriver([], captured=False)
    try:
        harvest_many(d, ["0x1:0x1"], "u")
    except HarvestError as exc:
        assert "import-cookies" in str(exc)
        print("  many: no capture explained  ok")
        return
    raise AssertionError("must raise when nothing was captured")


if __name__ == "__main__":
    print("reviews -- the ListUgcPosts chain\n")
    for fn in (test_chains_pages_to_the_end,
               test_duplicate_ids_are_dropped,
               test_a_looping_cursor_stops_instead_of_grinding,
               test_partial_is_kept_but_empty_is_an_error,
               test_no_capture_names_the_signed_out_cause,
               test_max_reviews_is_respected,
               test_a_chain_error_raises_rather_than_returning_short,
               test_progress_is_reported_as_it_goes,
               test_harvest_many_batches_and_reports_each_business,
               test_harvest_many_without_a_capture_names_the_cause):
        fn()
    print("\nall review checks passed")
