"""
coordinator_core.ops.session.rotate_orphan_sweep_log — session.rotate_orphan_sweep_log op.

Purpose: Rotate tasks/orphan-sweep-notes.md — the append-only WARN-marker file
written by session.boot_sweep's _append_warn_marker — after /workday-start
Step 0.8 has read it.  Rotation keeps the 4-line header and surfaces every
marker line past it (the "tail") back to the caller, so the read-and-rotate
contract documented in boot_sweep ("the file is append-only here;
/workday-start rotates it after reading") has a native op home.

The whole read-decide-write runs end-to-end inside
coordinator_core.locked_write.locked_rmw (A2 settlement, RATIFIES): the same
cross-process file lock plus mkstemp + os.replace atomic swap that the C1a
write paths use.  A concurrent boot_sweep appender that writes the same file
serialises against the same lock sidecar keyed on the canonical target path —
its append lands fully before or fully after the rotation swap, never
interleaved with a partial truncate.  Pure-Python file I/O throughout; the
fence's tail/head/mv pipeline does not survive (no subprocess anywhere).

Idempotency (DEC-7 note, AC8): the >4-line rotation predicate IS the
idempotency mechanism.  Rotation rewrites the file to exactly its 4-line
header, so the post-rotation state fails the >4-line predicate by
construction and the second invocation with identical inputs is a
{rotated: false} no-op — no marker file, no timestamp guard, no extra state.
An absent file (boot_sweep never fired, or a fresh clone) is likewise a
{rotated: false} no-op with header_lines_kept: 0, not an error: absence of
WARN markers is a valid healthy state.

Scope keying: _OP_KEY_SCOPE = "common_dir", registered to MATCH
session.boot_sweep's existing entry (same key class, same derivation) — the
handler derives the worktree via main_worktree_root(common_dir) exactly as
boot_sweep's WARN-marker writer targets GIT_ROOT-worktree-rooted
tasks/orphan-sweep-notes.md.  A mismatched scope would rotate a different
repo's copy of the file than the one boot_sweep wrote to (manifest hazard
note); the shared keying is what pins both ops to the same file.

Self-registration: importing this module calls
register_op("session.rotate_orphan_sweep_log", _handler) as a side-effect.
Add this module to coordinator_core/ops/__init__.py (CC-3 registration pass —
NOT done by this module's landing commit) to trigger registration.

Spec backlinks:
  - Settlement (binding): docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A2
  - Parent plan: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Wave 3
  - Manifest row: state/audits/2026-07-22-command-payload-inventory/op-classification.tsv
    (op-key session.rotate_orphan_sweep_log)
  - Writer being rotated: coordinator_core/ops/session/boot_sweep.py _append_warn_marker
  - Lock primitive: coordinator_core/locked_write.py locked_rmw

Negative-spec:
  - Does NOT edit or import boot_sweep.py (A2 is a disjoint sibling — cross-plan
    constraint; the 4-line-header shape is a documented file-format contract,
    not a code-level import).
  - Does NOT spawn tail/head/mv or any subprocess — the fence's shell pipeline
    is replaced by pure-Python line slicing under locked_rmw (CC-1).
  - Does NOT hold any state outside the target file — no rotation marker, no
    sidecar; the ≤4-line predicate is the entire rerun story (DEC-7 above).
  - Does NOT treat params.repo_root as the path source — D3 consistency check
    only; the worktree derives from the engine-supplied common_dir arg.
  - Does NOT create the file when absent — rotation of nothing is a no-op,
    not a header-scaffolding write (boot_sweep owns file creation).
  - Does NOT preserve the tail anywhere on disk — tail_surfaced in the wire
    result is the single hand-off of the rotated-out marker lines; the caller
    (/workday-start Step 0.8) has already read/consumed them by contract.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, locked_rmw
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root

_LOG = logging.getLogger(__name__)

# The boot_sweep-authored header is exactly 4 lines (title, blank, purpose,
# blank — see boot_sweep._append_warn_marker's create-with-header branch).
# Everything past line 4 is appended WARN markers, i.e. the rotatable tail.
_HEADER_LINE_COUNT: int = 4

_MARKER_RELPATH: Tuple[str, str] = ("tasks", "orphan-sweep-notes.md")


def _rotate_text(old_text: str) -> Tuple[str, List[str]]:
    """Pure rotation core: return (new_text, tail_lines) for one file snapshot.

    ≤ _HEADER_LINE_COUNT lines (including the absent-file "" case) → unchanged
    text, empty tail — the no-op branch.  > _HEADER_LINE_COUNT lines → new text
    is the first 4 lines byte-for-byte (keepends slicing preserves the header's
    exact line endings), tail is every subsequent line with its line ending
    stripped (wire-friendly list entries).
    """
    lines_keepends = old_text.splitlines(keepends=True)
    if len(lines_keepends) <= _HEADER_LINE_COUNT:
        return old_text, []
    new_text = "".join(lines_keepends[:_HEADER_LINE_COUNT])
    tail = [line.rstrip("\r\n") for line in lines_keepends[_HEADER_LINE_COUNT:]]
    return new_text, tail


def _rotate_marker_file(worktree: Path) -> dict:
    """Sync: rotate <worktree>/tasks/orphan-sweep-notes.md under locked_rmw.

    Runs the full read-decide-write inside the cross-process lock so a
    concurrent boot_sweep append serialises against the rotation (never
    interleaved with the truncate-to-header swap).  missing_ok=True maps an
    absent file to the "" snapshot, which the ≤4-line predicate turns into the
    healthy-absence no-op.
    """
    target = worktree.joinpath(*_MARKER_RELPATH)
    captured: dict = {"tail": [], "kept": 0}

    def _mutate(old_text: str) -> str:
        new_text, tail = _rotate_text(old_text)
        captured["tail"] = tail
        captured["kept"] = len(new_text.splitlines())
        return new_text

    locked_rmw(target, _mutate, repo_root=worktree, missing_ok=True)

    rotated = bool(captured["tail"])
    return {
        "exit_code": 0,
        "rotated": rotated,
        "tail_surfaced": captured["tail"],
        "header_lines_kept": captured["kept"],
    }


def _build_error_result(reason: str) -> dict:
    """Build a setup/lock-error result (exit_code:1) — nothing was rotated."""
    _LOG.error("session.rotate_orphan_sweep_log: %s", reason)
    return {
        "exit_code": 1,
        "error": reason,
        "rotated": False,
        "tail_surfaced": [],
        "header_lines_kept": 0,
    }


@register_op("session.rotate_orphan_sweep_log")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.rotate_orphan_sweep_log — rotate the boot_sweep WARN-marker file.

    Reads tasks/orphan-sweep-notes.md under the caller's main worktree, and —
    when it has grown past its 4-line header — atomically rewrites it back to
    the header alone, returning the rotated-out marker lines.  The entire
    read-decide-write runs under locked_write.locked_rmw so concurrent
    boot_sweep appends serialise cleanly (A2 settlement).

    params:
      repo_root (str, optional): D3 consistency check only — NOT the path source.

    repo_root handler arg: the git common dir supplied by the engine
    (_OP_KEY_SCOPE = "common_dir", matching session.boot_sweep's entry).
    Worktree derived via main_worktree_root(common_dir) — the same
    GIT_ROOT-worktree keying boot_sweep's WARN-marker writer uses, so both ops
    always address the same file.

    Wire output:
      exit_code         int        0=ok (rotated or clean no-op), 1=setup/lock error
      rotated           bool       true iff the file was truncated back to its header
      tail_surfaced     list[str]  marker lines removed by the rotation ([] on no-op)
      header_lines_kept int        line count retained on disk (4 after a rotation;
                                   the existing ≤4 count on no-op; 0 when absent)
      error             str        present only on exit_code:1

    Idempotency (DEC-7): the ≤4-line no-op IS the mechanism — a successful
    rotation leaves exactly the 4-line header, so an identical second call
    fails the >4-line predicate and returns {rotated: false} by construction.
    """
    if repo_root is None:
        return _build_error_result("repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root

    # D3: optional repo_root consistency check (check only, never the path source).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return _build_error_result(mismatch)

    worktree = main_worktree_root(common_dir)

    # locked_rmw is a blocking lock-poll + file I/O — off the event loop (D4).
    try:
        return await asyncio.to_thread(_rotate_marker_file, worktree)
    except LockTimeout as exc:
        # Fail-closed: nothing was read or written; surface loudly (CC-7 —
        # never silently skip a rotation the caller believes happened).
        return _build_error_result(f"lock acquisition timed out: {exc}")
    except OSError as exc:
        return _build_error_result(f"rotation I/O failed: {exc}")
