"""HTTP API and the server that hosts the UI.

One process serves everything: the SPA, the REST API and the live progress
stream. A self-hosted tool that needs Redis, a queue and three containers
before it says hello does not get installed, and installing in thirty seconds
is the product.

Runs execute on worker threads. The pipeline is I/O-bound -- it spends its life
waiting on other people's servers -- so threads are the right tool and an
executor pool would add ceremony without throughput.

Progress reaches the browser over SSE rather than WebSockets: it is strictly
one-directional, EventSource reconnects for free, and a subscriber that
connects late still gets the whole history because every event is retained for
the life of the run.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import queue
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import paths, scoring, signals, sources
from .campaign import Campaign, durable_problem, validate
from .models import Outcome, Verdict
from .store import Store, default_dir
from .packs import library
from .pipeline import Pipeline, stored_signals, terminal_status
from .sources.csv_ingest import inspect as inspect_file

STATIC = Path(__file__).parent / "static"

# How many finished runs to keep, and how many events per run. Both were
# unbounded: a long-lived server accumulated every run and every event it had
# ever emitted, and a machine left running overnight grew without limit.
MAX_RUNS = 200          # runs kept on disk; older finished ones are pruned
MAX_EVENTS = 5000       # progress lines kept per run, for a late subscriber

# How often the worker flushes verdicts to disk. Batched because a synchronous
# commit per business would dominate a 200,000-row run, and small enough that a
# client polling mid-run sees results arriving rather than nothing.
FLUSH_EVERY = 50
FLUSH_SECONDS = 2.0

_STORE: Optional[Store] = None
_STORE_LOCK = threading.Lock()


def store() -> Store:
    """The ledger. One per process, opened lazily so importing this module
    does not create a database file as a side effect."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = Store()                 # honours KERB_STATE_DB itself
            # Anything still marked running belongs to a process that no longer
            # exists. Say so rather than leaving a record that lies.
            _STORE.reconcile()
            _STORE.prune(keep=MAX_RUNS)
        return _STORE


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------
# The CLI reads whatever the user's shell can read, which is correct -- it IS
# the user. The HTTP surface is different: `kerb serve --host 0.0.0.0` puts it
# on the network, and campaign source paths are attacker-controlled from there.
# Unconstrained, `{"path": "/etc/passwd"}` turned the importer into a file
# reader for anyone on the LAN.
#
# The confinement itself is switched on in create_app(), so the HTTP surface is
# confined however it is started. It used to happen only in `kerb serve`, so
# `uvicorn kerb.api:app` -- or uvicorn's --reload child -- served every file.

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def safe_path(p: str) -> Path:
    """A client-supplied path, refused unless it is inside an allowed root.

    Thin wrapper over the process-wide policy so the HTTP layer can turn a
    refusal into the right status code. The enforcement itself happens where
    the file is opened, so this is an early, friendlier check rather than the
    only thing standing in the way.
    """
    if not p or not str(p).strip():
        raise HTTPException(422, "no path given")
    try:
        return paths.check(p)
    except paths.PathNotAllowed as exc:
        text = str(exc)
        raise HTTPException(422 if text.startswith("not a usable") else 403, text)


def check_campaign_paths(campaign: Dict[str, Any]) -> None:
    """Every path a campaign names, checked before the run starts.

    Source paths were confined here from the start; suppression lists were not,
    and a campaign is a campaign -- `suppress: {lists: [/etc/passwd]}` was read
    over HTTP by anyone who could reach the port. It even reported success,
    because a file whose first line has no comma parses as a single-column list
    of ids, so the read was silent as well as unauthorised.
    """
    for spec in campaign.get("sources") or []:
        if isinstance(spec, dict):
            path = (spec.get("options") or {}).get("path")
            if path:
                safe_path(path)
    for path in (campaign.get("suppress") or {}).get("lists") or []:
        safe_path(path)


# --------------------------------------------------------------------------
# Run registry
# --------------------------------------------------------------------------

