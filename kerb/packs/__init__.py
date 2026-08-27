"""Packs -- the community data layer.

A pack is versioned YAML holding the knowledge that makes a signal correct:
which hosts are booking platforms, which categories are impostors for a trade,
which districts are affluent. Deliberately data and not code, because the
contribution that improves this project most is one line adding a Brazilian
booking platform, and that should not require reading Python.

Two loading paths:
  - shipped packs, bundled with the install (this directory's data/)
  - user packs, in ~/.kerb/packs, which override shipped ones by id

User packs win. Someone who has curated their own dentist taxonomy for their
own market should not have it silently replaced by an update.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

SHIPPED = Path(__file__).parent / "data"


def user_dir() -> Path:
    return Path(os.environ.get("KERB_PACKS_DIR",
                               Path.home() / ".kerb" / "packs")).expanduser()


class Pack:
    """One loaded pack. Thin wrapper so callers get helpers, not raw dicts."""

    def __init__(self, data: Dict[str, Any], origin: str = "shipped"):
        self.id: str = data["id"]
        self.version: int = int(data.get("version", 1))
        self.label: str = data.get("label", self.id)
        self.data = data
        self.origin = origin

    @property
    def ref(self) -> str:
        """How a pack cites itself in evidence: `trades/dentist@3`."""
        return "%s@%d" % (self.id, self.version)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def list(self, key: str) -> List[str]:
        v = self.data.get(key) or []
        return [str(x) for x in v]

    def __repr__(self):
        return "<Pack %s (%s)>" % (self.ref, self.origin)


class PackLibrary:
    """Every pack available, shipped and user, indexed by id."""

    def __init__(self, extra_dirs: Optional[Iterable[Path]] = None):
        self._packs: Dict[str, Pack] = {}
        self._load_dir(SHIPPED, "shipped")
        udir = user_dir()
        if udir.is_dir():
            self._load_dir(udir, "user")          # user overrides shipped
        for d in (extra_dirs or []):
            self._load_dir(Path(d), "extra")

    def _load_dir(self, root: Path, origin: str) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError as exc:
                raise ValueError("pack %s is not valid YAML: %s" % (path, exc)) from exc
            if not isinstance(data, dict) or "id" not in data:
                raise ValueError("pack %s has no 'id'" % path)
            self._packs[data["id"]] = Pack(data, origin)

    # -- access ----------------------------------------------------------

    def get(self, pack_id: str) -> Pack:
        try:
            return self._packs[pack_id]
        except KeyError:
            raise KeyError("no pack %r (have: %s)"
                           % (pack_id, ", ".join(sorted(self._packs)))) from None

    def maybe(self, pack_id: str) -> Optional[Pack]:
        return self._packs.get(pack_id)

    def by_kind(self, kind: str) -> List[Pack]:
        """kind is the id prefix: trades, geo, web-presence, chains."""
        return [p for pid, p in sorted(self._packs.items())
                if pid.startswith(kind + "/")]

    def all(self) -> List[Pack]:
        return [self._packs[k] for k in sorted(self._packs)]

    def with_pack(self, data: Dict[str, Any], origin: str = "campaign") -> "PackLibrary":
        """This library plus one pack defined inline by a campaign.

        A COPY, not a mutation. The library is a process-wide singleton and the
        server runs many campaigns against it, so registering an ad-hoc trade
        into the shared object would leak one run's trade into the next.
        """
        clone = PackLibrary.__new__(PackLibrary)
        clone._packs = dict(self._packs)
        clone._packs[data["id"]] = Pack(dict(data), origin)
        return clone

    def __contains__(self, pack_id: str) -> bool:
        return pack_id in self._packs

    def __len__(self) -> int:
        return len(self._packs)


# --------------------------------------------------------------------------
# Host matching -- shared by every web-presence pack
# --------------------------------------------------------------------------

def host_matches(host: str, needles: Iterable[str]) -> Optional[str]:
    """Suffix match on a DOT BOUNDARY, returning the entry that matched.

    The boundary is the whole point: a naive `in` test makes "notfresha.com"
    match "fresha.com" and quietly disqualifies a real business. Matching on
    "." also means one entry covers every per-business subdomain, which is
    exactly how these platforms are structured (salon.booksy.com).
    """
    host = (host or "").lower().strip()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    for needle in needles:
        n = needle.lower().strip()
        if host == n or host.endswith("." + n):
            return needle
    return None


_LIBRARY: Optional[PackLibrary] = None


ADHOC_ID = "trades/_typed"


def adhoc_trade(text: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A trade pack built from a phrase somebody typed.

    The shipped packs exist because a curated category list beats a guess. But
    six of them is not the world, and an open-source tool whose usefulness stops
    at the trades its author happened to write is barely a tool. Anything typed
    here works immediately; a curated pack simply works better.

    Both category and keyword matching are substring tests, so the singular stem
    covers the plural and most compounds: "hospital" matches "Hospital",
    "Children's hospital" and "Hospital department".
    """
    extra = dict(extra or {})
    term = " ".join(str(text or "").split()).strip()
    if not term:
        raise ValueError("what.trade is empty -- name the trade to look for")
    stem = term.lower()
    if stem.endswith("ies") and len(stem) > 4:
        stem = stem[:-3] + "y"                 # bakeries -> bakery
    elif stem.endswith("es") and len(stem) > 4 and stem[-3] in "sxzh":
        stem = stem[:-2]                       # churches -> church
    elif stem.endswith("s") and not stem.endswith("ss") and len(stem) > 3:
        stem = stem[:-1]                       # hospitals -> hospital
    return {
        "id": ADHOC_ID,
        "version": 1,
        "label": term[:1].upper() + term[1:],
        "categories": [str(c) for c in (extra.get("categories") or [stem])],
        "keywords": [str(k) for k in (extra.get("keywords") or [stem])],
        "veto_categories": [str(v) for v in (extra.get("vetoes") or [])],
        "osm_tags": [str(t) for t in (extra.get("osm_tags") or [])],
        "typed": True,
    }


def library(reload: bool = False) -> PackLibrary:
    """Process-wide library. Packs are read once; call reload after editing."""
    global _LIBRARY
    if _LIBRARY is None or reload:
        _LIBRARY = PackLibrary()
    return _LIBRARY
