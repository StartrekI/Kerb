"""The command line.

    kerb                      start the server and open the UI
    kerb run campaign.yaml    run headless, write results to a file
    kerb estimate campaign.yaml   what it would do, without doing it
    kerb doctor               is this install healthy
    kerb stop                 kill everything kerb started

`stop` exists because of a real incident, not for symmetry. Killing a driver
process does not kill the browsers it launched: they lose their parent, keep
their memory, and never exit. A previous run of the ancestor of this tool left
enough orphans to consume 88GB and take a machine down. Anything kerb starts is
therefore tagged so it can always be found again, and `stop` verifies the kill
rather than assuming it.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__

# Every process kerb spawns carries this tag in its command line so `stop` and
# `doctor` can find it later without guessing at process names.
TAG = "KERB_MANAGED=1"

# --------------------------------------------------------------------------
# Terminal
# --------------------------------------------------------------------------

def _colour_ok() -> bool:
    return (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") != "dumb")


class C:
    on = _colour_ok()

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return "\033[%sm%s\033[0m" % (code, s) if cls.on else s

    dim    = classmethod(lambda c, s: c._w("2", s))
    bold   = classmethod(lambda c, s: c._w("1", s))
    good   = classmethod(lambda c, s: c._w("32", s))
    bad    = classmethod(lambda c, s: c._w("31", s))
    warn   = classmethod(lambda c, s: c._w("33", s))
    accent = classmethod(lambda c, s: c._w("35", s))


def say(*parts) -> None:
    print(*parts, file=sys.stderr)


def out(*parts) -> None:
    """Data a person or a script will consume, on stdout.

    `say` is progress and goes to stderr so it never contaminates piped output;
    a listing IS the output, so `kerb jobs | awk ...` has to work.
    """
    print(*parts)


def die(msg: str, code: int = 1):
    say(C.bad("error:"), msg)
    raise SystemExit(code)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

# `outcome` is a real column, not a nicety. Exporting only `qualified` collapses
# the three states back to two the moment results reach a file, so a business we
# never managed to measure would arrive in the user's spreadsheet as False --
# indistinguishable from one we measured and rejected.
COLUMNS = ["score", "name", "category", "address", "phone", "website",
           "booking_url", "rating", "review_count", "web_presence",
           "trade_match", "outcome", "qualified", "reject_reason",
           "failed_signals", "cid", "source"]


def _flatten(v: Dict[str, Any], columns: Optional[List[str]] = None) -> Dict[str, Any]:
    """One verdict as a flat row.

    Every signal is addressable by name, not just the two that used to be
    hard-coded -- otherwise `columns: [chain_size]` produced a column of blanks
    rather than an error, which is the worst of both.
    """
    cols = columns or COLUMNS
    sig = v.get("signals") or {}
    row: Dict[str, Any] = {k: v.get(k) for k in cols if k in v}
    for name, data in sig.items():
        row.setdefault(name, (data or {}).get("value"))
    row["web_presence"] = (sig.get("web_presence") or {}).get("value")
    row["trade_match"] = (sig.get("trade_match") or {}).get("value")
    row["failed_signals"] = ",".join(v.get("failed_signals") or [])
    return {k: row.get(k, v.get(k)) for k in cols}


# A phone number or a signed number: starts with + or - and holds nothing a
# spreadsheet could run. Everything else starting with a formula character is
# neutralised -- see csv_safe.
_SIGNED_NUMBER = re.compile(r"^[+-][\d\s().\-/]*$")


def csv_safe(value: Any) -> Any:
    """A cell a spreadsheet will show rather than run.

    Business names are scraped, so they are written by strangers, and a CSV
    cell starting `=`, `+`, `-` or `@` is executed as a formula by Excel,
    Sheets and LibreOffice -- `=HYPERLINK(...)` as a business name is enough.
    Those cells get a leading apostrophe, the conventional "this is text"
    marker. Phone numbers (`+44 20 ...`) and signed numbers are left alone:
    they cannot execute, and prefixing every phone in the export would damage
    the column people export for.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] in "=@\t\r" or (value[0] in "+-" and not _SIGNED_NUMBER.match(value)):
        return "'" + value
    return value


def attributions(verdicts: List[Dict[str, Any]]) -> List[str]:
    """Licence notices the data carries. Emitted automatically, not opt-in --
    a user should not have to remember someone else's licence terms."""
    seen = []
    for v in verdicts:
        a = (v.get("extras") or {}).get("attribution")
        if a and a not in seen:
            seen.append(a)
    return seen


