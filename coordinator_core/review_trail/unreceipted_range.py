"""
coordinator_core.review_trail.unreceipted_range — which chain commits between
the last receipted run and a run's base carry no review credit.

Purpose: `unreceipted_ancestors` runs ONE capped `git log` over
`<run_base> --not <exclude_tips>`, keeps the commits `member` attributes to the
chain that touch at least one non-bookkeeping path, and drops every commit
credited by the reviewed-set store or by a reviewer sidecar receipt. The
population rule and both credit sources are the merge gate's
(`ops.gate_dimension_review._review_dimension_check`), reused and not widened.
The date and trailers needed for receipt credit ride the same walk, so the call
costs one spawn.

Endpoints must be full 40-hex. A short or symbolic endpoint yields an error
result, never an empty `uncovered`: an unresolvable range collapsing to the
empty set is the silent-pass trap `reviewed_set.py` documents.

Negative-spec:
    - Emits no verdict and blocks nothing; the caller maps the result onto a
      leg status.
    - Writes no store, ledger or sidecar.
    - Never walks uncapped: `--max-count` is always `cap + 1`, and a walk that
      returns `cap + 1` commits reports `truncated`.
    - A commit with neither a Session-Id nor a Deliverable-Id trailer cannot
      be attributed to the chain; it is counted in `unattributed` and never
      named in `uncovered`, never dropped silently.
    - Does not widen `coverage._BOOKKEEPING_PATH_PREFIXES`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_TIMEOUT_SECS = 30
_COMMIT_MARK = "\x01"
_FIELD_SEP = "\x1f"
_TRAILER = "%(trailers:key={key},valueonly=true,unfold=true,separator=%x20)"
_LOG_FORMAT = (
    f"{_COMMIT_MARK}%H{_FIELD_SEP}%cI{_FIELD_SEP}"
    + _TRAILER.format(key="Session-Id")
    + _FIELD_SEP
    + _TRAILER.format(key="Deliverable-Id")
)


@dataclass(frozen=True)
class RangeResult:
    """`uncovered`: full shas, newest first, of chain code commits no credit
    source covers. `truncated`: the walk hit `cap` and older commits were not
    examined. `unattributed`: code commits in the walk with no chain trailer.
    `error`: why the range could not be computed; `uncovered` is then empty
    and means nothing."""

    uncovered: Tuple[str, ...] = ()
    truncated: bool = False
    unattributed: int = 0
    error: Optional[str] = None


def _error(reason: str) -> RangeResult:
    return RangeResult(error=reason)


def _parse_walk(out: str) -> List[Tuple[str, str, str, str, bool]]:
    """`(sha, committed_at, session_id, deliverable_id, has_code_path)` per
    commit, in git's newest-first order."""
    from coordinator_core.coverage import _is_bookkeeping_path

    commits: List[list] = []
    for field in out.split("\0"):
        for line in field.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith(_COMMIT_MARK):
                parts = line[len(_COMMIT_MARK):].split(_FIELD_SEP)
                parts += [""] * (4 - len(parts))
                sha, committed_at, session_id, deliverable_id = (
                    (p.split() or [""])[0] for p in parts[:4]
                )
                if _FULL_SHA_RE.match(sha):
                    commits.append([sha, committed_at, session_id, deliverable_id, False])
                else:
                    commits.append(None)
            elif commits and commits[-1] is not None:
                if not _is_bookkeeping_path(line.replace("\\", "/")):
                    commits[-1][4] = True
    return [tuple(c) for c in commits if c is not None]  # type: ignore[misc]


def unreceipted_ancestors(
    repo_root: str,
    run_base: str,
    *,
    exclude_tips: Iterable[str],
    member: Callable[[str, str, str], bool],
    cap: int,
) -> RangeResult:
    """Chain code commits reachable from `run_base`, not reachable from any of
    `exclude_tips`, that no review credit covers.

    `member(sha, session_id, deliverable_id)` decides chain membership for an
    attributed commit. At most `cap` commits are examined."""
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    tips = list(exclude_tips)
    if cap < 1:
        return _error(f"cap must be positive, got {cap}")
    if not _FULL_SHA_RE.match(run_base or ""):
        return _error(f"run_base is not a full 40-hex sha: {run_base!r}")
    for tip in tips:
        if not _FULL_SHA_RE.match(tip or ""):
            return _error(f"exclude tip is not a full 40-hex sha: {tip!r}")

    argv = [
        "git", "log", "--no-merges", f"--format={_LOG_FORMAT}", "--name-only", "-z",
        f"--max-count={cap + 1}", run_base,
    ]
    if tips:
        argv += ["--not", *tips]
    argv.append("--")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return _error(f"git log timed out after {_GIT_TIMEOUT_SECS}s")
    except OSError as exc:
        return _error(f"git log could not start: {exc}")
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        return _error(f"git log failed: {lines[-1] if lines else 'rc=' + str(proc.returncode)}")

    walk = _parse_walk(proc.stdout)
    truncated = len(walk) > cap
    walk = walk[:cap]

    unattributed = 0
    candidates: List[Tuple[str, str, str]] = []
    for sha, committed_at, session_id, deliverable_id, has_code in walk:
        if not has_code:
            continue
        if not session_id and not deliverable_id:
            unattributed += 1
            continue
        if member(sha, session_id, deliverable_id):
            candidates.append((sha, committed_at, session_id))

    uncovered: List[str] = []
    if candidates:
        from coordinator_core.review_trail.receipt_credit import receipt_credited_shas
        from coordinator_core.review_trail.reviewed_set import read_reviewed_set

        reviewed = read_reviewed_set(str(repo_root))
        remaining = [c for c in candidates if c[0] not in reviewed]
        credited = receipt_credited_shas(str(repo_root), remaining) if remaining else set()
        uncovered = [c[0] for c in remaining if c[0] not in credited]

    return RangeResult(
        uncovered=tuple(uncovered), truncated=truncated, unattributed=unattributed
    )
