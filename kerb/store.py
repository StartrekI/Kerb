"""The work ledger — what makes a long collection survivable.

The durability does not belong inside the fetcher. Whether the thing on the
other end is a public API, an official endpoint or an export someone else
produced, the same four questions decide whether a multi-hour run survives:

    what is left to do        a task queue that outlives the process
    who is doing it now       a lease, so a dead worker's task comes back
    what has been collected   results written with the state change, not after
    what went wrong           attempts and errors kept, not swallowed

SQLite in WAL mode is the whole storage layer: atomic commits, crash-safe to
`kill -9`, no daemon, one file you can copy. A tool that needs Postgres running
before it can resume a scrape is a tool that loses the scrape.

**The single most important line in this file** is that a task is marked done
and its results are written *in the same transaction*. Nearly every scraper
that loses data on a crash does those as two steps: results written, then
"mark done" — die in between and the work is repeated, or marked done and the
results are gone. One transaction makes both impossible.

Results are keyed by (run, cid), so retrying a task that half-finished cannot
duplicate anything. That is what makes retries safe enough to be automatic.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id          TEXT PRIMARY KEY,
  name        TEXT,
  campaign    TEXT,
  state       TEXT NOT NULL,          -- running | done | stopped | failed
  started     REAL,
  finished    REAL,
  stats       TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id              TEXT PRIMARY KEY,   -- deterministic, so enqueue is idempotent
  run_id          TEXT NOT NULL,
  kind            TEXT NOT NULL,
  key             TEXT NOT NULL,
  payload         TEXT,
  state           TEXT NOT NULL,      -- pending|running|done|failed|deferred
  attempts        INTEGER NOT NULL DEFAULT 0,
  lease_until     REAL,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  error           TEXT,
  updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_claim
  ON tasks (run_id, state, next_attempt_at);

CREATE TABLE IF NOT EXISTS results (
  run_id  TEXT NOT NULL,
  cid     TEXT NOT NULL,
  task_id TEXT,
  data    TEXT NOT NULL,
  PRIMARY KEY (run_id, cid)           -- a retried task cannot duplicate a row
);
CREATE INDEX IF NOT EXISTS results_run ON results (run_id);
"""

PENDING, RUNNING, DONE, FAILED, DEFERRED = (
    "pending", "running", "done", "failed", "deferred")

# How long a worker may hold a task before it is considered dead. Long enough
# that a slow fetch is not stolen, short enough that a killed process does not
# strand its work for the rest of the run.
LEASE_SECONDS = 300.0
MAX_ATTEMPTS = 4
BACKOFF_BASE = 5.0


def default_dir() -> Path:
    return Path(os.environ.get("KERB_STATE_DIR",
                               Path.home() / ".kerb" / "state")).expanduser()


def default_db() -> Path:
    """The ledger every command shares unless told otherwise.

    KERB_STATE_DB used to be read by the server alone, so a server pointed at
    one ledger and `kerb reviews` / `kerb jobs` reading another could not see
    each other's runs.
    """
    override = os.environ.get("KERB_STATE_DB")
    if override:
        return Path(override).expanduser()
    return default_dir() / "kerb.db"


