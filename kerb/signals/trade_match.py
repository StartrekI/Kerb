"""Is this business actually in the trade that was searched for?

Google's search is generous: "roofing company" returns restaurants, gated
communities and taxi ranks. Every one of those would ship as a qualified lead
from a tool that trusts the search term.

Order matters and is the whole algorithm:
  1. VETO on category first -- a veto wins regardless of how good the name looks
  2. match on category
  3. only then fall back to the name

Name-first matching is what lets "Red Roof Building" (an apartment block) and
"ALMAROOF PERFORMANCE jetski" through. Category is Google's own classification
and is far harder to fool.
"""

from __future__ import annotations

from typing import List

from ..models import Business, Cost, Signal
from . import Context, signal

SHARED_VETOES = "trades/_shared-vetoes"


def _vetoes(ctx: Context, trade_pack) -> List[str]:
    """This pack's vetoes, plus the shared ones -- minus any shared veto that
    would exclude the trade being searched for.

    The shared list carries "cafe" so a rooftop cafe never qualifies as a
    DENTIST. Applied blindly it also rejected every result of a campaign whose
    trade IS cafes: the tool refused to find the thing it was asked to find,
    and blamed each business for it with a confident "vetoed category".

    A pack's OWN vetoes are never filtered -- those are a deliberate statement
    about that trade. Only the shared list defers.
    """
    own = list(trade_pack.list("veto_categories"))
    claims = {t.lower() for t in trade_pack.list("categories")}
    claims |= {t.lower() for t in trade_pack.list("keywords")}

    out = [v.lower() for v in own]
    shared = ctx.packs.maybe(SHARED_VETOES)
    if shared:
        for v in shared.list("veto_categories"):
            vl = v.lower()
            if any(vl in c or c in vl for c in claims if c):
                continue                  # this trade claims it; it cannot veto it
            out.append(vl)
    return out


def _terms(pack) -> List[str]:
    """Keywords plus every locale term, all lowercased."""
    terms = [t.lower() for t in pack.list("keywords")]
    for words in (pack.get("locale_terms") or {}).values():
        terms += [str(w).lower() for w in words]
    return terms


@signal(name="trade_match", cost=Cost.FREE, version=1,
        label="Trade match",
        description="Does the category or name really place it in the target trade?",
        kind="boolean",
        suggest={"op": "!=", "value": False, "default_on": True})
def trade_match(biz: Business, ctx: Context) -> Signal:
    # `packs` is the real input; `pack` is kept for campaigns written against
    # the single-trade form. A campaign listing three trade packs used to be
    # silently truncated to the first, so a vet in a dentist+medical+vet
    # campaign was rejected as "not the trade" -- with a confident reason.
    pack_ids = ctx.opt("trade_match", "packs")
    if not pack_ids:
        one = ctx.opt("trade_match", "pack")
        pack_ids = [one] if one else []
    if not pack_ids:
        return Signal("trade_match", "unknown", 0.0,
                      {"note": "no trade pack configured -- nothing to check against"})

    mode = (ctx.opt("trade_match", "match") or "any").lower()
    if mode not in ("any", "all"):
        # Refused rather than quietly treated as "any". A campaign asking for
        # something this signal does not implement must be told, not guessed at.
        return Signal("trade_match", "unknown", 0.0,
                      {"error": "what.match must be 'any' or 'all', got %r" % mode})
    if len(pack_ids) > 1:
        return _match_many(biz, ctx, pack_ids, mode)
    return _match_one(biz, ctx, pack_ids[0])


def _match_many(biz: Business, ctx: Context, pack_ids: List[str],
                mode: str) -> Signal:
    """`any`: whichever trade claims it. `all`: every one must.

    Either way a veto anywhere wins first -- a dental laboratory is not
    rescued by also being scanned against the medical pack.
    """
    results = [_match_one(biz, ctx, pid) for pid in pack_ids]
    vetoed = [r for r in results if r.value is False
              and r.evidence.get("reason") == "vetoed category"]
    if vetoed:
        return Signal("trade_match", False, 1.0,
                      dict(vetoed[0].evidence, checked=pack_ids, match=mode))

    hits = [r for r in results if r.value not in (False, "unknown")]
    if mode == "all" and len(hits) != len(pack_ids):
        missed = [pid for pid, r in zip(pack_ids, results)
                  if r.value in (False, "unknown")]
        return Signal("trade_match", False, 0.9,
                      {"reason": "match: all -- did not match every trade",
                       "matched": [r.value for r in hits], "missed": missed,
                       "category": (biz.category or "").strip() or None})
    if hits:
        best = max(hits, key=lambda r: r.confidence)
        # `all` is only as certain as its least certain component.
        conf = min(r.confidence for r in hits) if mode == "all" else best.confidence
        return Signal("trade_match", best.value, conf,
                      dict(best.evidence, checked=pack_ids, match=mode,
                           also_matched=[r.value for r in hits if r is not best]))
    return Signal("trade_match", False, 0.9,
                  {"reason": "matched none of the configured trades",
                   "category": (biz.category or "").strip() or None,
                   "checked": pack_ids, "match": mode})


def _match_one(biz: Business, ctx: Context, pack_id: str) -> Signal:
    pack = ctx.packs.maybe(pack_id)
    if pack is None:
        return Signal("trade_match", "unknown", 0.0, {"error": "no pack %r" % pack_id})

    category = (biz.category or "").strip()
    cat_l = category.lower()
    name_l = (biz.name or "").lower()

    # 1. Veto on category, before anything else.
    for veto in _vetoes(ctx, pack):
        if veto and veto in cat_l:
            return Signal("trade_match", False, 1.0,
                          {"reason": "vetoed category", "category": category,
                           "matched_veto": veto, "pack": pack.ref})

    # 2. Category match -- the trustworthy path.
    for want in pack.list("categories"):
        if want.lower() in cat_l and cat_l:
            return Signal("trade_match", pack.id.split("/")[-1], 0.95,
                          {"matched_category": category, "rule": want,
                           "pack": pack.ref})

    # 3. Name fallback, and only when the category was generic or missing.
    generic = (not cat_l) or cat_l in {"contractor", "service", "business",
                                       "general contractor", "establishment"}
    if generic:
        for term in _terms(pack):
            if term and term in name_l:
                return Signal("trade_match", pack.id.split("/")[-1], 0.6,
                              {"matched_name_term": term, "name": biz.name,
                               "category": category or None,
                               "rule": "name fallback (category was generic)",
                               "pack": pack.ref})

    return Signal("trade_match", False, 0.9,
                  {"reason": "no category or name match",
                   "category": category or None, "pack": pack.ref})
