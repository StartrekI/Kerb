"""Not showing you what you have already dealt with.

Prospecting is longitudinal. You contact people, you form judgements, and both
have to stick — otherwise every weekly run hands back the same five hundred
leads and someone filters them in a spreadsheet, which is exactly what the
predecessor's `find_new_listings.py` was doing by hand.

**There are two kinds of suppression and treating them alike is a real bug.**

  hard   a human decided something: contacted, not a fit, already a client.
         That decision is about the business and does not expire on its own.
         Applied at discovery, before anything is measured, so it also saves
         the requests.

  soft   "this appeared in last week's run". That is not a decision, it is a
         memory — and it must NOT hide a business whose verdict has since
         changed. A practice you skipped last month because it had a website,
         whose site is now dead, is the best lead in the file. Hiding it would
         be the single worst thing this module could do, so soft suppression
         is applied after judging and only when the verdict is unchanged.

Anything hidden is counted and reported. Fewer results with no explanation is
the failure this project keeps removing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

# Columns that plausibly hold a suppression date, most specific first.
DATE_FIELDS = ("suppressed_at", "contacted_at", "contacted", "date", "added_at",
               "last_contacted", "timestamp")


class SuppressionError(ValueError):
    """A suppression source that cannot be used, said out loud.

    A list we cannot read must never degrade to "suppress nothing": the run
    would look successful and quietly re-surface everyone the user has already
    called.
    """


def _parse_date(raw: Any) -> Optional[float]:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return time.mktime(time.strptime(text[:len(fmt) + 2], fmt))
        except (ValueError, OverflowError):
            continue
    try:                                    # a bare epoch
        n = float(text)
        return n if n > 1_000_000_000 else None
    except ValueError:
        return None


# What a business id actually looks like: a Google cid, an OSM reference, or a
# long digit string. Deliberately narrow.
_ID_RE = __import__("re").compile(
    r"^(0x[0-9a-f]+:0x[0-9a-f]+|osm:(node|way|relation)/\d+|\d{8,}|"
    r"[a-z0-9][a-z0-9_.:/-]{7,})$", __import__("re").I)


def _looks_like_ids(values: Iterable[Any], threshold: float = 0.8) -> bool:
    """Does this column hold identifiers, or did someone point at the wrong file?

    The single-column fallback exists so a plain list of ids works without a
    header. Unchecked it accepted anything: a notes file, a README, even
    /etc/passwd -- loading prose as "ids" that match no business, so the list
    suppressed nothing and reported success. A suppression list that silently
    does nothing is the failure this module was written to prevent.
    """
    tokens = [str(v).strip() for v in values if str(v or "").strip()]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if " " not in t and _ID_RE.match(t))
    return hits / len(tokens) >= threshold


def load_list(path: Path) -> Dict[str, Optional[float]]:
    """cid -> when it was suppressed (or None, meaning permanently).

    Accepts a CSV with a cid column, JSON/JSONL records, or a plain text file
    of one cid per line -- because a "do not contact" list is whatever the
    user's CRM exported, and demanding a format is how a safety feature stops
    being used.
    """
    from .paths import PathNotAllowed, check as _check
    try:
        path = _check(path)
    except PathNotAllowed as exc:
        raise SuppressionError(str(exc))
    if not path.exists():
        raise SuppressionError("no suppression list at %s" % path)

    from .sources.csv_ingest import (ALIASES, build_mapping, extract_cid,
                                     read_rows)

    out: Dict[str, Optional[float]] = {}
    rows = 0
    try:
        records = list(read_rows(path))
    except Exception as exc:                # noqa: BLE001
        raise SuppressionError("could not read %s: %s" % (path, exc))

    if records:
        headers = [h for h in records[0].keys() if h]
        mapping = build_mapping(headers)
        has_id_column = bool(mapping.get("cid") or mapping.get("maps_url"))

        if has_id_column:
            date_key = next((f for f in DATE_FIELDS
                             if f in {str(k).lower() for k in records[0]}), None)
            for row in records:
                rows += 1
                cid = extract_cid(row, mapping)
                if cid:
                    when = None
                    if date_key:
                        when = _parse_date(next((v for k, v in row.items()
                                                 if str(k).lower() == date_key), None))
                    out[cid] = when

        elif len(headers) == 1 and _looks_like_ids(
                [headers[0]] + [row.get(headers[0]) for row in records]):
            # One unnamed column: a plain list of ids, where the "header" the
            # csv reader consumed is really the first entry.
            #
            # This is the ONLY case that may fall back. A structured file with
            # several columns and no id column is an error, not a list of ids:
            # treating its header row as an id silently loaded `name` and
            # `phone` as businesses to hide, which suppresses nothing real and
            # reports success.
            key = headers[0]
            out[str(key).strip().lower()] = None
            for row in records:
                rows += 1
                token = str(row.get(key) or "").strip().strip('"')
                if token:
                    out[token.lower()] = None
            out.pop("", None)

    if not out:
        raise SuppressionError(
            "%s has no column holding a business id -- looked for %s. "
            "Read %d row(s) with columns: %s.\n"
            "       Refusing to continue: a suppression list that loads nothing "
            "would silently re-surface everyone on it."
            % (path, ", ".join(ALIASES["cid"][:4]), len(records),
               ", ".join(str(h) for h in (records[0].keys() if records else [])) or "none"))
    return out


@dataclass
class Suppression:
    """Everything a run should not show, and why."""
    hard: Dict[str, Optional[float]] = field(default_factory=dict)
    soft: Dict[str, str] = field(default_factory=dict)      # cid -> prior outcome
    after_seconds: Optional[float] = None
    sources: List[str] = field(default_factory=list)

    hidden_hard: int = 0
    hidden_soft: int = 0
    resurfaced: int = 0                    # soft-suppressed, but the verdict moved

    @property
    def active(self) -> bool:
        return bool(self.hard or self.soft)

    def _expired(self, when: Optional[float]) -> bool:
        """An entry old enough to be worth showing again.

        Permanent suppression is wrong for a dated entry: a business that had a
        website when you passed on it may not have one now.
        """
        if self.after_seconds is None or when is None:
            return False
        return (time.time() - when) > self.after_seconds

    def hide(self, cid: str) -> bool:
        """Hard check, at discovery -- before a single request is spent."""
        key = (cid or "").lower()
        if key not in self.hard:
            return False
        if self._expired(self.hard[key]):
            return False
        self.hidden_hard += 1
        return True

    def hide_verdict(self, cid: str, outcome: str) -> bool:
        """Soft check, after judging. Only hides an UNCHANGED verdict."""
        key = (cid or "").lower()
        prior = self.soft.get(key)
        if prior is None:
            return False
        if prior != outcome:
            self.resurfaced += 1
            return False                   # the verdict moved: this is news
        self.hidden_soft += 1
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {"sources": self.sources,
                "hard_entries": len(self.hard), "soft_entries": len(self.soft),
                "hidden_hard": self.hidden_hard, "hidden_soft": self.hidden_soft,
                "resurfaced": self.resurfaced}

    def summary(self) -> str:
        bits = []
        if self.hidden_hard:
            bits.append("%d already dealt with" % self.hidden_hard)
        if self.hidden_soft:
            bits.append("%d unchanged since a previous run" % self.hidden_soft)
        if self.resurfaced:
            bits.append("%d shown again because their verdict changed"
                        % self.resurfaced)
        return ", ".join(bits)


def build(cfg: Optional[Dict[str, Any]], store=None,
          extra_lists: Optional[Iterable[str]] = None) -> Suppression:
    """Assemble suppression from a campaign block plus any --suppress files."""
    from .campaign import parse_duration

    cfg = dict(cfg or {})
    sup = Suppression(after_seconds=parse_duration(cfg.get("after")))

    for raw in list(cfg.get("lists") or []) + list(extra_lists or []):
        entries = load_list(Path(raw))
        sup.hard.update(entries)
        sup.sources.append("%s (%d)" % (raw, len(entries)))

    for cid in cfg.get("cids") or []:
        sup.hard[str(cid).lower()] = None

    run_ids = list(cfg.get("runs") or [])
    if run_ids:
        if store is None:
            raise SuppressionError(
                "suppress.runs needs the run ledger; pass --state-db or use "
                "--durable so previous runs can be read")
        for run_id in run_ids:
            if store.get_run(run_id) is None:
                raise SuppressionError("no run %r in the ledger" % run_id)
            n = 0
            for row in store.results(run_id):
                cid = str(row.get("cid") or "").lower()
                if cid:
                    sup.soft[cid] = str(row.get("outcome") or "unknown")
                    n += 1
            sup.sources.append("run %s (%d)" % (run_id, n))
    return sup
