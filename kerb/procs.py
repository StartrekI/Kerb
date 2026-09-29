"""The browser processes this install started, written down.

`kerb stop` and `kerb doctor` find what Kerb started by reading command lines.
That only works for a process whose command line says so, and the browser the
review signal launches did not: Selenium starts chromedriver with a bare port
and Chrome with a temporary profile, neither of which names Kerb. So the one
kind of process this project exists to never leak -- the one that once filled a
disk -- was the one kind the cleanup command could not see.

Two things fix that, and each covers the other's gap:

  * Chrome gets a profile directory named `kerb-worker-...`, which the process
    finder already recognises. That survives the worst case, an orphan whose
    parent is gone.
  * chromedriver and everything under it is recorded here, by pid. That covers
    a profile the user chose themselves (KERB_CHROME_PROFILE), which carries no
    tag.

A pid is only trusted while the process behind it still looks like a browser;
pids are reused, and a stale entry must never make `kerb stop` kill something
unrelated.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()


def registry_path() -> Path:
    from .store import default_dir
    return default_dir() / "browsers.json"


def registered() -> List[Dict[str, Any]]:
    """Every recorded browser launch, newest last. Never raises."""
    try:
        data = json.loads(registry_path().read_text())
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _write(entries: List[Dict[str, Any]]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.%d.%d" % (os.getpid(), threading.get_ident()))
    try:
        tmp.write_text(json.dumps(entries, indent=2))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def register(pids: List[int], profile_dir: Optional[str] = None) -> None:
    """Record a browser this process launched."""
    with _LOCK:
        entries = registered()
        entries.append({"owner": os.getpid(), "pids": sorted({int(p) for p in pids}),
                        "profile_dir": profile_dir, "at": time.time()})
        _write(entries)


def forget(owner: Optional[int] = None, pids: Optional[List[int]] = None) -> None:
    """Drop entries: this process's (default), or those holding any of `pids`."""
    owner = os.getpid() if owner is None and pids is None else owner
    with _LOCK:
        keep = []
        for e in registered():
            if owner is not None and e.get("owner") == owner:
                continue
            if pids and set(e.get("pids") or []) & set(pids):
                continue
            keep.append(e)
        if keep or registry_path().exists():
            _write(keep)


def registered_pids() -> set:
    return {int(p) for e in registered() for p in (e.get("pids") or [])}


def descendants(rows: List[Dict[str, Any]], roots) -> set:
    """Every pid below `roots` in a process listing (roots included)."""
    children: Dict[int, List[int]] = {}
    for r in rows:
        children.setdefault(r["ppid"], []).append(r["pid"])
    found, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found.add(pid)
        stack.extend(children.get(pid, []))
    return found