def shape_output(rows: List[Dict[str, Any]],
                 cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Apply the campaign's `output:` block.

    That block was parsed and then ignored entirely, so anything written in it
    was silently discarded -- config that looks supported and is not is worse
    than config that does not exist.
    """
    if not cfg:
        return rows
    out = rows
    floor = cfg.get("min_score")
    if floor is not None:
        out = [r for r in out if (r.get("score") or 0) >= float(floor)]
    if cfg.get("sort", "score") == "score":
        out = sorted(out, key=lambda r: -(r.get("score") or 0))
    top = cfg.get("top")
    if top:
        out = out[:int(top)]
    return out


_TEMPLATE_FIELD = re.compile(r"\{([a-z_][a-z0-9_.]*)\}", re.I)


def template_fields(template: Dict[str, Any]) -> List[str]:
    """Every field a template refers to, so they can be checked up front."""
    names: List[str] = []
    for spec in (template or {}).values():
        for match in _TEMPLATE_FIELD.finditer(str(spec)):
            names.append(match.group(1))
        if not _TEMPLATE_FIELD.search(str(spec)):
            names.append(str(spec))            # a bare field name
    return names


def render_template(row: Dict[str, Any], flat: Dict[str, Any],
                    template: Dict[str, Any]) -> Dict[str, Any]:
    """Build the columns an outreach tool wants, named the way it wants them.

    A bare value is a field name; anything with `{braces}` is a sentence built
    from fields. A missing value renders empty rather than "None", because this
    output goes straight into a mail merge and "Dear None" is unforgivable.
    """
    def lookup(name: str) -> str:
        for source in (flat, row):
            if name in source and source[name] is not None:
                return str(source[name])
        sig = (row.get("signals") or {}).get(name) or {}
        value = sig.get("value")
        return "" if value is None else str(value)

    out: Dict[str, Any] = {}
    for column, spec in (template or {}).items():
        text = str(spec)
        if _TEMPLATE_FIELD.search(text):
            out[column] = _TEMPLATE_FIELD.sub(lambda m: lookup(m.group(1)), text)
        else:
            out[column] = lookup(text)
    return out


def split_key(row: Dict[str, Any], field: str) -> str:
    """A filename-safe grouping value. Prospecting work gets handed out by
    territory, so splitting the export is a chore worth removing."""
    raw = str(row.get(field) or (row.get("extras") or {}).get(field) or "unsorted")
    return re.sub(r"[^\w.-]+", "-", raw).strip("-").lower() or "unsorted"


def write_results(path: Path, verdicts: List[Dict[str, Any]], fmt: str,
                  output: Optional[Dict[str, Any]] = None) -> List[Path]:
    """Write the results. Callers shape first, so the count they report is the
    count on disk -- reporting "7 rows" beside a 4-row file is exactly the kind
    of small dishonesty this project keeps removing.

    Returns every file written -- `split_by` produces one per group.
    """
    cfg = output or {}
    if cfg.get("split_by"):
        field = cfg["split_by"]
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in verdicts:
            groups.setdefault(split_key(row, field), []).append(row)
        written = []
        for key, rows in sorted(groups.items()):
            target = path.with_name("%s-%s%s" % (path.stem, key, path.suffix))
            _write_one(target, rows, fmt, cfg)
            written.append(target)
        return written

    _write_one(path, verdicts, fmt, cfg)
    return [path]


def _write_one(path: Path, verdicts: List[Dict[str, Any]], fmt: str,
               cfg: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        payload = {"results": verdicts, "attribution": attributions(verdicts)}
        path.write_text(json.dumps(payload, indent=2, default=str))
    elif fmt == "jsonl":
        with path.open("w") as fh:
            for v in verdicts:
                fh.write(json.dumps(v, default=str) + "\n")
    else:
        # utf-8 explicitly: on a machine with a non-UTF-8 locale the default
        # encoding silently mangles every accented business name.
        template = cfg.get("template")
        if template:
            # A template names its own columns, so `columns` would be a second
            # answer to the same question.
            cols = list(template.keys())
            rows = [render_template(v, _flatten(v, template_fields(template)), template)
                    for v in verdicts]
        else:
            cols = [c for c in (cfg.get("columns") or COLUMNS)]
            rows = [_flatten(v, cols) for v in verdicts]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow({k: csv_safe(v) for k, v in row.items()})

    # A licence notice belongs beside every format, not only the two that
    # happened to have somewhere convenient to put it. All notices, not just
    # the last -- the loop used to overwrite the file on each pass.
    notes = attributions(verdicts)
    if notes and fmt != "json":
        path.with_suffix(path.suffix + ".ATTRIBUTION.txt").write_text(
            "\n".join(notes) + "\n")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_serve(args) -> int:
    import uvicorn
    from . import paths
    os.environ[TAG.split("=")[0]] = "1"
    # Which address the app is on decides which Host headers it answers; see
    # api.allowed_hosts. An env var so it also reaches uvicorn's --reload child.
    os.environ["KERB_BIND_HOST"] = args.host
    # The server confines reads; the CLI never does. `kerb run` is the user's
    # own shell and may read whatever they can.
    roots = paths.confine(paths.from_env() or [Path.cwd()])
    url = "http://%s:%d" % (args.host, args.port)
    say(C.bold("kerb"), C.dim("v" + __version__))
    say("  UI      ", C.accent(url))
    say("  API docs", C.dim(url + "/docs"))

    # Binding beyond loopback hands the campaign builder -- which names files
    # to read -- to everyone on the network. Reads are confined to the working
    # directory, but the user should know the surface exists at all.
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        say(C.warn("  reachable from the network on %s" % args.host))
        say(C.dim("  file reads are confined to: %s"
                  % ", ".join(str(r) for r in roots)))
        say(C.dim("  there is no authentication -- do not expose this to the internet"))
    say(C.dim("  ctrl-c to stop"))
    if not args.no_browser:
        # Delay so the browser does not race the first bind.
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("kerb.api:app", host=args.host, port=args.port,
                log_level=args.log_level, reload=args.reload)
    return 0


def _load(path: str):
    from .campaign import Campaign, validate
    import yaml
    p = Path(path).expanduser()
    if not p.exists():
        die("no campaign file at %s" % p)
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        # A campaign is hand-edited YAML; a mistake in it is the user's to fix,
        # and a traceback is not how to tell them where it is.
        mark = getattr(exc, "problem_mark", None)
        where = (" (line %d, column %d)" % (mark.line + 1, mark.column + 1)
                 if mark is not None else "")
        die("%s is not valid YAML%s: %s.\n       Quote values that start with "
            "a symbol, e.g. op: \">=\"." % (p, where,
                                             getattr(exc, "problem", None) or exc))
    problems = validate(raw)
    if problems:
        say(C.bad("this campaign cannot run:"))
        for prob in problems:
            say("  -", prob)
        raise SystemExit(2)
    return Campaign.from_dict(raw)


def load_checkpoint(path: Path) -> List[Dict[str, Any]]:
    """Verdicts already written by an earlier attempt.

    Tolerant of a truncated final line: the interesting crash is the one that
    happened mid-write, and refusing to resume because the last record is half
    a line would defeat the entire point.
    """
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue                    # a partial write; the rest is good
            if isinstance(row, dict) and row.get("cid"):
                rows.append(row)
    # A business measured again on a later attempt appears twice; the later
    # line is the current verdict.
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        latest[str(row["cid"]).lower()] = row
    return list(latest.values())


def cmd_run(args) -> int:
    from .models import Outcome
    from .pipeline import Pipeline

    campaign = _load(args.campaign)
    summary = (campaign.description or "").strip().split("\n")[0]
    say(C.bold(campaign.name), C.dim("- " + summary) if summary else "")

    # Suppression is assembled before any work starts: a list that cannot be
    # read must stop the run, never silently degrade to hiding nothing.
    suppression = None
    sup_cfg = dict(getattr(campaign, "suppress", None) or {})
    if getattr(args, "since", None):
        sup_cfg.setdefault("runs", []).append(args.since)
    if sup_cfg or getattr(args, "suppress", None):
        from . import suppress as suppress_mod
        store_for_sup = None
        if sup_cfg.get("runs"):
            from .store import Store
            store_for_sup = Store(Path(args.state_db) if getattr(args, "state_db", None)
                                  else None)
        try:
            suppression = suppress_mod.build(sup_cfg, store=store_for_sup,
                                             extra_lists=getattr(args, "suppress", None))
        except suppress_mod.SuppressionError as exc:
            die(str(exc))
        if suppression.sources:
            say(C.dim("  suppressing from: %s" % "; ".join(suppression.sources)))

    # A journal, written as results arrive and flushed every time. Without it
    # a run that dies at 90% loses the whole day; with it the next attempt
    # starts where this one stopped.
    done: List[Dict[str, Any]] = []
    journal = None
    if args.checkpoint:
        ckpt = Path(args.checkpoint).expanduser()
        # Fail here, before any work, with a sentence rather than a traceback.
        # Discovering the journal is unwritable an hour into a run -- which is
        # when the old code raised -- defeats the point of having one.
        try:
            ckpt.parent.mkdir(parents=True, exist_ok=True)
            done = load_checkpoint(ckpt)
            journal = ckpt.open("a", encoding="utf-8")
        except OSError as exc:
            die("cannot write the checkpoint at %s (%s).\n"
                "       Pick a writable path, or drop --checkpoint to run "
                "without one." % (ckpt, exc.strerror or exc))
        if done:
            say(C.dim("  resuming: %d already done in %s" % (len(done), ckpt)))

    # SIGTERM must behave like ctrl-c: stop, then write what we have. Killed
    # without this, a run discards everything it had collected.
    def _term(_sig, _frm):
        raise KeyboardInterrupt
    for sig_name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig_name):
            try:
                signal.signal(getattr(signal, sig_name), _term)
            except (ValueError, OSError):
                pass                        # not the main thread; fine

    last = [0.0]

    def progress(e):
        stage = e.get("stage")
        if stage == "qualified" and not args.quiet:
            say("  %s %-40s %s" % (C.good("+"), e["name"][:40],
                                   C.dim(str(e.get("score", "")))))
        elif stage == "discover" and e.get("found") and time.time() - last[0] > 1:
            last[0] = time.time()
            say(C.dim("  ... %d found" % e["found"]))
        elif stage == "stop":
            say(C.warn("  stopped: %s" % e.get("reason")))
        elif stage in ("error", "source_error"):
            say(C.bad("  %s" % e.get("error")))
        elif stage == "halt":
            say(C.bad("  halted: %s" % e.get("reason")))

    if getattr(args, "durable", False):
        return _run_durable(args, campaign, progress, suppression)

    # Resume skips what was DECIDED. A business we never managed to measure
    # is not done -- the breaker stopped the run precisely so those could be
    # measured next time -- and skipping it made "unevaluated" permanent.
    pipe = Pipeline(campaign, on_progress=progress,
                    skip=[r["cid"] for r in done
                          if r.get("outcome") != Outcome.UNEVALUATED.value],
                    suppression=suppression)
    verdicts = []
    interrupted = False
    try:
        for v in pipe.run():
            verdicts.append(v)
            if journal is not None:
                try:
                    journal.write(json.dumps(v.to_dict(), default=str) + "\n")
                    journal.flush()         # survive a kill -9, not just a clean exit
                    os.fsync(journal.fileno())
                except OSError as exc:
                    # The journal is insurance. If the disk fills, losing the
                    # insurance is bad; losing the run it was insuring because
                    # the insurance failed is absurd -- which is what an
                    # unguarded write did, taking the output file with it.
                    say(C.warn("\n  checkpoint stopped: %s" % (exc.strerror or exc)))
                    say(C.dim("  the run continues; results will still be written"))
                    try:
                        journal.close()
                    except OSError:
                        pass
                    journal = None
    except KeyboardInterrupt:
        interrupted = True
        say(C.warn("\n  interrupted -- writing what was collected"))
    finally:
        if journal is not None:
            journal.close()

    # Earlier verdicts, overlaid with this attempt's -- a re-measured business
    # replaces its old unevaluated row rather than appearing twice.
    merged = {str(r["cid"]).lower(): r for r in done}
    for v in verdicts:
        merged[v.business.cid] = v.to_dict()
    rows_all = list(merged.values())
    if args.include_rejected:
        rows = rows_all
    else:
        rows = sorted([r for r in rows_all if r.get("qualified")],
                      key=lambda r: -(r.get("score") or 0))

    st = pipe.stats
    say("")
    say(C.bold("  %d found, %d unique, %s qualified" % (
        st.discovered, st.deduped, C.good(str(st.qualified)))))
    if st.rejected_by:
        for name, n in list(st.rejected_by.items())[:6]:
            say(C.dim("    %-16s rejected %d" % (name, n)))

    # The breaker stops the run rather than letting an outage write itself into
    # the dataset as thousands of rejections. Say so at the top of the voice.
    if st.unevaluated:
        say("")
        say(C.warn("  %d business(es) could not be measured -- NOT rejected, "
                   "just unknown." % st.unevaluated))
        if args.checkpoint:
            say(C.dim("    Re-run with the same --checkpoint when the source is "
                      "healthy; only these are measured again."))
        else:
            say(C.dim("    Re-run when the source is healthy. With --checkpoint FILE, "
                      "a re-run measures only what is left."))
    if pipe.breaker.tripped:
        say("")
        for line in textwrap.wrap(st.stopped_reason or "", 74):
            say(C.bad("  " + line))
    if st.discovery_health and st.discovery_health.get("verdict") not in (
            "healthy", "unknown", None):
        say("")
        say(C.warn("  discovery looks %s -- %.0f%% of expected yield"
                   % (st.discovery_health["verdict"],
                      st.discovery_health.get("yield_pct") or 0)))
        if pipe.health.advice():
            say(C.dim("    " + pipe.health.advice()))

    if suppression is not None and (suppression.hidden_hard or
                                    suppression.hidden_soft or
                                    suppression.resurfaced):
        say(C.dim("    hidden: %s" % suppression.summary()))

    # Loud, because "5 qualified" means something different when a source was
    # down. A quiet partial result is indistinguishable from a complete one.
    if st.source_errors:
        say("")
        say(C.bad("  incomplete -- %d source(s) failed:" % len(st.source_errors)))
        for sid, why in st.source_errors.items():
            say(C.bad("    %-12s %s" % (sid, why)))
    if st.skipped_places:
        say(C.warn("  %d place(s) could not be read:" % len(st.skipped_places)))
        for where, why in list(st.skipped_places.items())[:5]:
            say(C.dim("    %-28s %s" % (where, why)))
        if len(st.skipped_places) > 5:
            say(C.dim("    +%d more" % (len(st.skipped_places) - 5)))
    if st.empty_places:
        say(C.dim("  %d place(s) searched with nothing found:" % len(st.empty_places)))
        for where, why in list(st.empty_places.items())[:5]:
            say(C.dim("    %-28s %s" % (where, why)))
        if len(st.empty_places) > 5:
            say(C.dim("    +%d more" % (len(st.empty_places) - 5)))
    for note in st.notes:
        say(C.warn("  note: %s" % note))
    if st.elapsed >= 1:
        say(C.dim("  %.1fs" % st.elapsed))

    if args.out:
        out = Path(args.out).expanduser()
        fmt = args.format or (out.suffix.lstrip(".") if out.suffix.lstrip(".")
                              in ("csv", "json", "jsonl") else "csv")
        rows = shape_output(rows, campaign.output)
        written = write_results(out, rows, fmt, campaign.output)
        for f in written:
            say("  ->", C.accent(str(f)),
                C.dim("(%s)" % fmt) if len(written) > 1 else
                C.dim("(%d rows, %s)" % (len(rows), fmt)))
    else:
        json.dump(rows, sys.stdout, indent=2, default=str)
        print()

    for note in attributions(rows):
        say(C.dim("  " + note))

    # Distinct exit codes, because a script needs to tell "nothing matched"
    # from "we were cut off and the dataset is incomplete".
    if pipe.breaker.tripped:
        return 4
    if interrupted:
        return 130
    return 0 if st.qualified else 3


def cmd_estimate(args) -> int:
    """What the campaign would do, without doing it.

    Deliberately reports what is knowable and refuses to invent the rest. A
    made-up number is worse than no number: someone will plan around it.
    """
    from . import signals as sig
    from .models import Cost

    campaign = _load(args.campaign)
    places = campaign.places
    say(C.bold(campaign.name))
    say("")
    say("  sources   ", ", ".join(
        s.get("id") if isinstance(s, dict) else s for s in campaign.sources))
    say("  trade     ", campaign.trade or C.dim("none"))
    say("  places    ", ("%d" % len(places)) if places else C.dim("n/a for this source"))
    if places[:4]:
        for p in places[:4]:
            say(C.dim("              " + p))
        if len(places) > 4:
            say(C.dim("              +%d more" % (len(places) - 4)))
    say("")
    for cost in (Cost.FREE, Cost.CHEAP, Cost.EXPENSIVE):
        names = campaign.signals_for(cost)
        if names:
            say("  %-10s %s" % (cost.value, ", ".join(names)))
    say("")
    say("  filters   ", "%d" % len(campaign.filters))
    for f in campaign.filters:
        if "group" in f:
            say(C.dim("              any of %d conditions" % len(f.get("of") or [])))
        else:
            say(C.dim("              %s %s %r" % (f.get("signal"), f.get("op", "=="),
                                                  f.get("value"))))
    lim = campaign.limits
    say("")
    say("  budget    ", ", ".join(filter(None, [
        "%d results" % lim.max_results if lim.max_results else "",
        "%d requests" % lim.max_requests if lim.max_requests else "",
        "%ds runtime" % lim.max_runtime_seconds if lim.max_runtime_seconds else "",
    ])) or C.dim("unbounded"))

    cheap = campaign.signals_for(Cost.CHEAP) + campaign.signals_for(Cost.EXPENSIVE)
    say("")
    if cheap:
        say(C.dim("  Per-business request count depends on how many survive the free"))
        say(C.dim("  filters, which is not knowable before the run. On past runs 74%"))
        say(C.dim("  were rejected before anything paid ran."))
    else:
        say(C.good("  No SIGNAL in this campaign costs a request."))

    # Signals being free does not make the run free: the source still fetches.
    # Saying "no requests" for an overpass campaign that will make two calls per
    # place was simply wrong, in the one command whose job is to predict cost.
    from . import sources as src
    src_ids = [s.get("id") if isinstance(s, dict) else s for s in campaign.sources]
    # Asked of the source, not a list of ids: there has never been a `gosom`
    # source, and a new file source would have been costed as networked.
    networked = [s for s in src_ids if src.get(s).takes != "path"]
    if networked:
        if "overpass" in networked and places:
            detail = ("about %d requests -- %d places, each geocoded then queried"
                      % (len(places) * 2, len(places)))
        else:
            detail = "an unknown number of requests"
        say(C.dim("  The SOURCE still fetches: %s makes %s."
                  % (", ".join(networked), detail)))
    elif not cheap:
        say(C.good("  Nothing in this campaign touches the network at all."))
    return 0


def _run_durable(args, campaign, progress, suppression=None) -> int:
    """Place discovery through the ledger, then qualify what it collected.

    Split in two on purpose: fetching is the part that fails and is worth
    making durable, while qualifying is pure arithmetic over what was already
    collected and can simply be redone.
    """
    from .pipeline import Pipeline
    from .sources.durable import collect_places
    from .store import Store

    from .campaign import durable_problem
    places = campaign.places
    why = durable_problem(campaign)
    if why:
        die("--durable: " + why)
    source_id = next((s.get("id") if isinstance(s, dict) else s)
                     for s in campaign.sources
                     if not (isinstance(s, dict) and s.get("enabled") is False))

    store = Store(Path(args.state_db) if args.state_db else None)
    try:
        run_id = args.resume or store.create_run(campaign.to_dict())
        if args.resume and not store.get_run(run_id):
            die("no run %s in the ledger -- `kerb jobs` lists them" % run_id)
        say(C.dim("  ledger %s  run %s" % (store.path, run_id)))

        opts = next((s.get("options") or {} for s in campaign.sources
                     if isinstance(s, dict)), {})
        res = collect_places(store, run_id, source_id, places,
                             trade=campaign.trade, options=opts,
                             workers=campaign.limits.workers.get("discover", 6),
                             on_progress=progress,
                             attempts=campaign.limits.attempts)

        say("")
        say(C.bold("  %d/%d places, %s new businesses"
                   % (res.units_done, len(places), C.good(str(res.collected)))))
        if res.fetched != res.collected:
            say(C.dim("    %d duplicate(s) across overlapping areas" %
                      (res.fetched - res.collected)))
        if res.failures:
            say(C.bad("    %d place(s) gave up:" % len(res.failures)))
            for f in res.failures[:5]:
                say(C.dim("      %-24s %s" % (f["key"][:24], (f["error"] or "")[:52])))
        if res.stopped:
            for line in textwrap.wrap(res.stopped, 74):
                say(C.bad("  " + line))
        left = store.pending_count(run_id)
        if left:
            say(C.warn("  %d place(s) still queued -- resume with:" % left))
            say(C.dim("    kerb run %s --durable --resume %s" % (args.campaign, run_id)))

        # Qualify everything the ledger holds, however many attempts it took.
        from .models import Business
        collected = [Business(**{k: v for k, v in r.items()
                                 if k in Business.__dataclass_fields__})
                     for r in store.results(run_id)]
        if suppression is not None:
            keep, drop = [], []
            for b in collected:
                (drop if suppression.hide(b.cid) else keep).append(b)
            collected = keep
            if drop:
                # Out of the ledger as well as out of the results: a leftover
                # row would count in the total and read as unevaluated.
                store.delete_results(run_id, [b.cid for b in drop])
                say(C.dim("    hidden: %s" % suppression.summary()))
        pipe = Pipeline(campaign, on_progress=lambda e: None,
                        suppression=suppression)
        verdicts = list(pipe.qualify(collected))

        # Write the verdicts back over the businesses they were judged from.
        # Same cids, so this upgrades each row in place rather than adding one.
        # Without it a durable run left the ledger holding businesses with no
        # outcome and no score -- second-class rows that `export`, `requalify`
        # and the qualified count could not use.
        counts = {"qualified": 0, "rejected": 0, "unevaluated": 0}
        for v in verdicts:
            counts[v.outcome.value] = counts.get(v.outcome.value, 0) + 1
        store.save_results(run_id, [v.to_dict() for v in verdicts])
        # One stats record, written at the end. update_stats() here was
        # immediately overwritten by finish_run(..., res.to_dict()) below, so
        # the ledger lost the qualified/rejected counts it had just saved.
        run_stats = {**res.to_dict(), **counts, "total": len(verdicts)}

        rows = [v.to_dict() for v in verdicts]
        if not args.include_rejected:
            rows = sorted([r for r in rows if r.get("qualified")],
                          key=lambda r: -(r.get("score") or 0))

        st = pipe.stats
        say("")
        say(C.bold("  %d collected, %s qualified"
                   % (len(collected), C.good(str(st.qualified)))))
        for name, n in list(st.rejected_by.items())[:6]:
            say(C.dim("    %-16s rejected %d" % (name, n)))
        if st.unevaluated:
            say(C.warn("    %d could not be measured -- NOT rejected" % st.unevaluated))

        store.finish_run(run_id, "done" if not left else "running", run_stats)
        if args.out:
            out = Path(args.out).expanduser()
            fmt = args.format or (out.suffix.lstrip(".") if out.suffix.lstrip(".")
                                  in ("csv", "json", "jsonl") else "csv")
            rows = shape_output(rows, campaign.output)
            written = write_results(out, rows, fmt, campaign.output)
            for f in written:
                say("  ->", C.accent(str(f)), C.dim("(%d rows, %s)" % (len(rows), fmt)))
        else:
            json.dump(rows, sys.stdout, indent=2, default=str)
            print()
        return 4 if res.stopped else (0 if st.qualified else 3)
    finally:
        store.close()


def cmd_requalify(args) -> int:
    """Re-judge a stored run without re-collecting anything.

    A taxonomy fix used to require a data migration -- the predecessor has a
    `remove_wrong_trade_roofing.py` that deleted rows by hand, and over-deleted
    two good businesses doing it. Every signal is already stored, so this is
    free and, by default, changes nothing until you have seen what it would do.
    """
    import yaml
    from .campaign import Campaign, validate
    from .models import Business
    from .pipeline import Pipeline, stored_signals
    from .store import Store

    store = Store(Path(args.state_db) if args.state_db else None)
    try:
        run = store.get_run(args.run_id)
        if run is None:
            die("no run %s in the ledger -- `kerb jobs` lists them" % args.run_id)

        if args.campaign:
            campaign = _load(args.campaign)
        else:
            raw = json.loads(run["campaign"] or "{}")
            problems = validate(raw)
            if problems:
                say(C.bad("the run's stored campaign is no longer valid:"))
                for prob in problems:
                    say("  -", prob)
                say(C.dim("  pass -c <campaign.yaml> to judge with different rules"))
                return 2
            campaign = Campaign.from_dict(raw)

        stored = list(store.results(args.run_id))
        if not stored:
            die("run %s holds no results to re-judge" % args.run_id)

        before = {str(r.get("cid")): str(r.get("outcome") or "unknown") for r in stored}
        fields = Business.__dataclass_fields__
        businesses = [Business(**{k: v for k, v in r.items() if k in fields})
                      for r in stored]

        # FREE signals are recomputed -- that is where a changed pack or rule
        # takes effect. Paid ones are reused as stored, so this really is free:
        # it recomputed everything, refetching every website it had checked.
        pipe = Pipeline(campaign, reuse=stored_signals(stored))
        verdicts = list(pipe.qualify(businesses))

        moved = [(v, before.get(v.business.cid, "unknown")) for v in verdicts
                 if before.get(v.business.cid, "unknown") != v.outcome.value]
        say(C.bold("%s  %s" % (run["id"], run["name"] or "")))
        say("  %d stored, %d would change" % (len(stored), len(moved)))
        shifts: Dict[str, int] = {}
        for v, was in moved:
            shifts["%s -> %s" % (was, v.outcome.value)] = \
                shifts.get("%s -> %s" % (was, v.outcome.value), 0) + 1
        for shift, n in sorted(shifts.items(), key=lambda kv: -kv[1]):
            say("    %-28s %d" % (shift, n))
        for v, was in moved[:10]:
            say(C.dim("      %-32s %s -> %s   %s"
                      % (v.business.display[:32], was, v.outcome.value,
                         (v.reject_reason or "")[:40])))
        if len(moved) > 10:
            say(C.dim("      +%d more" % (len(moved) - 10)))

        if not args.apply:
            say("")
            say(C.dim("  nothing written. re-run with --apply to store these verdicts."))
        else:
            store.add_tasks(args.run_id, "requalify", ["pass-%d" % time.time()])
            task = store.claim(args.run_id, "requalify")
            if task:
                store.complete(task["id"], [v.to_dict() for v in verdicts],
                               run_id=args.run_id)
            say(C.good("  %d verdicts updated in the ledger" % len(verdicts)))

        if args.out:
            rows = [v.to_dict() for v in verdicts]
            if not args.include_rejected:
                rows = sorted([r for r in rows if r.get("qualified")],
                              key=lambda r: -(r.get("score") or 0))
            rows = shape_output(rows, campaign.output)
            target = Path(args.out).expanduser()
            fmt = args.format or (target.suffix.lstrip(".") if
                                  target.suffix.lstrip(".") in ("csv", "json", "jsonl")
                                  else "csv")
            for f in write_results(target, rows, fmt, campaign.output):
                say("  ->", C.accent(str(f)), C.dim("(%d rows)" % len(rows)))
        return 0
    finally:
        store.close()


def cmd_jobs(args) -> int:
    """What the ledger knows. The answer to "did that overnight run finish?"."""
    from .store import Store
    store = Store(Path(args.state_db) if args.state_db else None)
    try:
        if not args.run_id:
            runs = store.list_runs()
            if not runs:
                say(C.dim("no durable runs recorded"))
                return 0
            out(C.bold("%-14s %-22s %-9s %8s %8s" %
                       ("RUN", "NAME", "STATE", "RESULTS", "LEFT")))
            for r in runs:
                left = store.pending_count(r["id"])
                colour = C.good if r["state"] == "done" else (
                    C.warn if left else C.dim)
                out("%-14s %-22s %s %8d %8d" % (
                    r["id"], (r["name"] or "")[:22],
                    colour("%-9s" % r["state"]), store.result_count(r["id"]), left))
            say(C.dim("\n  kerb run <campaign> --durable --resume <RUN> to continue one"))
            return 0

        counts = store.counts(args.run_id)
        run = store.get_run(args.run_id)
        if not run:
            die("no run %s in the ledger" % args.run_id)
        out(C.bold("%s  %s" % (run["id"], run["name"] or "")))
        for k in ("done", "pending", "deferred", "running", "failed"):
            if counts.get(k):
                out("  %-10s %d" % (k, counts[k]))
        out("  %-10s %d" % ("results", counts["results"]))
        failures = store.failures(args.run_id)
        if failures:
            say("")
            say(C.bad("  %d unit(s) gave up:" % len(failures)))
            for f in failures[:10]:
                say(C.dim("    %-28s %s" % (f["key"][:28], (f["error"] or "")[:60])))
        return 0
    finally:
        store.close()


def cmd_setup(args) -> int:
    """Create -- and prove -- the profile the Google Maps collector browses with.

    Proving it is the whole point. A setup that writes a file and declares
    success leaves the first real run to discover the problem, forty places in.
    """
    from .session import Profile, SetupError, check, setup

    if args.check:
        rep = check()
        print("profile: %s" % rep["profile"])
        print("         %s" % rep["detail"])
        if rep["ok"]:
            if not rep.get("signed_in"):
                print("\nworking, but SIGNED OUT. Google serves signed-out clients a")
                print("reduced view of Maps -- listings come through, review data does")
                print("not. To fix it, see `kerb setup --help` (--import-cookies).")
            else:
                print("\nworking, signed in. `kerb serve` and pick Google Maps.")
            return 0
        print("\nnot working: %s" % rep["problem"])
        print("run `kerb setup` to rebuild it.")
        return 1

    cookies = None
    if args.import_cookies:
        from .session import parse_cookies
        src = Path(args.import_cookies).expanduser()
        if not src.exists():
            print("no such file: %s" % src)
            return 1
        try:
            cookies = parse_cookies(src.read_text())
        except SetupError as exc:
            print("could not read cookies: %s" % exc)
            return 1
        # NAMES only. A cookie value is a credential and does not belong in a
        # terminal, a scrollback buffer, or a screenshot of one.
        print("read %d cookie(s) from %s: %s\n"
              % (len(cookies), src.name, ", ".join(sorted(cookies)[:8])))

    print("Creating a browsing profile for the Google Maps collector.")
    if not cookies:
        print("No account and no password are involved. Kerb reads public")
        print("business listings, which needs neither -- but see the note at")
        print("the end about what a signed-out session does not get.\n")
    try:
        prof = setup(hl=args.hl, gl=args.gl, cookies=cookies)
    except SetupError as exc:
        print("setup failed: %s" % exc)
        return 1
    print("saved  %s" % prof.path)
    print("       %s" % prof.describe())

    if prof.signed_in:
        print("\nSigned in. You get the full Maps view.")
    else:
        print("\n" + "-" * 68)
        print("SIGNED OUT -- this still works, with one limit.")
        print("Google serves signed-out clients a reduced view of Maps.")
        print("Listings come through in full (name, category, address, phone,")
        print("website, rating, coordinates); review data does not.")
        print("")
        print("To lift it, export cookies from a browser already signed in to")
        print("Google and hand them to Kerb once:")
        print("")
        print("    kerb setup --import-cookies ~/Downloads/cookies.txt")
        print("")
        print("They are stored in your profile at 0600 and never leave the")
        print("machine. Kerb never asks for a password.")
        print("-" * 68)
    print("\nReady. `kerb serve` and choose Google Maps, or run a campaign")
    print("with sources: [{id: gmaps}].")
    return 0


def cmd_reviews(args) -> int:
    """Reviews for a run that already exists. Deliberately a SECOND step.

    Normal Kerb never opens a browser. Reviews are the one thing that needs
    one -- the request cannot be synthesised -- and most runs do not want them,
    so this is opt-in and runs over the businesses that QUALIFIED rather than
    everything discovery happened to find.
    """
    import json as _json
    from .reviews import HarvestError, harvest
    from .signals import detail
    from .store import Store

    if not detail.HAVE_SELENIUM:
        say(C.bad("reviews need a browser"))
        say('  pip install "kerb[browser]"')
        return 1

    store = Store(Path(args.state_db) if getattr(args, "state_db", None) else None)
    try:
        if store.get_run(args.run) is None:
            say(C.bad("no run %r in the ledger at %s" % (args.run, store.path)))
            say("  `kerb jobs` lists what is there")
            return 1
        stored = store.results(args.run)
    finally:
        store.close()

    rows = [r for r in stored if args.all or (r.get("outcome") == "qualified")]
    # The harvest swaps each business's hex cid into one captured request, so
    # only a `0x...:0x...` cid can be harvested. A row with a decimal id or an
    # OSM id used to be passed through and fail inside the browser.
    usable = [r for r in rows if _HEX_CID.match(str(r.get("cid") or ""))]
    if len(usable) < len(rows):
        say(C.dim("  %d business(es) skipped: no Google Maps id to address them by"
                  % (len(rows) - len(usable))))
    rows = usable
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        say(C.bad("nothing to harvest"))
        say("  the run has no qualified businesses carrying a Maps id"
            + ("" if args.all else " (try --all)"))
        return 1

    out_path = Path(args.out or ("reviews-%s.jsonl" % args.run))
    say(C.bold("harvesting reviews"), C.dim("%d business(es) -> %s" % (len(rows), out_path)))
    say(C.dim("  one browser; one request captured, then reused for every business"))

    total, failed = 0, 0
    from .reviews import harvest_many
    from .session import Profile
    by_cid = {str(r.get("cid")): r for r in rows}
    first_url = ((rows[0].get("extras") or {}).get("maps_url")
                 or _maps_url(str(rows[0].get("cid") or "")))
    drv = detail._driver(Profile.load())
    try:
        with out_path.open("w") as fh:
            def wrote(cid, revs, fail):
                nonlocal total, failed
                r = by_cid.get(cid) or {}
                name = (r.get("name") or cid or "?")[:38]
                if fail and not revs:
                    failed += 1
                    say(" ", C.bad("%-40s" % name), C.dim(str(fail)[:52]))
                    return
                for rev in revs:
                    rev["cid"] = cid
                    rev["business"] = r.get("name")
                    fh.write(_json.dumps(rev) + "\n")
                total += len(revs)
                say(" ", C.good("%-40s" % name), C.dim("%d reviews" % len(revs)))

            harvest_many(drv, list(by_cid), first_url,
                         max_reviews=args.max_reviews, on_business=wrote)
    except HarvestError as exc:
        say(C.bad("harvest failed:"), str(exc))
        return 1
    finally:
        # The predecessor's worst failure was a driver outliving its run.
        detail.close()

    say("")
    say("  %d review(s) from %d business(es)%s"
        % (total, len(rows) - failed,
           ", %d could not be read" % failed if failed else ""))
    say("  ->", C.bold(str(out_path)))
    return 0 if total else 1


_HEX_CID = re.compile(r"^0x[0-9a-f]+:0x[0-9a-f]+$", re.I)


def _maps_url(cid: str) -> str:
    """`0xAAA:0xBBB` -> the maps.google.com/?cid= link the RPC needs."""
    try:
        return "https://www.google.com/maps?cid=%d" % int(cid.split(":")[1], 16)
    except (IndexError, ValueError):
        return "https://www.google.com/maps?q=%s" % cid


def cmd_doctor(args) -> int:
    """Is this install healthy, and is anything left running from last time."""
    import importlib
    ok = True

    def check(label: str, good, detail: str = "") -> None:
        """good=None means "worth knowing, not a fault" -- an optional feature
        that has not been set up must not fail an install check."""
        nonlocal ok
        if good is None:
            say(" ", C.dim("--  "), "%-26s %s" % (label, C.dim(detail)))
            return
        ok = ok and good
        say(" ", C.good("ok  ") if good else C.bad("bad "), "%-26s %s"
            % (label, C.dim(detail)))

    say(C.bold("kerb doctor"), C.dim("v" + __version__))
    say("")
    check("python", sys.version_info >= (3, 9),
          ".".join(map(str, sys.version_info[:3])) + " (need 3.9+)")

    for mod in ("yaml", "httpx"):
        try:
            m = importlib.import_module(mod)
            check(mod, True, getattr(m, "__version__", ""))
        except ImportError:
            check(mod, False, "not installed")

    # The server extras are genuinely optional: `kerb run` needs none of them.
    # Reporting their absence as a fault would tell an engine-only install that
    # it is broken when it is doing exactly what was asked of it.
    missing = []
    for mod in ("fastapi", "uvicorn", "pydantic"):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        say(" ", C.warn("--  "), "%-26s %s" % ("web UI",
            C.dim("not installed (%s) -- `pip install 'kerb[server]'` for the UI; "
                  "`kerb run` works without it" % ", ".join(missing))))
    else:
        check("web UI", True, "fastapi, uvicorn, pydantic")

    try:
        from .packs import library
        lib = library()
        bad_packs = []
        for p in lib.all():
            if not isinstance(p.data, dict):
                bad_packs.append(p.id)
        check("packs", not bad_packs,
              "%d loaded%s" % (len(lib), (", broken: " + ", ".join(bad_packs))
                               if bad_packs else ""))
    except Exception as exc:                       # noqa: BLE001
        check("packs", False, str(exc))

    try:
        from . import signals as sig, sources as src
        check("signals", True, "%d registered" % len(sig.all_signals()))
        check("sources", True, ", ".join(s.id for s in src.all_sources()))

        # "Do I have to set something up first?" answered before the first run
        # rather than by a confusing failure forty places in.
        from .session import Profile
        prof = Profile.load()
        cool = prof.cooling()
        if not prof.exists:
            check("google maps profile", None,
                  "not set up -- run `kerb setup` (only needed for the gmaps source)")
        elif cool:
            check("google maps profile", False,
                  "cooling down for another %d min after %d block(s)"
                  % (cool // 60, prof.blocks))
        else:
            check("google maps profile", True, prof.describe().split("\n")[0])
    except Exception as exc:                       # noqa: BLE001
        check("signals/sources", False, str(exc))

    try:
        leftovers = find_managed()
        check("no leftover processes", not leftovers,
              "%d still running -- run `kerb stop`" % len(leftovers) if leftovers
              else "clean")
    except CannotEnumerate as exc:
        check("no leftover processes", False,
              "%s -- kerb stop cannot verify cleanup here" % exc)

    say("")
    say(C.good("  healthy") if ok else C.bad("  problems above"))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Process hygiene
# --------------------------------------------------------------------------

# Two tiers, because a substring match on a command line is not evidence that
# the command IS that thing. `grep -r "kerb.api:app" .` contains the pattern
# and must never be killed by it.
#
# STRONG patterns are unambiguous on their own -- nothing else on a machine
# writes these strings. WEAK patterns must additionally be confirmed by the
# executable actually being an interpreter we could have launched.
# Every pattern is case-insensitive, and that is not cosmetic. macOS's framework
# interpreter is literally named `Python` with a capital P, so a case-sensitive
# `\bpython\S*` never matched `.../MacOS/Python -m kerb serve` -- `kerb stop`
# reported "nothing to stop" while the server it started was still holding the
# port. A cleanup command that cannot see its own processes is worse than none,
# because it is trusted.
STRONG_PATTERNS = [re.compile(p, re.I) for p in (
    # A browser kerb started, and only kerb: signals/detail.py gives every
    # Chrome it launches a `kerb-worker-*` profile directory. chromedriver
    # itself has no such tag -- it is found through kerb/procs.py's registry.
    r"--user-data-dir=\S*kerb-worker",
)]
# A registered pid is only trusted while it still looks like a browser: pids are
# reused, and a stale registry entry must never aim `kerb stop` at a stranger.
BROWSERISH = re.compile(r"chrom", re.I)
WEAK_PATTERNS = [re.compile(p, re.I) for p in (
    r"\buvicorn\s+kerb\.api",
    r"\bkerb\.api:app\b",
    r"\bpython\S*\s+-m\s+kerb\b",
    r"\bkerb\s+(serve|run)\b",             # the installed console script
)]
# Chrome's real executable contains a space ("Google Chrome"), so it is never
# identified this way -- it is caught by STRONG_PATTERNS instead.
EXE_OK = re.compile(r"^(python[\d.]*|uvicorn|kerb)$", re.I)


class CannotEnumerate(RuntimeError):
    """We could not list processes, which is NOT the same as there being none.

    Returning an empty list here made `kerb stop` print "nothing to stop --
    clean" on any machine without a working `ps` (Windows, a stripped
    container). Given that this command exists because orphaned browsers once
    ate 88GB, a confident all-clear it cannot actually back up is the single
    worst thing it could say.
    """


def _ps() -> List[Dict[str, Any]]:
    out = None
    if os.name == "nt":
        # PowerShell is the only thing that reliably reports a full command
        # line on modern Windows; wmic is deprecated and often absent.
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | ForEach-Object "
                 "{ \"$($_.ProcessId) $($_.ParentProcessId) $($_.CommandLine)\" }"],
                capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            out = None
    else:
        # -ww: unlimited width. Without it ps truncates every command line to
        # $COLUMNS whenever that is exported -- 80 characters under pytest and
        # in plenty of shells -- and a browser's tag (`--user-data-dir=...
        # kerb-worker`) sits far past column 80 of a Chrome command line. The
        # process was running; `kerb stop` simply could not read far enough to
        # recognise it. COLUMNS is dropped as well, for any ps that honours it
        # over the flag.
        env = {k: v for k, v in os.environ.items() if k != "COLUMNS"}
        try:
            out = subprocess.run(["ps", "-ww", "-eo", "pid=,ppid=,command="],
                                 capture_output=True, text=True, timeout=15,
                                 env=env).stdout
        except (OSError, subprocess.SubprocessError):
            out = None
    if out is None:
        raise CannotEnumerate(
            "could not list running processes on this system (%s)" % sys.platform)
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "cmd": parts[2]})
        except ValueError:
            continue
    return rows


def find_managed() -> List[Dict[str, Any]]:
    """Every process this install is responsible for.

    Never returns the caller or any of its ancestors. `kerb stop` run from a
    shell must not kill the shell it was typed into, and the shell's command
    line can easily contain the very pattern being searched for.
    """
    rows = _ps()
    by_pid = {r["pid"]: r for r in rows}

    protected, cur = set(), os.getpid()
    while cur and cur not in protected:
        protected.add(cur)
        cur = (by_pid.get(cur) or {}).get("ppid", 0)

    # Browsers recorded at launch, and everything under them. Descendants are
    # included whatever their command line: a renderer or GPU helper does not
    # say "chrome" reliably, and it is exactly what an orphaned tree leaves.
    from . import procs
    seeds = [r["pid"] for r in rows
             if r["pid"] in procs.registered_pids() and BROWSERISH.search(r["cmd"])]
    tree = procs.descendants(rows, seeds)

    found = []
    for r in rows:
        if r["pid"] in protected:
            continue
        cmd = r["cmd"]
        if r["pid"] in tree or any(p.search(cmd) for p in STRONG_PATTERNS):
            found.append(r)
            continue
        exe = os.path.basename(cmd.split()[0]) if cmd.split() else ""
        if EXE_OK.match(exe) and any(p.search(cmd) for p in WEAK_PATTERNS):
            found.append(r)
    return found


def _forget_dead_browsers() -> None:
    """After a verified stop: drop registry entries whose processes are gone,
    and the throwaway profile directories they were using."""
    import shutil
    from . import procs
    try:
        # Alive AND still a browser: the same trust rule find_managed uses. A
        # recorded pid now held by some other program is not our browser, and
        # counting it as alive would keep the entry forever.
        alive = {r["pid"] for r in _ps() if BROWSERISH.search(r["cmd"])}
    except CannotEnumerate:
        return
    for entry in procs.registered():
        pids = entry.get("pids") or []
        if any(p in alive for p in pids):
            continue
        profile = entry.get("profile_dir") or ""
        if "kerb-worker" in os.path.basename(profile.rstrip("/\\")):
            shutil.rmtree(profile, ignore_errors=True)
        procs.forget(owner=None, pids=pids)


def cmd_stop(args) -> int:
    """Terminate, wait, kill, then VERIFY. Reporting success without checking
    is how orphans survive a cleanup that claimed to work."""

    # A driver that outlives its run is the predecessor's worst failure. The
    # detail signal shares one browser; closing it is part of stopping.
    try:
        from .signals.detail import close as _close_browser
        _close_browser()
    except Exception:                            # noqa: BLE001
        pass
    try:
        procs = find_managed()
    except CannotEnumerate as exc:
        say(C.bad("cannot verify: %s" % exc))
        say(C.dim("  Not reporting 'clean' -- this command has no way to check."))
        say(C.dim("  Close any kerb server and browser windows by hand."))
        return 1
    if not procs:
        _forget_dead_browsers()
        say(C.good("nothing to stop"), C.dim("- clean"))
        return 0

    say("%s %d process%s" % ("would stop" if args.dry_run else "stopping",
                             len(procs), "" if len(procs) == 1 else "es"))
    for p in procs:
        say(C.dim("  %-7s %s" % (p["pid"], p["cmd"][:110])))
    if args.dry_run:
        return 0

    for sig_num, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
        alive = find_managed()
        if not alive:
            break
        for p in alive:
            try:
                os.kill(p["pid"], sig_num)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(wait)

    left = find_managed()
    if left:
        say(C.bad("  %d survived:" % len(left)))
        for p in left:
            say(C.dim("    %s %s" % (p["pid"], p["cmd"])))
        return 1
    _forget_dead_browsers()
    say(C.good("  clean"), C.dim("- verified, nothing left"))
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kerb",
        description="Find and qualify local businesses. Anywhere, any trade.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="run `kerb` with no arguments to open the UI")
    p.add_argument("--version", action="version", version="kerb " + __version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the server and open the UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--reload", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--log-level", default="warning")
    s.set_defaults(fn=cmd_serve)

    r = sub.add_parser("run", help="run a campaign headless")
    r.add_argument("campaign")
    r.add_argument("-o", "--out", help="write results here (default: stdout)")
    r.add_argument("-f", "--format", choices=["csv", "json", "jsonl"])
    r.add_argument("--include-rejected", action="store_true",
                   help="keep rejected businesses, with their reasons")
    r.add_argument("--durable", action="store_true",
                   help="run place discovery through the work ledger: survives "
                        "kill -9, resumes where it stopped, retries per place")
    r.add_argument("--state-db", metavar="FILE",
                   help="where the ledger lives (default ~/.kerb/state/kerb.db)")
    r.add_argument("--resume", metavar="RUN_ID",
                   help="continue a previous durable run")
    r.add_argument("--suppress", action="append", metavar="FILE", default=[],
                   help="hide businesses listed in this file (repeatable); "
                        "any CSV with a cid column, or one id per line")
    r.add_argument("--since", metavar="RUN_ID",
                   help="hide what a previous run already surfaced, unless its "
                        "verdict has changed")
    r.add_argument("--checkpoint", metavar="FILE",
                   help="journal results as they arrive; re-run with the same "
                        "file to resume instead of starting over")
    r.add_argument("-q", "--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)

    e = sub.add_parser("estimate", help="what a campaign would do, without doing it")
    e.add_argument("campaign")
    e.set_defaults(fn=cmd_estimate)

    rq = sub.add_parser("requalify",
                        help="re-judge a stored run with today's packs and rules")
    rq.add_argument("run_id")
    rq.add_argument("-c", "--campaign", help="rules to apply (default: the run's own)")
    rq.add_argument("--state-db")
    rq.add_argument("--apply", action="store_true",
                    help="write the new verdicts back (default: show the diff only)")
    rq.add_argument("-o", "--out")
    rq.add_argument("-f", "--format", choices=["csv", "json", "jsonl"])
    rq.add_argument("--include-rejected", action="store_true")
    rq.set_defaults(fn=cmd_requalify)

    j = sub.add_parser("jobs", help="durable runs: what finished, what is left")
    j.add_argument("run_id", nargs="?")
    j.add_argument("--state-db")
    j.set_defaults(fn=cmd_jobs)

    su = sub.add_parser("setup",
                        help="create the browsing profile the gmaps collector uses")
    su.add_argument("--hl", default="en", help="interface language (default en)")
    su.add_argument("--gl", default="us", help="region, e.g. uk, de (default us)")
    su.add_argument("--check", action="store_true",
                    help="only report whether the existing profile still works")
    su.add_argument("--import-cookies", metavar="FILE", dest="import_cookies",
                    help="a cookies.txt, JSON cookie export, or a Cookie: header "
                         "line, exported from a browser already signed in to "
                         "Google. Unlocks the full Maps view.")
    su.set_defaults(fn=cmd_setup)

    rv = sub.add_parser("reviews",
                        help="harvest reviews for a finished run (opt-in, needs a browser)")
    rv.add_argument("run", help="run id, from `kerb jobs`")
    rv.add_argument("--out", help="where to write them (default reviews-<run>.jsonl)")
    rv.add_argument("--limit", type=int, help="max businesses to harvest")
    rv.add_argument("--max-reviews", type=int, default=None,
                    help="stop after this many reviews per business")
    rv.add_argument("--all", action="store_true",
                    help="every business in the run, not just the qualified ones")
    rv.add_argument("--state-db", metavar="FILE",
                    help="where the ledger lives (default: $KERB_STATE_DB, "
                         "else ~/.kerb/state/kerb.db)")
    rv.set_defaults(fn=cmd_reviews)

    sub.add_parser("doctor", help="check this install").set_defaults(fn=cmd_doctor)
    st = sub.add_parser("stop", help="kill everything kerb started")
    st.add_argument("-n", "--dry-run", action="store_true",
                    help="list what would be killed, kill nothing")
    st.set_defaults(fn=cmd_stop)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        # Bare `kerb` is the friendly path: start the UI.
        args = parser.parse_args(["serve"])
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        say("")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
