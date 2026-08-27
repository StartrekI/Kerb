"""Where this process is allowed to read from.

The guard used to live in the API's campaign validator, which meant it only
covered the keys someone had remembered to list there. Source paths were
checked from the start; suppression lists, named by the same campaign, were
not -- and `suppress: {lists: [/etc/passwd]}` was read over HTTP.

Enumerating config keys is the wrong shape for this. A new key that names a
file is added by someone thinking about the feature, not about the guard, and
the guard silently fails to cover it. So the check lives at the point a file is
actually opened: whatever names it, and however it got there, it goes through
here.

Two modes, and the distinction is the whole design:

  unrestricted  the default, and correct for the CLI. It reads whatever the
                user's own shell can read, because it IS the user.
  confined      what `kerb serve` sets. The HTTP surface may be reachable by
                someone who is not the user, so a campaign arriving over it may
                only name files inside declared roots.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import List, Optional

_ROOTS: Optional[List[Path]] = None      # None means unrestricted
_LOCK = threading.Lock()


class PathNotAllowed(PermissionError):
    """A path outside every declared root."""


def confine(roots) -> List[Path]:
    """Restrict reads to these directories. Called by the server, not the CLI."""
    resolved = []
    for r in roots or []:
        try:
            resolved.append(Path(r).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    with _LOCK:
        global _ROOTS
        _ROOTS = resolved or [Path.cwd().resolve()]
        return list(_ROOTS)


def unrestrict() -> None:
    with _LOCK:
        global _ROOTS
        _ROOTS = None


def roots() -> Optional[List[Path]]:
    with _LOCK:
        return list(_ROOTS) if _ROOTS is not None else None


def confined() -> bool:
    return roots() is not None


def check(path) -> Path:
    """Resolve a path and confirm this process may read it.

    resolve() before the comparison is what makes it real: `..` is collapsed
    and symlinks are followed, so neither can be used to step outside a root.
    """
    target = Path(path).expanduser()
    allowed = roots()
    try:
        target = target.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathNotAllowed("not a usable path: %s (%s)" % (path, exc))
    if allowed is None:
        return target
    for root in allowed:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    raise PathNotAllowed(
        "%s is outside the folders this server may read (%s). Set "
        "KERB_ALLOWED_PATHS to widen it, or run kerb from that folder."
        % (target, ", ".join(str(r) for r in allowed)))


def from_env() -> Optional[List[Path]]:
    raw = os.environ.get("KERB_ALLOWED_PATHS")
    if raw:
        return [Path(p) for p in raw.split(os.pathsep) if p.strip()]
    return None
