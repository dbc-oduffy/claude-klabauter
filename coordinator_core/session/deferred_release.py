"""Durable record of a post-commit claim release that was skipped past the land deadline.

``record`` writes one file per (session, commit sha) under
``<sessions_dir>/deferred-release/<sid>/``; ``has_pending`` is a directory
listing; ``drain`` releases the recorded paths whose blob at the recorded sha
still equals HEAD's, then removes the record. Skip-on-falsy-sid and
swallow-on-failure come from ``scope.release_committed_claims_or_retain``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from typing import Iterable, List, Optional

_LOG = logging.getLogger(__name__)

_DIRNAME = "deferred-release"


def _record_dir(sid: str, cwd: str) -> Optional[str]:
    from coordinator_core.session import core

    base = core.sessions_dir(cwd)
    if not base or not sid:
        return None
    return os.path.join(base, _DIRNAME, hashlib.sha1(sid.encode("utf-8")).hexdigest())


def record(sid: Optional[str], cwd: str, sha: str, paths: Iterable[str]) -> bool:
    """Persist ``paths`` for commit ``sha``; False when nothing was written."""
    try:
        path_list = sorted({p for p in paths if p})
        d = _record_dir(sid or "", cwd)
        if d is None or not sha or not path_list:
            return False
        os.makedirs(d, exist_ok=True)
        final = os.path.join(d, sha + ".json")
        tmp = f"{final}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"sha": sha, "paths": path_list}, fh)
        os.replace(tmp, final)
        return True
    except Exception:
        _LOG.debug("deferred_release.record failed; claims retained", exc_info=True)
        return False


def has_pending(sid: Optional[str], cwd: str) -> bool:
    """True when ``sid`` has at least one undrained record. One listdir, no git."""
    try:
        d = _record_dir(sid or "", cwd)
        return d is not None and any(n.endswith(".json") for n in os.listdir(d))
    except OSError:
        return False


def _matching_paths(cwd: str, sha: str, paths: List[str]) -> Optional[List[str]]:
    """Paths whose blob at ``sha`` equals HEAD's (both absent counts as equal).

    One ``git cat-file --batch-check`` spawn for every path at both revs.
    None means git could not answer, so the record must be kept. An unresolvable ``sha`` (a
    gc'd commit, or another repo's) yields ``[]``, so the record is dropped with its claims
    retained for the reaper; a path containing a newline cannot be batched and is likewise
    left out of the result.
    """
    safe = [p for p in paths if "\n" not in p]
    lines = [f"{sha}^{{commit}}"]
    for p in safe:
        lines.append(f"{sha}:{p}")
        lines.append(f"HEAD:{p}")
    proc = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=cwd,
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = proc.stdout.splitlines()
    if proc.returncode != 0 or len(out) != len(lines):
        return None
    if out[0].endswith(" missing"):
        return []
    return [
        p
        for i, p in enumerate(safe)
        if out[1 + 2 * i].split(" ")[0] == out[2 + 2 * i].split(" ")[0]
        or (out[1 + 2 * i].endswith(" missing") and out[2 + 2 * i].endswith(" missing"))
    ]


def drain(sid: Optional[str], cwd: str) -> List[str]:
    """Release every pending record's still-matching paths; return the released paths."""
    released: List[str] = []
    try:
        d = _record_dir(sid or "", cwd)
        if d is None or not sid:
            return released
        from coordinator_core.session import scope

        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            rec_path = os.path.join(d, name)
            try:
                with open(rec_path, encoding="utf-8") as fh:
                    rec = json.load(fh)
                sha, rec_paths = rec["sha"], list(rec["paths"])
            except (OSError, ValueError, KeyError, TypeError):
                sha, rec_paths = "", []
            try:
                match = _matching_paths(cwd, sha, rec_paths) if rec_paths else []
            except Exception:
                _LOG.debug("deferred_release.drain git read failed; record kept", exc_info=True)
                continue
            if match is None:
                continue
            scope.release_committed_claims_or_retain(cwd, match, sid, "deferred_release.drain")
            released.extend(match)
            try:
                os.remove(rec_path)
            except OSError:
                pass
    except Exception:
        _LOG.debug("deferred_release.drain failed; claims retained", exc_info=True)
    return released
