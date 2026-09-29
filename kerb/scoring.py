"""Filtering and scoring -- turning measurements into a decision, explainably.

Two jobs, deliberately separate:

  filters   a hard yes/no. A business that fails one is out, and the run
            records WHICH filter rejected it, because "0 results" with no
            reason is the most useless output a tool can produce.

  scoring   a 0-100 ranking of everything that survived, with a per-signal
            breakdown showing where every point came from.

Nothing here fetches. Both operate on signals already computed, which is what
makes re-scoring with new weights instant and free -- change the weights, keep
the data, get a new ranking with no refetch.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .models import Signal, Verdict

# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

def _as_number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


OPS = {
    "==":     lambda a, b: a == b,
    "!=":     lambda a, b: a != b,
    "in":     lambda a, b: a in b if isinstance(b, (list, tuple, set)) else a == b,
    "not in": lambda a, b: a not in b if isinstance(b, (list, tuple, set)) else a != b,
    "is":     lambda a, b: bool(a) is bool(b),
}
# Numeric comparisons. Kept apart from OPS because unknown values never satisfy
# them, where OPS compares whatever it is given.
ORDERING = (">", ">=", "<", "<=")


def _compare(op: str, actual, want) -> bool:
    if op in OPS:
        return OPS[op](actual, want)
    if op in ORDERING:
        a, b = _as_number(actual), _as_number(want)
        if a is None or b is None:
            return False                      # unknown never satisfies an ordering
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
    raise ValueError("unknown operator %r" % op)


def _resolve(verdict: Verdict, path: str):
    """`reviews` or `establishment_age.first_review_year` -- signal, then evidence."""
    if "." not in path:
        return verdict.get(path)
    name, key = path.split(".", 1)
    s = verdict.signals.get(name)
    if s is None:
        return None
    if isinstance(s.evidence, dict) and key in s.evidence:
        return s.evidence[key]
    return None


def _signal_failed(verdict: Verdict, path: str) -> bool:
    s = verdict.signals.get(path.split(".")[0])
    return bool(s is not None and s.failed)


def evaluate(verdict: Verdict, rules: List[Dict[str, Any]],
             available: Optional[set] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    """(passed, failing_rule, human_reason). All top-level rules must pass.

    A rule whose signal BROKE does not reject the business -- it makes the
    business unevaluable. `unmeasurable:` on the failing rule name is how the
    caller tells the two apart, and the difference is not cosmetic: recording
    a network error as a rejection is a claim about the business that the data
    does not support, and it is permanent.

    `available` names the signals measured so far. The pipeline evaluates after
    each cost tier, so without it a filter on a CHEAP signal was tested while
    that signal was still unmeasured: it resolved to None, failed, and rejected
    the business before the tier that would have answered it ever ran. Every
    campaign filtering on anything but free data returned nothing, and the
    tiering that is the whole economic argument could never execute.
    """
    for rule in rules or []:
        if "group" in rule:
            mode = rule.get("group", "all")
            subs = rule.get("of") or []
            results = [evaluate(verdict, [s], available) for s in subs]
            ok = (any(r[0] for r in results) if mode == "any"
                  else all(r[0] for r in results))
            if not ok:
                # If the group could only fail because measurements broke, the
                # group is unmeasurable rather than unmet.
                blocking = [r for r in results if not r[0]]
                if blocking and all(str(r[1]).startswith("unmeasurable:")
                                    for r in blocking):
                    return False, blocking[0][1], blocking[0][2]
                return False, "group:%s" % mode, \
                    "no condition in the %s group matched" % mode
            continue

        path = rule["signal"]
        if available is not None and path.split(".")[0] not in available:
            continue                   # not measured yet; a later tier decides
        op = rule.get("op", "==")
        want = rule.get("value")
        actual = _resolve(verdict, path)
        if not _compare(op, actual, want):
            if _signal_failed(verdict, path):
                err = (verdict.signals[path.split(".")[0]].evidence or {}).get("error")
                return False, "unmeasurable:%s" % path, \
                    "%s could not be measured (%s) -- not judged" % (path, err)
            return False, path, "%s is %r, needs %s %r" % (path, actual, op, want)
    return True, None, None


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

SCALES = ("linear", "log")


def check_weight(name: str, spec) -> List[str]:
    """Problems with one weight spec, in the user's words. Empty means usable.

    This exists because the failure mode it catches is invisible. A scaled spec
    written as `{weight: 80, scale: 200, cap: true}` used to score every single
    business 100.0 -- `float(True)` is 1.0, so `min(reviews, 1.0)` flattened
    142 reviews and 31 reviews to the same value, and the run produced a
    confident, plausible, completely uniform ranking. A weight spec that cannot
    rank must say so, not quietly rank everything equally.
    """
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return []
    if not isinstance(spec, dict):
        return ["weight for %r must be a number or a mapping, got %s"
                % (name, type(spec).__name__)]
    if "weight" not in spec:
        bad = [k for k, v in spec.items()
               if not isinstance(v, (int, float)) or isinstance(v, bool)]
        return ["weight for %r maps %s to a non-number" % (name, ", ".join(map(repr, bad)))
                ] if bad else []

    problems = []
    if isinstance(spec["weight"], bool) or not isinstance(spec["weight"], (int, float)):
        problems.append("weight for %r must be a number" % name)
    scale = spec.get("scale", "linear")
    if scale not in SCALES:
        problems.append("weight for %r has scale %r; use one of %s"
                        % (name, scale, " or ".join(SCALES)))
    cap = spec.get("cap")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        problems.append(
            "weight for %r needs a positive numeric 'cap' -- the value that earns "
            "full points (e.g. cap: 300 means 300+ reviews score full marks). "
            "Without one there is nothing to scale against and every business "
            "scores identically." % name)
    if "invert" in spec and not isinstance(spec["invert"], bool):
        problems.append("weight for %r: invert must be true or false" % name)
    unknown = set(spec) - {"weight", "scale", "cap", "invert"}
    if unknown:
        problems.append("weight for %r has unknown key(s) %s; a scaled weight takes "
                        "weight, scale, cap and invert"
                        % (name, ", ".join(sorted(map(repr, unknown)))))
    return problems


def _points(value, spec) -> float:
    """One signal's contribution, before normalisation.

    Three spec shapes, chosen by what the signal returns:
      40                        award the full weight if the signal is truthy
      {none: 40, builder: 20}   categorical, points per value
      {weight: 25, scale: log, cap: 300}   numeric, scaled against cap
      {weight: 25, scale: log, cap: 10, invert: true}   numeric, lower is better
                                (chain_size: an independent beats a branch)
    """
    problems = check_weight("?", spec)
    if problems:
        raise ValueError(problems[0])

    if isinstance(spec, (int, float)):
        # Presence-based: full marks for any truthy value. Correct for booleans
        # and categoricals; for a numeric signal use the scaled form instead,
        # or 1 review and 500 reviews both earn the same points.
        n = _as_number(value)
        return float(spec) if (n is None and value) else (float(spec) if n else 0.0)

    if "weight" in spec:                       # numeric, scaled against cap
        n = _as_number(value)
        if n is None:
            return 0.0
        weight, cap = float(spec["weight"]), float(spec["cap"])
        n = min(max(n, 0.0), cap)
        if spec.get("scale", "linear") == "log":
            # Saturating: the difference between 10 and 60 reviews matters far
            # more than between 250 and 300.
            frac = math.log10(n + 1) / math.log10(cap + 1)
        else:
            frac = n / cap
        frac = max(0.0, min(1.0, frac))
        if spec.get("invert"):
            frac = 1.0 - frac
        return weight * frac

    # Categorical map.
    key = value.value if hasattr(value, "value") else value
    return float(spec.get(key, spec.get(str(key), 0)) or 0)


def score(verdict: Verdict, weights: Dict[str, Any],
          normalise: bool = True, confidence: bool = False) -> Verdict:
    """Attach a score and a per-signal breakdown. Never fetches anything.

    `confidence=True` multiplies each signal's points by how sure that signal
    is of its own value. A `web_presence` read straight off the website field
    carries confidence 1.0; the same value inferred from a booking-field
    redirect carries 0.9 -- and by default they score identically, which is a
    project built on visible uncertainty throwing it away at the last step.

    Off by default because turning it on changes every existing score.
    """
    breakdown: Dict[str, float] = {}
    total = 0.0
    possible = 0.0

    for name, spec in (weights or {}).items():
        value = _resolve(verdict, name)
        pts = _points(value, spec)
        if confidence:
            sig = verdict.signals.get(name.split(".")[0])
            if sig is not None:
                pts *= sig.confidence
        breakdown[name] = round(pts, 2)
        total += pts
        if isinstance(spec, (int, float)):
            possible += float(spec)
        elif isinstance(spec, dict):
            possible += float(spec["weight"]) if "weight" in spec else max(
                [float(v or 0) for v in spec.values()] or [0.0])

    if normalise and possible > 0:
        total = total / possible * 100.0

    verdict.score = round(total, 1)
    verdict.breakdown = breakdown
    return verdict


def check_bands(bands: Any) -> List[str]:
    """Problems with a `scoring.bands` block, in the user's words."""
    if bands in (None, [], {}):
        return []
    if not isinstance(bands, list):
        return ["scoring.bands must be a list of {min, label}"]
    problems = []
    for i, band in enumerate(bands):
        if not isinstance(band, dict):
            problems.append("scoring.bands[%d] must be a mapping with min and label" % i)
            continue
        if "label" not in band:
            problems.append("scoring.bands[%d] has no label" % i)
        low = band.get("min", band.get("from"))
        if isinstance(low, bool) or not isinstance(low, (int, float)):
            problems.append("scoring.bands[%d] needs a numeric 'min'" % i)
    return problems


def band_for(score_value: Optional[float], bands: Any) -> Optional[str]:
    """The label a score falls into.

    Bands are sorted by threshold descending and the first match wins, so the
    order they are written in cannot change the answer -- a list a user
    reordered while editing should not silently re-label the whole dataset.
    """
    if not bands or score_value is None:
        return None
    ordered = sorted(
        (b for b in bands if isinstance(b, dict)),
        key=lambda b: -float(b.get("min", b.get("from", 0)) or 0))
    for band in ordered:
        if float(score_value) >= float(band.get("min", band.get("from", 0)) or 0):
            return str(band.get("label"))
    return None


def explain(verdict: Verdict) -> List[Dict[str, Any]]:
    """Rows for the evidence drawer: what each signal found and what it was worth."""
    rows = []
    for name, pts in sorted(verdict.breakdown.items(), key=lambda kv: -kv[1]):
        s: Optional[Signal] = verdict.signals.get(name.split(".")[0])
        rows.append({
            "signal": name,
            "value": None if s is None else s.value,
            "confidence": None if s is None else round(s.confidence, 2),
            "points": pts,
            "evidence": {} if s is None else s.evidence,
        })
    return rows