class Store:
    """One SQLite file. Safe to share across threads; safe to kill at any point."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False,
                                  isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        # WAL survives a hard kill and lets readers run while a worker writes.
        # synchronous=FULL because the entire promise of this file is that a
        # committed task is genuinely on disk before we act as if it is.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass

    # -- runs -------------------------------------------------------------

    def create_run(self, campaign: Dict[str, Any], run_id: Optional[str] = None) -> str:
        run_id = run_id or uuid.uuid4().hex[:12]
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO runs (id,name,campaign,state,started,stats)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, campaign.get("name", "untitled"), json.dumps(campaign),
                 "running", time.time(), "{}"))
        return run_id

    def finish_run(self, run_id: str, state: str, stats: Dict[str, Any]) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE runs SET state=?, finished=?, stats=? WHERE id=?",
                (state, time.time(), json.dumps(stats, default=str), run_id))

    def set_state(self, run_id: str, state: str) -> None:
        """Change a run's state alone. Resuming used finish_run, which stamped
        a finish time on a run that had just started again."""
        with self._lock:
            self.db.execute("UPDATE runs SET state=?, finished=NULL WHERE id=?",
                            (state, run_id))

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?",
                                  (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def unfinished_runs(self) -> List[Dict[str, Any]]:
        """Runs that were interrupted -- the ones a restart should offer to resume."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM runs WHERE state='running' ORDER BY started").fetchall()
        return [dict(r) for r in rows]

    # -- tasks ------------------------------------------------------------

    def add_tasks(self, run_id: str, kind: str, keys: List[str],
                  payload: Optional[Dict[str, Any]] = None) -> int:
        """Enqueue work. Idempotent: the same key twice is one task, so a
        resumed run can re-declare its whole plan without duplicating it."""
        now = time.time()
        body = json.dumps(payload or {})
        rows = [("%s:%s:%s" % (run_id, kind, k), run_id, kind, k, body,
                 PENDING, now) for k in keys]
        with self._lock:
            cur = self.db.executemany(
                "INSERT OR IGNORE INTO tasks"
                " (id,run_id,kind,key,payload,state,updated_at)"
                " VALUES (?,?,?,?,?,?,?)", rows)
            return cur.rowcount

    def claim(self, run_id: str, kind: Optional[str] = None,
              lease: float = LEASE_SECONDS) -> Optional[Dict[str, Any]]:
        """Take the next runnable task, atomically.

        Expired leases are reclaimed first. That reclaim is what makes a
        `kill -9` survivable: a task the dead process was holding returns to
        the queue by itself rather than sitting in `running` for ever.

        Select-then-update inside one IMMEDIATE transaction rather than
        `UPDATE ... RETURNING`: RETURNING needs SQLite 3.35, and the Python 3.9
        this package supports ships with older ones on common distributions,
        where every claim was a syntax error. The IMMEDIATE lock is what keeps
        two processes from claiming the same task.
        """
        now = time.time()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "UPDATE tasks SET state=?, lease_until=NULL"
                    " WHERE run_id=? AND state=? AND lease_until IS NOT NULL"
                    " AND lease_until < ?", (PENDING, run_id, RUNNING, now))
                sql = ("SELECT id FROM tasks WHERE run_id=? AND state IN (?,?)"
                       " AND next_attempt_at<=?" + (" AND kind=?" if kind else "") +
                       " ORDER BY next_attempt_at, updated_at LIMIT 1")
                args = [run_id, PENDING, DEFERRED, now] + ([kind] if kind else [])
                pick = self.db.execute(sql, args).fetchone()
                row = None
                if pick is not None:
                    self.db.execute(
                        "UPDATE tasks SET state=?, lease_until=?, attempts=attempts+1,"
                        " updated_at=? WHERE id=?",
                        (RUNNING, now + lease, now, pick["id"]))
                    row = self.db.execute("SELECT * FROM tasks WHERE id=?",
                                          (pick["id"],)).fetchone()
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return dict(row) if row else None

    def complete(self, task_id: str, results: Optional[List[Dict[str, Any]]] = None,
                 run_id: Optional[str] = None) -> int:
        """Mark done AND store the results in one transaction.

        Two statements, one commit. Doing these separately is how scrapers lose
        work: crash between them and the results are either orphaned or
        silently repeated. There is no window here to crash in.

        Returns how many rows were genuinely NEW. Overlapping search areas
        return the same business more than once, so rows fetched and rows
        stored are different numbers -- and reporting the larger one as the
        haul overstates a run by however much its areas overlapped.
        """
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                new = 0
                if results:
                    rows = [(run_id, r["cid"], task_id, json.dumps(r, default=str))
                            for r in results if r.get("cid")]
                    before = self.db.execute(
                        "SELECT COUNT(*) n FROM results WHERE run_id=?",
                        (run_id,)).fetchone()["n"]
                    self.db.executemany(
                        "INSERT OR REPLACE INTO results (run_id,cid,task_id,data)"
                        " VALUES (?,?,?,?)", rows)
                    after = self.db.execute(
                        "SELECT COUNT(*) n FROM results WHERE run_id=?",
                        (run_id,)).fetchone()["n"]
                    new = after - before
                self.db.execute(
                    "UPDATE tasks SET state=?, lease_until=NULL, error=NULL,"
                    " updated_at=? WHERE id=?", (DONE, time.time(), task_id))
                self.db.execute("COMMIT")
                return new
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def fail(self, task_id: str, error: str, max_attempts: int = MAX_ATTEMPTS,
             backoff: float = BACKOFF_BASE) -> str:
        """Record a failure: retry with backoff, or give up and keep the reason.

        A task that runs out of attempts is `failed`, never deleted. A run that
        finishes with failures still shows exactly which units were lost and
        why -- silently returning fewer results is the thing to avoid.
        """
        now = time.time()
        with self._lock:
            row = self.db.execute("SELECT attempts FROM tasks WHERE id=?",
                                  (task_id,)).fetchone()
            attempts = row["attempts"] if row else max_attempts
            if attempts >= max_attempts:
                self.db.execute(
                    "UPDATE tasks SET state=?, error=?, lease_until=NULL,"
                    " updated_at=? WHERE id=?", (FAILED, error[:500], now, task_id))
                return FAILED
            self.db.execute(
                "UPDATE tasks SET state=?, error=?, lease_until=NULL,"
                " next_attempt_at=?, updated_at=? WHERE id=?",
                (DEFERRED, error[:500], now + backoff * (2 ** (attempts - 1)),
                 now, task_id))
            return DEFERRED

    def save_results(self, run_id: str, rows: List[Dict[str, Any]]) -> int:
        """Store a batch of verdicts, atomically, with no task attached.

        The collector writes results as part of completing a unit of work. The
        API has no work queue -- it streams verdicts out of a pipeline -- so it
        needs the same atomic, idempotent write without the task. Keyed by
        (run, cid) exactly as before, so a re-run cannot duplicate.
        """
        if not rows:
            return 0
        payload = [(run_id, str(r["cid"]), None, json.dumps(r, default=str))
                   for r in rows if r.get("cid")]
        if not payload:
            return 0
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                before = self.db.execute(
                    "SELECT COUNT(*) n FROM results WHERE run_id=?",
                    (run_id,)).fetchone()["n"]
                self.db.executemany(
                    "INSERT OR REPLACE INTO results (run_id,cid,task_id,data)"
                    " VALUES (?,?,?,?)", payload)
                after = self.db.execute(
                    "SELECT COUNT(*) n FROM results WHERE run_id=?",
                    (run_id,)).fetchone()["n"]
                self.db.execute("COMMIT")
                return after - before
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def delete_results(self, run_id: str, cids: List[str]) -> int:
        """Remove specific rows from a run.

        Needed because durable collection stores a whole place at a time: a
        business the user has already dealt with is fetched before anything can
        look at it. Leaving the row behind would count it in the total and show
        it as a verdict-less record, which reads exactly like a business we
        failed to measure.
        """
        if not cids:
            return 0
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = self.db.executemany(
                    "DELETE FROM results WHERE run_id=? AND cid=?",
                    [(run_id, str(c)) for c in cids])
                self.db.execute("COMMIT")
                return cur.rowcount
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def update_stats(self, run_id: str, stats: Dict[str, Any]) -> None:
        with self._lock:
            self.db.execute("UPDATE runs SET stats=? WHERE id=?",
                            (json.dumps(stats, default=str), run_id))

    def reconcile(self) -> List[str]:
        """Mark runs the process died in the middle of.

        A run left as `running` by a killed server is not running -- nothing is
        executing it. Saying so on startup is the difference between a stale
        record and a lie, and the results it did collect are still there.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT id FROM runs WHERE state='running'").fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self.db.execute(
                    "UPDATE runs SET state='interrupted', finished=?"
                    " WHERE state='running'", (time.time(),))
        return ids

    def prune(self, keep: int = 200) -> int:
        """Drop the oldest finished runs, and everything they own.

        Bounded on purpose: the point of the ledger is that history survives a
        restart, but history that grows for ever eventually fills a disk.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT id FROM runs WHERE state != 'running'"
                " ORDER BY COALESCE(finished, started) DESC").fetchall()
            doomed = [r["id"] for r in rows[keep:]]
            if not doomed:
                return 0
            marks = ",".join("?" * len(doomed))
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("DELETE FROM results WHERE run_id IN (%s)" % marks, doomed)
                self.db.execute("DELETE FROM tasks   WHERE run_id IN (%s)" % marks, doomed)
                self.db.execute("DELETE FROM runs    WHERE id     IN (%s)" % marks, doomed)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return len(doomed)

    def release(self, task_id: str) -> None:
        """Hand a task back untouched -- used when stopping, so a clean shutdown
        does not burn an attempt on work it simply chose not to do."""
        with self._lock:
            self.db.execute(
                "UPDATE tasks SET state=?, lease_until=NULL,"
                " attempts=MAX(attempts-1,0), updated_at=? WHERE id=? AND state=?",
                (PENDING, time.time(), task_id, RUNNING))

    # -- reading ----------------------------------------------------------

    def counts(self, run_id: str) -> Dict[str, int]:
        with self._lock:
            rows = self.db.execute(
                "SELECT state, COUNT(*) n FROM tasks WHERE run_id=? GROUP BY state",
                (run_id,)).fetchall()
        out = {s: 0 for s in (PENDING, RUNNING, DONE, FAILED, DEFERRED)}
        out.update({r["state"]: r["n"] for r in rows})
        out["results"] = self.result_count(run_id)
        return out

    def result_count(self, run_id: str) -> int:
        with self._lock:
            return self.db.execute(
                "SELECT COUNT(*) n FROM results WHERE run_id=?",
                (run_id,)).fetchone()["n"]

    def results(self, run_id: str, limit: Optional[int] = None,
                offset: int = 0) -> List[Dict[str, Any]]:
        """Rows for a run, fetched under the lock and returned as a list.

        Deliberately NOT a generator. One sqlite3.Connection is shared by every
        thread, and it is not safe for concurrent use: a lazy cursor left open
        across a caller's loop interleaves with a worker's BEGIN/COMMIT and the
        request either errors or hangs. Streaming the rows out was a genuine
        race that showed up as an API call disconnecting mid-run.
        """
        sql = "SELECT data FROM results WHERE run_id=? ORDER BY cid"
        args: List[Any] = [run_id]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args += [limit, offset]
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def failures(self, run_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT kind,key,attempts,error FROM tasks"
                " WHERE run_id=? AND state=? ORDER BY key",
                (run_id, FAILED)).fetchall()
        return [dict(r) for r in rows]

    def pending_count(self, run_id: str) -> int:
        with self._lock:
            return self.db.execute(
                "SELECT COUNT(*) n FROM tasks WHERE run_id=? AND state IN (?,?,?)",
                (run_id, PENDING, DEFERRED, RUNNING)).fetchone()["n"]

    def reclaim_all(self, run_id: str) -> int:
        """Return every leased task to the queue.

        Called when a run is picked up again: any task still marked `running`
        belongs to a process that no longer exists.
        """
        with self._lock:
            cur = self.db.execute(
                "UPDATE tasks SET state=?, lease_until=NULL, attempts=MAX(attempts-1,0),"
                " updated_at=? WHERE run_id=? AND state=?",
                (PENDING, time.time(), run_id, RUNNING))
            return cur.rowcount