class Run:
    """One execution. Holds its own events so a late subscriber misses nothing."""

    def __init__(self, run_id: str, campaign: Campaign):
        self.id = run_id
        self.campaign = campaign
        self.status = "queued"
        self.error: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None
        # Counters, not records. The verdicts themselves live in the ledger,
        # so a restart loses progress lines and nothing else.
        self.stats: Dict[str, Any] = {}
        self.counts: Dict[str, int] = {"qualified": 0, "rejected": 0,
                                       "unevaluated": 0, "total": 0}
        # Bounded. EventSource reconnects replay the whole buffer, so this is
        # also what stops a day-long run from replaying a million lines.
        self.events: deque = deque(maxlen=MAX_EVENTS)
        self.events_dropped = 0
        self._seq = 0
        self._subscribers: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.durable = False

    def emit(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            # `i` lets a reconnecting client skip what it already rendered.
            # EventSource reconnects on its own and the server replays from the
            # start, so without an id every dropped connection duplicated the
            # entire progress log.
            event = {**event, "t": round(time.time() - self.started, 2),
                     "i": self._seq}
            if len(self.events) == MAX_EVENTS:
                self.events_dropped += 1
            self.events.append(event)
            for q in list(self._subscribers):
                q.put(event)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            for past in self.events:       # replay, so nothing is missed
                q.put(past)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.campaign.name, "status": self.status,
            "error": self.error, "started": self.started, "finished": self.finished,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "stats": self.stats,
            "qualified": self.counts["qualified"],
            "rejected": self.counts["rejected"],
            "unevaluated": self.counts["unevaluated"],
            "total": self.counts["total"],
            "events_dropped": self.events_dropped,
            "live": True,
        }


# Live runs only. Everything a client can read comes from the ledger, so this
# holds nothing a restart would miss except the in-flight progress stream.
RUNS: Dict[str, Run] = {}
_RUNS_LOCK = threading.Lock()


def stored_summary(run_id: str) -> Optional[Dict[str, Any]]:
    """A finished run, read back from disk after the process that ran it is gone."""
    row = store().get_run(run_id)
    if row is None:
        return None
    # Parsed defensively. A truncated write or a hand-edited row must cost that
    # one run its statistics, not make it unreadable -- and never take the whole
    # listing down with it, which is what an unguarded json.loads did.
    try:
        stats = json.loads(row["stats"] or "{}")
        if not isinstance(stats, dict):
            stats = {"unreadable": True}
    except (ValueError, TypeError):
        stats = {"unreadable": "stored statistics could not be parsed"}
    # The reason a run failed is part of the run, not a detail of the process
    # that happened to be executing it. Read back as `error: None`, a failed
    # run said only that it failed -- which is the shape of report this project
    # exists to eliminate.
    return {"id": row["id"], "name": row["name"], "status": row["state"],
            "error": stats.get("error"),
            "started": row["started"], "finished": row["finished"],
            "elapsed": round((row["finished"] or row["started"] or 0)
                             - (row["started"] or 0), 1),
            "stats": stats,
            "qualified": int(stats.get("qualified") or 0),
            "rejected": int(stats.get("rejected") or 0),
            "unevaluated": int(stats.get("unevaluated") or 0),
            "total": store().result_count(run_id),
            "events_dropped": 0, "live": False}


def run_data(run_id: str) -> List[Dict[str, Any]]:
    return list(store().results(run_id))


def qualified_data(run_id: str) -> List[Dict[str, Any]]:
    """A run's qualified rows, in ledger order, decoding only those.

    Rescoring and the default export want nothing else, and decoding every row
    to throw most of them away was most of their time on a large run.
    """
    db = store()
    outline = db.outline(run_id)
    if outline is None:
        return [r for r in db.results(run_id)
                if r.get("outcome") == Outcome.QUALIFIED.value]
    return db.results_for(run_id, [r["cid"] for r in outline
                                   if r["outcome"] == Outcome.QUALIFIED.value])


# Rows per chunk of a streamed export. Starlette runs each step of a plain
# generator in its threadpool, and one hop per row cost ~5s on 45,000 rows --
# more than writing the CSV did.
EXPORT_CHUNK = 500


def _default_columns() -> List[str]:
    from .cli import COLUMNS
    return COLUMNS


def _campaign_for(run_id: str) -> Optional[Campaign]:
    live = RUNS.get(run_id)
    if live:
        return live.campaign
    row = store().get_run(run_id)
    if not row:
        return None
    try:
        return Campaign.from_dict(json.loads(row["campaign"] or "{}"))
    except Exception:                              # noqa: BLE001
        return None


# Finished runs are kept only so a late subscriber can still replay the
# progress log. Bounded, because each one holds an event buffer.
MAX_FINISHED_IN_MEMORY = 20


def retire(run_id: str) -> None:
    """Drop finished runs beyond the cap, oldest first."""
    with _RUNS_LOCK:
        done = sorted((r for r in RUNS.values()
                       if r.status not in ("queued", "running")),
                      key=lambda r: r.finished or r.started)
        for run in done[:max(0, len(done) - MAX_FINISHED_IN_MEMORY)]:
            RUNS.pop(run.id, None)


def summary_for(run_id: str) -> Optional[Dict[str, Any]]:
    """Live counters while a run is executing; the ledger once it is not.

    Memory is only ahead of disk for a run still in flight. Once finished the
    ledger is authoritative -- reading a retained in-memory copy meant a
    requalify that rewrote the stored verdicts was invisible, because the
    summary still came from the object that had produced the old ones.
    """
    run = RUNS.get(run_id)
    if run is not None and run.status in ("queued", "running"):
        return run.summary()
    return stored_summary(run_id) or (run.summary() if run else None)


