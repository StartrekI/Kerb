"""Scoring maths, and the malformed-spec bug that made every business tie.

The centrepiece here is `test_cap_true_is_rejected`. A weight written as
`{weight: 80, scale: 200, cap: true}` used to score all five qualified
businesses exactly 100.0 -- 31 reviews tying 142 -- because `float(True)` is
1.0 and `min(reviews, 1.0)` flattened every input to the same number. Nothing
raised. The ranking looked plausible and was uniformly wrong.

That is the exact failure this whole tool argues against, committed by the
tool itself, so it gets a test that names it.
"""

import math

from kerb import scoring
from kerb.models import Business, Signal, Verdict


def verdict(reviews=100, presence="none", trade="dentist") -> Verdict:
    v = Verdict(business=Business(cid="0x1:0x1", name="Test"))
    v.add(Signal("reviews", reviews, 1.0, {}))
    v.add(Signal("web_presence", presence, 1.0, {}))
    v.add(Signal("trade_match", trade, 0.95, {}))
    return v


# -- the bug -------------------------------------------------------------

def test_cap_true_is_rejected():
    """`cap: true` must be a loud error, never a silent 1.0."""
    problems = scoring.check_weight("reviews", {"weight": 80, "scale": 200, "cap": True})
    assert problems, "cap:true was accepted -- this is the all-tie bug"
    assert any("cap" in p for p in problems)
    assert any("scale" in p for p in problems), "scale:200 should also be caught"


def test_cap_true_no_longer_ties_everything():
    """The original symptom: different review counts must not score the same."""
    bad = {"weight": 80, "scale": 200, "cap": True}
    for spec in (bad,):
        try:
            scoring._points(142, spec)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed spec scored instead of raising")

    good = {"weight": 80, "scale": "linear", "cap": 200}
    assert scoring._points(142, good) > scoring._points(31, good), \
        "142 reviews must outrank 31"


def test_missing_cap_is_rejected():
    """Linear and log both divide by cap; without one every input scores full."""
    for spec in ({"weight": 40}, {"weight": 40, "cap": 0}, {"weight": 40, "cap": -5}):
        assert scoring.check_weight("reviews", spec), "spec %r must be rejected" % spec


def test_unknown_scale_is_rejected():
    assert scoring.check_weight("reviews", {"weight": 40, "scale": "sqrt", "cap": 100})


# -- the maths -----------------------------------------------------------

def test_linear_scales_proportionally():
    spec = {"weight": 100, "scale": "linear", "cap": 200}
    assert scoring._points(0, spec) == 0.0
    assert scoring._points(100, spec) == 50.0
    assert scoring._points(200, spec) == 100.0


def test_cap_clamps_rather_than_overflows():
    spec = {"weight": 50, "scale": "linear", "cap": 100}
    assert scoring._points(100, spec) == 50.0
    assert scoring._points(9999, spec) == 50.0, "past the cap must not exceed the weight"


def test_log_saturates():
    """The point of the log curve: early reviews matter far more than late ones."""
    spec = {"weight": 100, "scale": "log", "cap": 300}
    low = scoring._points(60, spec) - scoring._points(10, spec)
    high = scoring._points(300, spec) - scoring._points(250, spec)
    assert low > high, "10->60 should be worth more than 250->300"
    assert scoring._points(300, spec) == 100.0


def test_categorical_map():
    spec = {"none": 40, "booking_only": 32, "owned_domain": 0}
    assert scoring._points("none", spec) == 40.0
    assert scoring._points("booking_only", spec) == 32.0
    assert scoring._points("owned_domain", spec) == 0.0
    assert scoring._points("something_else", spec) == 0.0, "unknown value earns nothing"


def test_scalar_is_presence_based():
    assert scoring._points("dentist", 20) == 20.0
    assert scoring._points(0, 20) == 0.0
    assert scoring._points(None, 20) == 0.0


# -- normalisation -------------------------------------------------------

def test_normalise_uses_max_possible():
    weights = {"web_presence": {"none": 40, "builder": 10},
               "reviews": {"weight": 30, "scale": "linear", "cap": 100},
               "trade_match": 30}
    v = scoring.score(verdict(reviews=100), weights, normalise=True)
    assert v.score == 100.0, "best case on every signal is 100"

    v2 = scoring.score(verdict(reviews=0, presence="builder"), weights, normalise=True)
    assert v2.score == round((10 + 0 + 30) / 100 * 100, 1)


def test_breakdown_sums_to_raw_total():
    weights = {"web_presence": {"none": 40},
               "reviews": {"weight": 30, "scale": "linear", "cap": 100}}
    v = scoring.score(verdict(reviews=50), weights, normalise=False)
    assert round(sum(v.breakdown.values()), 2) == v.score


# -- filters -------------------------------------------------------------

def test_filter_reports_which_rule_failed():
    ok, failed, reason = scoring.evaluate(
        verdict(reviews=12), [{"signal": "reviews", "op": ">=", "value": 30}])
    assert not ok and failed == "reviews"
    assert "12" in reason and "30" in reason


def test_unknown_never_satisfies_an_ordering():
    """A missing measurement must not pass a >= test by accident."""
    v = Verdict(business=Business(cid="0x1:0x1", name="T"))
    v.add(Signal("reviews", None, 0.0, {}))
    ok, _, _ = scoring.evaluate(v, [{"signal": "reviews", "op": ">=", "value": 1}])
    assert not ok


def test_any_group():
    rules = [{"group": "any", "of": [
        {"signal": "web_presence", "op": "==", "value": "none"},
        {"signal": "reviews", "op": ">=", "value": 500}]}]
    assert scoring.evaluate(verdict(presence="none"), rules)[0]
    assert not scoring.evaluate(verdict(presence="owned_domain", reviews=10), rules)[0]


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print("  ok  %s" % name)
    print("\n%d scoring checks passed" % passed)
