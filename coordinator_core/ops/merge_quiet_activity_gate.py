"""
coordinator_core.ops.merge_quiet_activity_gate — JSON-RPC
"merge.quiet_activity_gate" operation.

Purpose: native replacement for the `merging-to-main` fence's inline
now-minus-last-commit-time check (`coordinator/skills/merging-to-main/
SKILL.md:493`) — the fence resolves `python3`-vs-`python`, computes elapsed
time since the PR branch's most recent commit, and exits 1 (with guidance)
if under a 5-minute quiet threshold, to avoid merging a branch a sibling
session is still actively pushing to. This op ports the reusable part (the
timestamp diff and threshold decision); the interpreter-resolution half of
the oracle evaporates entirely once this IS the native Python entrypoint —
see `state/audits/2026-07-22-command-payload-inventory/distinct-ops-new.tsv`
row `pre-merge-quiet-activity-gate`.

Op-key / contract (frozen — see
`state/audits/2026-07-22-command-payload-inventory/op-classification.tsv`):
    merge.quiet_activity_gate
    params:   {quiet_threshold_seconds: int}
    response: {ok: bool, seconds_since_last_commit: float, message: str|None}

Scope-verdict `show_top`: this op reads the CALLING worktree's own current
checkout — the most recent commit on the branch being merged is per-
worktree state, the same class `op_scopes.py` names for `coverage.gate`, so
resolution goes through the dispatch-supplied `repo_root` (`_origin_
worktree`), never a shared `common_dir`.

Threshold semantics: `ok` is True iff the elapsed time since HEAD's commit
timestamp is >= `quiet_threshold_seconds` — i.e. the gate PASSES when the
branch has been quiet for at least the threshold, mirroring the oracle's
"exit 1 (block) if UNDER the quiet threshold" framing inverted into a
positive `ok` flag. `message` is `None` when `ok` is True; otherwise it
names the branch's remaining quiet time so a caller can surface actionable
guidance instead of a bare boolean.

Idempotency (AC7): manifest rates idempotency-hazard "none" — read-only
comparison of wall-clock now vs. HEAD's git-recorded commit timestamp;
identical git state on rerun yields the identical verdict, no mutation
performed. No DEC-7 note required (idempotency- and platform-hazard both
rate below medium).

Negative-spec:
    - Never runs a mutating git command (no add/commit/stash/checkout/
      reset/push) — `git log -1 --format=%ct` only, a single read.
    - Does NOT resolve `python3`-vs-`python` — that half of the oracle
      fence is dead weight once this op IS the native entrypoint; the
      manifest's own platform-hazard note says as much.
    - Not a git repo, no commits yet, or `git` unavailable degrades to
      `ok: True` with an explanatory message rather than raising — there
      is nothing to gate a merge against when there is no commit history
      to have just landed.
    - Elapsed time is computed from the commit's own recorded epoch second
      (`%ct`), never a subprocess-parsed ISO string or a shell `date -d`
      call — avoids the GNU-vs-BSD `date` divergence entirely.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

_DEFAULT_QUIET_THRESHOLD_SECONDS = 300
_GIT_TIMEOUT_SECONDS = 15
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _head_commit_epoch_seconds(repo_root: Path) -> Optional[float]:
    """Return HEAD's committer timestamp as Unix epoch seconds, or None on
    any failure (not a git repo, no commits yet, `git` missing, timeout) —
    every failure mode collapses to "nothing to gate on" per the module
    negative-spec, never a raise.
    """
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(
            f"skip: _head_commit_epoch_seconds: proc = subprocess.run(...) failed: {sys.exc_info()[1]}",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        return float(int(raw))
    except ValueError:
        return None


def _resolve_quiet_threshold_seconds(raw: object) -> int:
    """Coerce `params["quiet_threshold_seconds"]` to int, falling back to
    the 5-minute default (the oracle's own literal) on anything missing or
    non-coercible — mirrors the standalone invocation path, which can
    plausibly hand this through as a string.
    """
    if raw is None:
        return _DEFAULT_QUIET_THRESHOLD_SECONDS
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_QUIET_THRESHOLD_SECONDS


def evaluate(repo_root: Path, quiet_threshold_seconds: int = _DEFAULT_QUIET_THRESHOLD_SECONDS) -> dict:
    """Pure-ish quiet-activity check over an already-resolved `repo_root`.

    Returns the `{ok, seconds_since_last_commit, message}` response shape
    directly, so both the registered handler and a standalone caller share
    one code path.
    """
    commit_epoch = _head_commit_epoch_seconds(repo_root)
    if commit_epoch is None:
        return {
            "ok": True,
            "seconds_since_last_commit": 0.0,
            "message": "no commit history to gate on (not a git repo, or no commits yet)",
        }

    elapsed = time.time() - commit_epoch
    # A commit timestamp in the future (clock skew, rebased history) never
    # blocks the gate — clamp to 0.0 rather than reporting a negative
    # "seconds since" that would misleadingly read as already-quiet-enough
    # by a wide margin, or not, depending on sign.
    elapsed = max(elapsed, 0.0)

    if elapsed >= quiet_threshold_seconds:
        return {"ok": True, "seconds_since_last_commit": elapsed, "message": None}

    remaining = quiet_threshold_seconds - elapsed
    return {
        "ok": False,
        "seconds_since_last_commit": elapsed,
        "message": (
            f"branch had a commit {elapsed:.1f}s ago — quiet threshold is "
            f"{quiet_threshold_seconds}s; wait {remaining:.1f}s more before merging"
        ),
    }


@register_op("merge.quiet_activity_gate")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'merge.quiet_activity_gate' handler.

    `repo_root` is the dispatch-supplied CALLING worktree (scope-verdict
    `show_top`) — this op takes no `repo_root` param of its own (frozen
    contract has none), so the handler falls back to `Path.cwd()` only for
    the standalone `python3 -m` invocation path.
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    quiet_threshold_seconds = _resolve_quiet_threshold_seconds(params.get("quiet_threshold_seconds"))
    return evaluate(root, quiet_threshold_seconds)