def _verdict_from_row(row: Dict[str, Any]) -> Verdict:
    """Rebuild a Verdict from what was stored, for rescoring and explaining.

    Everything needed is in the row -- the signals with their evidence and
    confidence -- which is why re-ranking never has to refetch. Unknown keys
    are ignored rather than passed to Business(), so a row written by a newer
    version does not break an older reader.
    """
    from .models import Business, Signal
    fields = Business.__dataclass_fields__
    business = Business(**{k: v for k, v in row.items() if k in fields})
    verdict = Verdict(business=business, score=row.get("score"),
                      breakdown=dict(row.get("breakdown") or {}))
    try:
        verdict.outcome = Outcome(row.get("outcome") or "unevaluated")
    except ValueError:
        verdict.outcome = Outcome.UNEVALUATED
    verdict.band = row.get("band")
    verdict.rejected_by = row.get("rejected_by")
    verdict.reject_reason = row.get("reject_reason")
    for name, data in (row.get("signals") or {}).items():
        verdict.add(Signal.from_dict(name, data))
    return verdict


def _explain_row(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    return scoring.explain(_verdict_from_row(row))


def _apply_hard_suppression(businesses, suppression):
    """Drop what a human already dealt with, before judging.

    On the streaming path this happens during discovery, which also saves the
    requests. Durable collection fetches a whole PLACE at a time, so the rows
    already exist by the time we can look at them -- the filtering is the same,
    the saving is not, and pretending otherwise would be the lie.
    """
    if suppression is None:
        return businesses, []
    kept, dropped = [], []
    for b in businesses:
        (dropped if suppression.hide(b.cid) else kept).append(b)
    return kept, [b.cid for b in dropped]


def _collect_durably(run: Run, db: Store, suppression=None) -> None:
    """Place-by-place collection through the ledger, then qualification.

    Split in two on purpose, exactly as the CLI does it: fetching is the part
    that fails and is worth a work queue, while judging is arithmetic over what
    was already fetched and can simply be redone.
    """
    from .models import Business
    from .sources.durable import collect_places

    campaign = run.campaign
    places = campaign.places
    source_id = next((s.get("id") if isinstance(s, dict) else s)
                     for s in campaign.sources
                     if not (isinstance(s, dict) and s.get("enabled") is False))
    opts = next((s.get("options") or {} for s in campaign.sources
                 if isinstance(s, dict)), {})

    res = collect_places(db, run.id, source_id, places, trade=campaign.trade,
                         options=opts,
                         workers=campaign.limits.workers.get("discover", 6),
                         on_progress=run.emit, should_stop=run._stop,
                         attempts=campaign.limits.attempts)

    fields = Business.__dataclass_fields__
    collected = [Business(**{k: v for k, v in row.items() if k in fields})
                 for row in db.results(run.id)]
    collected, dropped = _apply_hard_suppression(collected, suppression)
    if dropped:
        # Take them out of the ledger too, not just out of the judging.
        db.delete_results(run.id, [c for c in dropped])
    hidden = len(dropped)
    pipe = Pipeline(campaign, on_progress=run.emit, suppression=suppression)
    verdicts = list(pipe.qualify(collected))
    if suppression is not None:
        pipe.stats.suppressed += hidden
        pipe.stats.suppression = suppression.to_dict()

    # Verdicts replace the businesses they were judged from -- same cids, so
    # each row is upgraded in place and a durable run ends up indistinguishable
    # from any other in the ledger.
    for v in verdicts:
        run.counts["total"] += 1
        run.counts[v.outcome.value] = run.counts.get(v.outcome.value, 0) + 1
    db.save_results(run.id, [v.to_dict() for v in verdicts])
    run.stats = {**pipe.stats.to_dict(), **res.to_dict()}
    left = db.pending_count(run.id)
    failed = len(res.failures)
    run.stats["places_left"] = left
    run.stats["places_failed"] = failed
    if res.stopped and not run.stopping:
        # A halt (Google blocked the address) or a tripped breaker. Said as the
        # run's stop reason, so it reaches the summary rather than only the log.
        run.stats["stopped_reason"] = run.stats.get("stopped_reason") or res.stopped
    # `done` has to mean "finished, and everything worked". A run where every
    # unit failed used to report `done`, which is the same shape of dishonesty
    # as a rejection reason that blames a business for a network error.
    run.status = terminal_status(run.stats, run.stopping)
    if not run.stopping and (left or failed) and run.status == "done":
        run.status = "partial"
    if left or failed:
        run.emit({"stage": "incomplete", "places_left": left,
                  "places_failed": failed,
                  "note": "resume to continue where this stopped"})


def _release_browser() -> None:
    """Close the review browser once no run is using it.

    It is shared across runs, so it cannot close when one finishes while
    another may be mid-way through the expensive tier. Left open for the life
    of the server it was a Chrome nobody was using and nothing would stop.
    """
    from .signals import detail
    if not detail.in_use():
        return
    with _RUNS_LOCK:
        busy = any(r.status in ("queued", "running") for r in RUNS.values())
    if not busy:
        detail.close()


def _execute(run: Run) -> None:
    run.status = "running"
    db = store()
    # A campaign's suppress: block was accepted, validated, and then ignored by
    # this layer -- config that looks supported and is not, which is the exact
    # failure this project keeps removing.
    suppression = None
    cfg = run.campaign.suppress or {}
    if cfg:
        from . import suppress as suppress_mod
        try:
            suppression = suppress_mod.build(cfg, store=db)
            run.emit({"stage": "suppression", "sources": suppression.sources})
        except suppress_mod.SuppressionError as exc:
            run.status = "failed"
            run.error = str(exc)
            run.emit({"stage": "error", "error": run.error})
            run.finished = time.time()
            db.finish_run(run.id, "failed", {"error": run.error})
            run.emit({"stage": "finished", "status": "failed"})
            return
    batch: List[Dict[str, Any]] = []
    last_flush = time.time()

    def flush():
        nonlocal batch, last_flush
        if batch:
            db.save_results(run.id, batch)
            batch = []
        last_flush = time.time()
        db.update_stats(run.id, {**run.stats, **run.counts})

    try:
        if getattr(run, "durable", False):
            _collect_durably(run, db, suppression)
            return
        pipe = Pipeline(run.campaign, on_progress=run.emit,
                        suppression=suppression)
        verdicts = pipe.run()
        try:
            for verdict in verdicts:
                batch.append(verdict.to_dict())
                run.counts["total"] += 1
                run.counts[verdict.outcome.value] = \
                    run.counts.get(verdict.outcome.value, 0) + 1
                if len(batch) >= FLUSH_EVERY or (time.time() - last_flush) > FLUSH_SECONDS:
                    flush()
                if run.stopping:
                    run.emit({"stage": "stop", "reason": "stopped by user"})
                    break
        finally:
            # Closed now, not at garbage collection: closing is what tells a
            # source's worker threads to stop fetching.
            verdicts.close()
        run.stats = pipe.stats.to_dict()
        run.status = terminal_status(run.stats, run.stopping)
    except Exception as exc:                       # noqa: BLE001
        run.status = "failed"
        run.error = "%s: %s" % (type(exc).__name__, exc)
        run.stats = {**(run.stats or {}), "error": run.error}
        run.emit({"stage": "error", "error": run.error})
    finally:
        # Whatever happened, what was collected is written down before the run
        # is declared over -- including when it failed halfway.
        try:
            flush()
            db.finish_run(run.id, run.status, {**run.stats, **run.counts})
        except Exception as exc:                   # noqa: BLE001
            run.emit({"stage": "error", "error": "could not save: %s" % exc})
        run.finished = time.time()
        run.emit({"stage": "finished", "status": run.status, **run.stats})
        retire(run.id)
        _release_browser()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class CampaignBody(BaseModel):
    campaign: Dict[str, Any]
    # Durable runs put the queue on disk: each place is a unit of work that is
    # leased, retried and committed with its results. A run killed halfway
    # resumes from where it stopped instead of starting over.
    durable: bool = False


class PathBody(BaseModel):
    path: str


class RequalifyBody(BaseModel):
    campaign: Optional[Dict[str, Any]] = None      # default: the run's own
    apply: bool = False                            # default: show the diff only


class RescoreBody(BaseModel):
    weights: Dict[str, Any]
    normalise: bool = True
    confidence: bool = False


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

def allowed_hosts() -> Optional[List[str]]:
    """Host headers this server answers, or None for any.

    Bound to loopback, only loopback names -- so a web page that rebinds its
    own DNS name to 127.0.0.1 cannot drive the API from the user's browser,
    which would otherwise let any site read the results and start runs.
    Bound to the network, the machine's names are not knowable here, so any
    host is accepted unless KERB_ALLOWED_HOSTS lists them; `kerb serve` warns
    that such a server has no authentication.
    """
    extra = [h.strip().lower() for h in
             os.environ.get("KERB_ALLOWED_HOSTS", "").split(",") if h.strip()]
    bind = os.environ.get("KERB_BIND_HOST", "127.0.0.1").strip().lower()
    if bind in LOOPBACK_HOSTS:
        return list(LOOPBACK_HOSTS) + extra
    return extra or None


def _host_of(header: str) -> str:
    """`example.com:8000` -> example.com, `[::1]:8000` -> ::1."""
    header = (header or "").strip().lower()
    if header.startswith("["):
        return header[1:header.find("]")] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def create_app() -> FastAPI:
    app = FastAPI(title="Kerb", version="0.1.0",
                  description="Find and qualify local businesses. Anywhere, any trade.")

    # The HTTP surface is confined however it is started. The CLI set this up
    # before handing over to uvicorn; started any other way -- `uvicorn
    # kerb.api:app`, or uvicorn's own --reload child -- it read every file.
    if not paths.confined():
        paths.confine(paths.from_env() or [Path.cwd()])

    hosts = allowed_hosts()
    if hosts is not None:
        from fastapi.responses import PlainTextResponse

        @app.middleware("http")
        async def check_host(request, call_next):
            if _host_of(request.headers.get("host", "")) not in hosts:
                return PlainTextResponse(
                    "this Kerb server only answers requests addressed to %s"
                    % ", ".join(hosts), status_code=400)
            return await call_next(request)

    # -- capability discovery: the UI renders itself from these -------------

    @app.get("/api/signals")
    def list_signals():
        # kind/values/suggest included so the builder can render the right
        # control per signal. It used to be a hard-coded list in the UI and
        # fell ten signals behind the registry without anything noticing.
        return [r.to_dict() for r in signals.all_signals()]

    def _key_present(reg) -> bool:
        # Read from the environment named by the source, never from a request.
        if not reg.needs_key:
            return True
        return bool(reg.key_env and os.environ.get(reg.key_env))

    @app.get("/api/sources")
    def list_sources():
        return [{"id": r.id, "label": r.label, "description": r.description,
                 "needs_key": r.needs_key, "needs_browser": r.needs_browser,
                 "legal_note": r.legal_note, "takes": r.takes,
                 "cannot_measure": list(r.cannot_measure),
                 "instead": r.instead or {},
                 # Whether the key is PRESENT, never the key itself. The UI has
                 # to be able to say "set this up first" without ever holding a
                 # secret it would then persist to localStorage.
                 "key_env": r.key_env,
                 "key_present": _key_present(r),
                 "ready": (not r.needs_browser and _key_present(r))}
                for r in sources.all_sources()]

    @app.get("/api/packs")
    def list_packs(kind: Optional[str] = None):
        lib = library()
        packs = lib.by_kind(kind) if kind else lib.all()
        return [{"id": p.id, "version": p.version, "label": p.label,
                 "kind": p.id.split("/")[0], "origin": p.origin,
                 "description": p.get("description", ""),
                 "counts": {k: len(v) for k, v in p.data.items()
                            if isinstance(v, list)}}
                for p in packs]

    @app.get("/api/packs/{kind}/{name}")
    def get_pack(kind: str, name: str):
        pack = library().maybe("%s/%s" % (kind, name))
        if pack is None:
            raise HTTPException(404, "no pack %s/%s" % (kind, name))
        return {"id": pack.id, "version": pack.version, "origin": pack.origin,
                "data": pack.data}

    # -- campaigns ---------------------------------------------------------

    @app.post("/api/campaigns/validate")
    def validate_campaign(body: CampaignBody):
        problems = validate(body.campaign)
        return {"valid": not problems, "problems": problems}

    @app.post("/api/import/inspect")
    def import_inspect(body: PathBody):
        """What a file contains, before importing a single row."""
        target = safe_path(body.path)
        if not target.exists():
            raise HTTPException(404, "no file at %s" % body.path)
        try:
            return inspect_file(str(target))
        except FileNotFoundError:
            raise HTTPException(404, "no file at %s" % body.path)
        except Exception as exc:                   # noqa: BLE001
            raise HTTPException(422, "could not read that file: %s" % exc)

    # -- runs --------------------------------------------------------------

    @app.post("/api/runs", status_code=202)
    def start_run(body: CampaignBody):
        problems = validate(body.campaign)
        if problems:
            raise HTTPException(422, {"problems": problems})
        check_campaign_paths(body.campaign)
        campaign = Campaign.from_dict(body.campaign)
        if body.durable:
            why = durable_problem(campaign)
            if why:
                raise HTTPException(422, why)
        db = store()
        run_id = db.create_run(body.campaign)
        run = Run(run_id, campaign)
        run.durable = body.durable
        with _RUNS_LOCK:
            RUNS[run.id] = run
        db.prune(keep=MAX_RUNS)
        threading.Thread(target=_execute, args=(run,), daemon=True).start()
        return run.summary()

    @app.get("/api/runs")
    def list_runs(limit: int = Query(50, ge=1, le=500)):
        # From the ledger, so the list survives a restart. A live run is
        # overlaid from memory because its counters are ahead of the last flush.
        out = []
        for row in store().list_runs(limit):
            live = RUNS.get(row["id"])
            try:
                out.append(live.summary() if live else stored_summary(row["id"]))
            except Exception as exc:               # noqa: BLE001
                # One unreadable row must not empty the list. Show it as
                # damaged rather than hiding it: a run that exists and cannot
                # be read is information, and silently omitting it is not.
                out.append({"id": row["id"], "name": row["name"],
                            "status": row["state"] or "unknown",
                            "error": "could not read this run: %s" % exc,
                            "started": row["started"], "finished": row["finished"],
                            "elapsed": 0, "stats": {}, "qualified": 0,
                            "rejected": 0, "unevaluated": 0, "total": 0,
                            "events_dropped": 0, "live": False})
        return [o for o in out if o]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        found = summary_for(run_id)
        if found is None:
            raise HTTPException(404, "no run %s" % run_id)
        return found

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str):
        run = RUNS.get(run_id)
        if run is None:
            # A finished run is not an error to stop; it is already stopped.
            found = stored_summary(run_id)
            if found is None:
                raise HTTPException(404, "no run %s" % run_id)
            return {"stopping": False, **found}
        run.stop()
        return {"stopping": True, **run.summary()}

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str):
        run = RUNS.get(run_id)
        if run is None:
            # The run may be real but finished, or from before a restart --
            # either way there is no live stream, only a final state.
            found = stored_summary(run_id)
            if found is None:
                raise HTTPException(404, "no run %s" % run_id)
            done = {"stage": "finished", "status": found["status"], "i": 1,
                    "t": found["elapsed"], **(found["stats"] or {})}
            return StreamingResponse(
                iter(["data: %s\n\n" % json.dumps(done)]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        def stream():
            q = run.subscribe()
            try:
                while True:
                    try:
                        event = q.get(timeout=15)
                    except queue.Empty:
                        yield ": keepalive\n\n"     # keeps proxies from closing it
                        if run.status in ("done", "partial", "failed", "stopped", "interrupted"):
                            return
                        continue
                    yield "data: %s\n\n" % json.dumps(event)
                    if event.get("stage") == "finished":
                        return
            finally:
                run.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{run_id}/businesses")
    def run_businesses(run_id: str, qualified: Optional[bool] = None,
                       outcome: Optional[Literal["qualified", "rejected",
                                                 "unevaluated"]] = None,
                       q: Optional[str] = None,
                       sort: Literal["score", "reviews", "name"] = "score",
                       # Bounded on purpose. A negative limit used to slice as
                       # rows[0:-1] and quietly drop the last business, and a
                       # negative offset wrapped around to the end of the list
                       # -- both returning HTTP 200 with plausible wrong data,
                       # which is the failure mode this project exists to kill.
                       limit: int = Query(200, ge=0, le=10000),
                       offset: int = Query(0, ge=0)):
        if summary_for(run_id) is None:
            raise HTTPException(404, "no run %s" % run_id)
        # Straight from the ledger, so the answer is the same before and after
        # a restart, and the same whether or not this process ran the job.
        #
        # Filtered and sorted on an outline -- the six fields below, read by
        # SQLite -- and only the page returned is decoded. Decoding every row
        # to hand back 200 of them took 17s on a 100,000-row run, on the
        # request the UI makes the moment a run's results open.
        db = store()
        rows = db.outline(run_id)
        whole = rows is None
        if whole:
            rows = db.results(run_id)
        if outcome is not None:
            rows = [r for r in rows if r.get("outcome") == outcome]
        elif qualified is not None:
            # Legacy two-state filter. `qualified=false` deliberately means
            # "actually rejected" and excludes the unevaluated -- lumping them
            # in would re-create the very confusion the third state removes.
            want = Outcome.QUALIFIED.value if qualified else Outcome.REJECTED.value
            rows = [r for r in rows if r.get("outcome") == want]
        if q:
            needle = q.lower()
            rows = [r for r in rows
                    if needle in str(r.get("name") or "").lower()
                    or needle in str(r.get("category") or "").lower()
                    or needle in str(r.get("address") or "").lower()]
        if sort == "score":
            rows = sorted(rows, key=lambda r: -(r.get("score") or 0))
        elif sort == "reviews":
            rows = sorted(rows, key=lambda r: -(r.get("review_count") or 0))
        elif sort == "name":
            rows = sorted(rows, key=lambda r: str(r.get("name") or "").lower())

        page = rows[offset:offset + limit]
        if not whole:
            page = db.results_for(run_id, [r["cid"] for r in page])
        # Returned as JSONResponse directly: the rows came out of json.loads,
        # so FastAPI's jsonable_encoder pass (2.3s for 5,000 rows) could only
        # hand back the same structure. The bytes on the wire are identical.
        return JSONResponse({"total": len(rows), "offset": offset,
                             "limit": limit, "rows": page})

    # `:path` because a cid legitimately contains slashes. The overpass source
    # -- the DEFAULT source -- emits `osm:way/456`, and a plain {cid} segment
    # cannot match that in any encoding, so the evidence drawer 404'd for every
    # business found the default way.
    @app.get("/api/runs/{run_id}/businesses/{cid:path}")
    def run_business(run_id: str, cid: str):
        if summary_for(run_id) is None:
            raise HTTPException(404, "no run %s" % run_id)
        # A keyed lookup. This scanned the whole run, decoding every row, and
        # took 12s to open one business's evidence on a 100,000-row run.
        row = store().find_result(run_id, cid)
        if row is not None:
            return {**row, "explain": _explain_row(row)}
        raise HTTPException(404, "no business %s in run %s" % (cid, run_id))

    @app.post("/api/runs/{run_id}/rescore")
    def rescore(run_id: str, body: RescoreBody):
        """New weights, no refetch. Signals are already stored."""
        if summary_for(run_id) is None:
            raise HTTPException(404, "no run %s" % run_id)
        problems = [p for name, spec in body.weights.items()
                    for p in scoring.check_weight(name, spec)]
        if problems:
            raise HTTPException(422, {"problems": problems})

        # Read, rescore, write back. Still no refetching -- the signals are
        # already stored -- but the new ranking now outlives the process too.
        # The band is recomputed with the score: a row moved from 95 to 40
        # still said "call today" because only the number was rewritten.
        campaign = _campaign_for(run_id)
        bands = ((campaign.scoring if campaign else {}) or {}).get("bands")
        changed = []
        # Only qualified rows are scored, so only those are decoded.
        for row in qualified_data(run_id):
            verdict = _verdict_from_row(row)
            scoring.score(verdict, body.weights, body.normalise,
                          confidence=body.confidence)
            row["score"] = verdict.score
            row["breakdown"] = verdict.breakdown
            row["band"] = scoring.band_for(verdict.score, bands)
            changed.append(row)
        store().save_results(run_id, changed)
        return {"rescored": len(changed)}

    @app.post("/api/runs/{run_id}/requalify")
    def requalify(run_id: str, body: RequalifyBody):
        """Re-judge a stored run with today's packs and rules.

        Free, because every signal is already stored. Changes nothing unless
        `apply` is set: a rule change that silently reclassified a dataset is
        how the predecessor's hand-written migration over-deleted two good
        businesses.
        """
        if summary_for(run_id) is None:
            raise HTTPException(404, "no run %s" % run_id)
        if body.campaign is not None:
            problems = validate(body.campaign)
            if problems:
                raise HTTPException(422, {"problems": problems})
            check_campaign_paths(body.campaign)
            campaign = Campaign.from_dict(body.campaign)
        else:
            campaign = _campaign_for(run_id)
            if campaign is None:
                raise HTTPException(422, "this run has no stored campaign; "
                                         "pass one to judge against")

        stored = run_data(run_id)
        if not stored:
            raise HTTPException(422, "run %s holds no results to re-judge" % run_id)

        before = {str(r.get("cid")): str(r.get("outcome") or "unknown")
                  for r in stored}
        businesses = [_verdict_from_row(r).business for r in stored]
        # Paid signals reused as measured: re-judging answers a question about
        # rules and must not refetch every website to do it.
        verdicts = list(Pipeline(campaign, reuse=stored_signals(stored))
                        .qualify(businesses))

        changes, shifts = [], {}
        for v in verdicts:
            was = before.get(v.business.cid, "unknown")
            if was == v.outcome.value:
                continue
            key = "%s -> %s" % (was, v.outcome.value)
            shifts[key] = shifts.get(key, 0) + 1
            changes.append({"cid": v.business.cid, "name": v.business.display,
                            "was": was, "now": v.outcome.value,
                            "reason": v.reject_reason})

        if body.apply:
            store().save_results(run_id, [v.to_dict() for v in verdicts])
            counts = {"qualified": 0, "rejected": 0, "unevaluated": 0}
            for v in verdicts:
                counts[v.outcome.value] = counts.get(v.outcome.value, 0) + 1
            store().update_stats(run_id, {**(summary_for(run_id) or {}).get("stats", {}),
                                          **counts, "total": len(verdicts)})

        return {"stored": len(stored), "changed": len(changes),
                "applied": body.apply, "shifts": shifts,
                "changes": changes[:200]}

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str):
        """Continue a durable run that stopped before it finished.

        The same call as starting one: enqueueing is idempotent and finished
        units are skipped, so resuming is not a separate code path that could
        drift from the one it is meant to mirror.
        """
        with _RUNS_LOCK:
            live = RUNS.get(run_id)
            if live is not None and live.status in ("queued", "running"):
                raise HTTPException(409, "run %s is already running" % run_id)
        row = store().get_run(run_id)
        if row is None:
            raise HTTPException(404, "no run %s" % run_id)
        campaign = _campaign_for(run_id)
        if campaign is None:
            raise HTTPException(422, "run %s has no stored campaign to resume" % run_id)
        why = durable_problem(campaign)
        if why:
            raise HTTPException(422, "run %s cannot be resumed durably: %s"
                                     % (run_id, why))

        left = store().pending_count(run_id)
        run = Run(run_id, campaign)
        run.durable = True
        run.started = row["started"] or time.time()
        with _RUNS_LOCK:
            RUNS[run_id] = run
        store().set_state(run_id, "running")
        threading.Thread(target=_execute, args=(run,), daemon=True).start()
        return {**run.summary(), "resumed": True, "places_left": left}

    @app.get("/api/runs/{run_id}/tasks")
    def run_tasks(run_id: str):
        """Unit-level state for a durable run: what finished, what is queued,
        what gave up and why."""
        if summary_for(run_id) is None:
            raise HTTPException(404, "no run %s" % run_id)
        db = store()
        return {"counts": db.counts(run_id), "pending": db.pending_count(run_id),
                "failures": db.failures(run_id)}

    @app.get("/api/runs/{run_id}/export")
    def export_run(run_id: str,
                   format: Literal["csv", "json", "jsonl"] = "csv",
                   include_rejected: bool = False):
        """Results as a file, shaped by the campaign's `output:` block.

        This is where `output:` becomes meaningful over HTTP. Without it a
        campaign could specify columns, a mail-merge template, a score floor --
        and get none of them when run from the UI, because the browser was
        building its own CSV from whatever the table happened to hold.
        """
        summary = summary_for(run_id)
        if summary is None:
            raise HTTPException(404, "no run %s" % run_id)
        from .cli import (_flatten, attributions, csv_safe, render_template,
                          shape_output, template_fields)

        row_data = run_data(run_id) if include_rejected else qualified_data(run_id)
        campaign = _campaign_for(run_id)
        cfg = (campaign.output if campaign else {}) or {}
        # Best first, as the CLI writes it. The ledger returns rows by cid, and
        # shape_output only sorts when an output block exists -- so a campaign
        # without one exported its leads in id order, not score order.
        row_data = sorted(row_data, key=lambda r: -(r.get("score") or 0))
        row_data = shape_output(row_data, cfg)

        name = "kerb-%s-%s.%s" % (
            re.sub(r"[^\w.-]+", "-", str(summary.get("name") or "run")).strip("-"),
            run_id[:8], format)
        headers = {"Content-Disposition": 'attachment; filename="%s"' % name}
        # Licence notices travel with the data, in every format -- the CLI
        # wrote them and this did not, so data exported from the UI dropped
        # the attribution OpenStreetMap's licence requires.
        notes = attributions(row_data)
        if notes:
            headers["X-Data-Attribution"] = "; ".join(notes)

        if format == "json":
            return StreamingResponse(
                iter([json.dumps({"results": row_data, "attribution": notes},
                                 default=str)]),
                media_type="application/json", headers=headers)
        if format == "jsonl":
            return StreamingResponse(
                ("".join(json.dumps(r, default=str) + "\n"
                         for r in row_data[i:i + EXPORT_CHUNK])
                 for i in range(0, len(row_data), EXPORT_CHUNK)),
                media_type="application/x-ndjson", headers=headers)

        template = cfg.get("template")
        cols = list(template.keys()) if template else list(
            cfg.get("columns") or _default_columns())
        # The default layout gains an attribution column when the data needs
        # one. A template or explicit column list is the user's mail-merge
        # layout, and is left exactly as they wrote it (the header carries it).
        with_attr = bool(notes) and not template and not cfg.get("columns")
        if with_attr:
            cols = cols + ["attribution"]

        def rows():
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            for n, record in enumerate(row_data, 1):
                if n % EXPORT_CHUNK == 0:
                    yield buf.getvalue()
                    buf.seek(0), buf.truncate(0)
                if template:
                    flat = render_template(
                        record, _flatten(record, template_fields(template)), template)
                else:
                    flat = _flatten(record, cols)
                    if with_attr:
                        flat["attribution"] = (record.get("extras") or {}).get("attribution")
                writer.writerow({k: csv_safe(v) for k, v in flat.items()})
            yield buf.getvalue()

        return StreamingResponse(rows(), media_type="text/csv", headers=headers)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.1.0",
                "signals": len(signals.all_signals()),
                "sources": len(sources.all_sources()),
                "packs": len(library())}

    # -- UI ----------------------------------------------------------------

    if STATIC.is_dir():
        app.mount("/assets", StaticFiles(directory=str(STATIC)), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(str(STATIC / "index.html"))

    return app


app = create_app()
