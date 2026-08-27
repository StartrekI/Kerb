"""Signal registry.

A signal is a pure function from a Business to a measurement. Registering it
declares three things the pipeline needs:

  name     what filters and scoring refer to it by
  cost     when it is allowed to run (see models.Cost -- this is the economics)
  version  bumped when the logic changes, so old scores stay explicable

Signals never do I/O of their own for the FREE tier -- they read what is
already on the Business. CHEAP and EXPENSIVE signals may fetch, and the
pipeline decides whether they are permitted to.

Adding a signal is one decorated function. A plugin adding one from outside
this package works identically, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..models import Business, Cost, Signal
from ..packs import PackLibrary, library


@dataclass
class Context:
    """Everything a signal needs that is not the business itself."""
    packs: PackLibrary = field(default_factory=library)
    options: Dict[str, Any] = field(default_factory=dict)
    # Requests made while measuring. A signal that fetches MUST report here,
    # otherwise `limits.max_requests` is a spend cap that cannot ever trigger.
    requests: int = 0

    def opt(self, signal: str, key: str, default=None):
        """Per-signal options from the campaign, e.g. options.web_presence.builder_counts."""
        return (self.options.get(signal) or {}).get(key, default)

    def note_request(self, n: int = 1) -> None:
        self.requests += n


@dataclass
class Registered:
    fn: Callable[[Business, Context], Signal]
    name: str
    cost: Cost
    version: int
    label: str
    description: str
    # What this signal's value can be, so a builder can offer the right control
    # without hard-coding a list that goes stale. The UI's filter builder used
    # to be a static array and fell ten signals behind the registry; a signal
    # knows its own value domain, so it declares it here.
    kind: str = "text"                 # categorical | number | boolean | text
    values: Optional[List[Any]] = None
    suggest: Optional[Dict[str, Any]] = None   # a sensible default filter

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "label": self.label, "cost": self.cost.value,
                "version": self.version, "description": self.description,
                "kind": self.kind, "values": self.values,
                "suggest": self.suggest}


_REGISTRY: Dict[str, Registered] = {}


def signal(name: str, cost: Cost = Cost.FREE, version: int = 1,
           label: str = "", description: str = "",
           kind: str = "text", values: Optional[List[Any]] = None,
           suggest: Optional[Dict[str, Any]] = None):
    """Decorator registering a signal function."""
    def wrap(fn):
        if name in _REGISTRY:
            raise ValueError("signal %r registered twice" % name)
        _REGISTRY[name] = Registered(
            fn=fn, name=name, cost=cost, version=version,
            label=label or name.replace("_", " ").title(),
            description=description or (fn.__doc__ or "").strip().split("\n")[0],
            kind=kind, values=values, suggest=suggest,
        )
        return fn
    return wrap


def get(name: str) -> Registered:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError("no signal %r (have: %s)"
                       % (name, ", ".join(sorted(_REGISTRY)))) from None


def all_signals() -> List[Registered]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def by_cost(cost: Cost) -> List[Registered]:
    return [r for r in all_signals() if r.cost == cost]


def compute(name: str, business: Business, ctx: Context) -> Signal:
    """Run one signal, stamping it with the registered version.

    A signal that raises is not allowed to kill a run -- it returns UNKNOWN
    with the error as evidence. One broken signal on one odd business must
    never cost the other 15,000 results.
    """
    reg = get(name)
    try:
        result = reg.fn(business, ctx)
        # Stamping happened outside the try, so a signal returning anything
        # other than a Signal -- None being the easy mistake in a plugin --
        # raised AttributeError straight through this guard and killed the
        # entire run. The type check is what makes the promise above true for
        # signals this package did not write.
        if not isinstance(result, Signal):
            raise TypeError("returned %s, not a Signal" % type(result).__name__)
        result.name = name
        result.version = reg.version
        return result
    except Exception as exc:                      # noqa: BLE001
        return Signal(name=name, value="unknown", confidence=0.0,
                      evidence={"error": "%s: %s" % (type(exc).__name__, exc),
                                "business": business.cid},
                      version=reg.version)


# Importing the built-ins registers them.
from . import detail, web_presence, trade_match, liveness, reviews, establishment_age  # noqa: E402,F401
from . import shape  # noqa: E402,F401  -- free, derived
from . import site   # noqa: E402,F401  -- the CHEAP tier

# Everything registered by the time this module finishes importing is a signal
# kerb ships. Anything registered afterwards came from somewhere else -- a
# plugin, an application, a test -- and must not join a campaign that never
# asked for it. Without this line, installing a package that registers one
# broken signal silently added it to every existing campaign and made every
# business fail to measure.
BUILTINS = frozenset(_REGISTRY)


def is_builtin(name: str) -> bool:
    return name in BUILTINS
