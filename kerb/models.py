"""Core domain types.

Three ideas carry the whole design, and everything else follows from them:

1. **A business is identified by its CID, never by its name.** Google's own
   place id. Name+address matching merged a Dallas and a Sydney "Lure Salon"
   into one record in an earlier tool; that class of bug is not recoverable
   after the fact, so identity is exact or it is nothing.

2. **A signal returns evidence, not just a verdict.** `(value, confidence,
   evidence)`. A score you cannot audit is a score you cannot defend to a
   client, and defending it is the entire product.

3. **Uncertainty is a value, not a null.** `UNCLEAR` is a first-class outcome
   that never silently becomes "no". A tool that guesses is worse than one
   that admits it does not know, because the guess is invisible.

Plain dataclasses on purpose: the domain has no reason to depend on a
validation library, and the API layer can build its own schemas from these.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------

class WebPresence(str, Enum):
    """What the website slot on a listing actually MEANS.

    The distinction competitors collapse: a URL is not a website. A business
    renting a page on `vagaro.com` has no website and is a prospect; one on
    `theirsalon.com` is not. Filtering a scraper's CSV on `website IS NULL`
    silently discards every business in the middle three states -- which in
    the salon and barber trades is a large share of the market.
    """
    NONE = "none"                  # nothing at all
    SOCIAL_ONLY = "social_only"    # a Facebook/Instagram page
    BOOKING_ONLY = "booking_only"  # renting a page on a booking platform
    BUILDER = "builder"            # free site builder (square.site, business.site)
    OWNED_DOMAIN = "owned_domain"  # a real site on their own domain
    UNKNOWN = "unknown"            # not established -- never assume NONE


class Liveness(str, Enum):
    OPEN = "open"
    TEMP_CLOSED = "temp_closed"
    PERM_CLOSED = "perm_closed"
    STALE = "stale"                # open, but nothing has happened in years
    UNKNOWN = "unknown"


class ReviewIntegrity(str, Enum):
    """Whether a review set can be trusted to be complete.

    Google's feed truncates silently -- HTTP 200, well formed, just short.
    It stamped 17 businesses with the wrong first-review year before this
    was detected. Anything derived from a TRUNCATED set is unsafe to score.
    """
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    UNAVAILABLE = "unavailable"


UNCLEAR = "unclear"


# --------------------------------------------------------------------------
# Business
# --------------------------------------------------------------------------

@dataclass
class Business:
    """One local business, normalised across every source.

    `extras` keeps whatever the source gave us that we have no column for.
    Nothing is discarded: a field we ignore today is a signal someone writes
    next month, and re-fetching to recover it costs far more than storing it.
    """
    cid: str
    name: str = ""
    category: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    booking_url: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    hours: Optional[Dict[str, Any]] = None
    status_raw: Optional[str] = None
    first_review_year: Optional[int] = None
    reviews_fetched: Optional[int] = None
    source: str = "unknown"
    place_label: Optional[str] = None       # the search/locality that found it
    extras: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.cid:
            raise ValueError("a Business without a cid has no identity")
        self.cid = self.cid.strip().lower()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def display(self) -> str:
        return self.name or self.cid


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------

@dataclass
class Signal:
    """One measurement about one business.

    `confidence` is how sure the signal is of its own value, NOT how good the
    business is -- those get conflated constantly and the distinction matters
    when a score is challenged. A `web_presence=none` read straight off an
    empty field is confidence 1.0; the same value inferred from a redirect
    chain is not.

    `evidence` is whatever a human would need to check the verdict: the host
    that matched, the pack and version that supplied the rule, the counts
    compared. It is what turns a number into an argument.
    """
    name: str
    value: Any
    confidence: float = 1.0
    evidence: Dict[str, Any] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self):
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within 0..1, got %r" % self.confidence)
        if isinstance(self.value, Enum):
            self.value = self.value.value

    @property
    def failed(self) -> bool:
        """Did the measurement break, as opposed to succeeding at "I can't tell"?

        The distinction decides whether a business gets a verdict at all. A
        signal that legitimately cannot determine something -- no dated reviews
        to infer an age from -- has measured correctly and returns low
        confidence. A signal that threw because the network was down has not
        measured anything, and a business must never be judged on it.
        """
        return bool(isinstance(self.evidence, dict) and self.evidence.get("error"))

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value,
                "confidence": round(self.confidence, 3),
                "evidence": self.evidence, "version": self.version,
                "failed": self.failed}


class Outcome(str, Enum):
    """What actually happened to a business. Three states, not two.

    UNEVALUATED is the one that has to exist. When a measurement fails -- the
    network drops, a host rate-limits us -- the old code recorded the business
    as REJECTED with the error as its reason, which is a claim about the
    business that the data does not support. A brief outage therefore marked
    thousands of perfectly good leads as rejected, permanently, and nothing
    ever revisited them.

    Rejected means "we measured it and it does not qualify". Unevaluated means
    "we never found out". Only the first is a verdict.
    """
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNEVALUATED = "unevaluated"


@dataclass
class Verdict:
    """A business plus everything measured about it, and why it passed or not."""
    business: Business
    signals: Dict[str, Signal] = field(default_factory=dict)
    score: Optional[float] = None
    breakdown: Dict[str, float] = field(default_factory=dict)
    outcome: Outcome = Outcome.UNEVALUATED
    band: Optional[str] = None              # a label for the score, if configured
    rejected_by: Optional[str] = None       # which filter rejected it
    reject_reason: Optional[str] = None

    @property
    def qualified(self) -> bool:
        return self.outcome is Outcome.QUALIFIED

    @qualified.setter
    def qualified(self, value: bool) -> None:
        self.outcome = Outcome.QUALIFIED if value else Outcome.REJECTED

    @property
    def failed_signals(self) -> List[str]:
        return sorted(n for n, s in self.signals.items() if s.failed)

    def get(self, signal: str, default=None):
        s = self.signals.get(signal)
        return default if s is None else s.value

    def confidence(self, signal: str) -> float:
        s = self.signals.get(signal)
        return 0.0 if s is None else s.confidence

    def add(self, signal: Signal) -> None:
        self.signals[signal.name] = signal

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.business.to_dict(),
            "score": self.score,
            "outcome": self.outcome.value,
            "band": self.band,
            "qualified": self.qualified,
            "rejected_by": self.rejected_by,
            "reject_reason": self.reject_reason,
            "failed_signals": self.failed_signals,
            "breakdown": self.breakdown,
            "signals": {k: v.to_dict() for k, v in self.signals.items()},
        }


# --------------------------------------------------------------------------
# Cost tiers -- the economics of the pipeline
# --------------------------------------------------------------------------

class Cost(str, Enum):
    """What a signal costs to compute, which decides when it runs.

    The ordering IS the product's economics: reject on FREE before paying for
    CHEAP, reject on CHEAP before paying for EXPENSIVE. Measured on a real
    run, 74% of candidates were rejected before the expensive stage. A tool
    charging per record cannot do this -- pre-filtering would cannibalise its
    own revenue -- which is why the ordering is a moat and not just a tidy
    implementation detail.
    """
    FREE = "free"            # arithmetic on data already in hand
    CHEAP = "cheap"          # one request
    EXPENSIVE = "expensive"  # many requests, or a browser, or an LLM call


@dataclass
class SourceQuery:
    """What a source adapter is asked for."""
    what: Optional[str] = None                       # trade / occupation
    places: List[str] = field(default_factory=list)
    path: Optional[str] = None                       # for file-based sources
    limit: Optional[int] = None
    options: Dict[str, Any] = field(default_factory=dict)

    # Somewhere for a source to report what went wrong without failing.
    # A source yields businesses and nothing else, so partial trouble -- 3 of
    # 50 towns unreachable -- had no way to reach the user: the run simply
    # returned fewer results and looked complete. The pipeline reads this after
    # the source is exhausted and folds it into the run's stats.
    report: Dict[str, Any] = field(default_factory=dict)
