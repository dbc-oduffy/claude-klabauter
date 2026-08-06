"""
coordinator_core.ops.workday_stitch_sidecar_summary — JSON-RPC
"workday.stitch_sidecar_into_summary" operation.

Purpose: port of the ``commands/workday-complete.md:644`` fence — concatenate a
daily-observer sidecar file onto the day's main summary file, then delete the
sidecar. The shell original is a ``cat >> summary && rm sidecar`` pair, which
the oracle rates a HIGH idempotency hazard: that pair is not atomic, and a
crash between the two steps leaves either a duplicate append (on naive rerun)
or an orphaned sidecar (if the append never lands). ``locked_write.locked_rmw``
alone closes only the append half of that race (single-file RMW under a
cross-process flock) — it does not, by itself, make the two-file
append-then-delete TRANSACTION safe across a crash between them.

DEC-7 idempotency note (manifest-flagged high idempotency-hazard row —
op-classification.tsv, stitch-sidecar-into-summary-file):

    The append is made CONTENT-IDEMPOTENT rather than relying on locked_rmw's
    byte-identical-skip alone to do the work. ``mutate()`` checks whether the
    caller-supplied ``marker`` is already present in the summary's current
    text; if so it returns ``old_text`` UNCHANGED, which turns locked_rmw's
    step-6 byte-identical comparison into a genuine zero-write no-op. Only
    then does this op delete the sidecar (delete-if-exists is already
    idempotent — unlinking a file that is already gone is a no-op, not an
    error).

    This closes the crash window the oracle names: if the process dies AFTER
    the append lands but BEFORE the delete runs, the sidecar file is still on
    disk. A rerun re-enters this op, reads the summary, finds ``marker``
    already present, skips the append entirely (no duplicate), and proceeds
    straight to deleting the now-stale sidecar — completing the transaction
    rather than repeating half of it. See
    ``test_crash_between_append_and_delete_does_not_duplicate_on_rerun`` for
    the mechanized proof (this file simulates the crash by calling the
    append-only mutate path directly, without invoking the delete step, then
    re-invokes the full handler and asserts single-occurrence + cleanup).

    Precondition this invariant depends on: ``marker`` must actually be a
    substring of ``sidecar_path``'s content at read time (typically embedded
    by whatever generates the sidecar — a unique per-day heading or comment).
    This op verifies that precondition explicitly and fails loud (exit_code 1,
    no write) rather than appending content that would make the marker check
    meaningless on a future rerun — see Negative-spec.

Registration: importing this module fires
``@register_op("workday.stitch_sidecar_into_summary", _handler)`` as a
side-effect. A serial tail pass wires this module into
``coordinator_core/ops/__init__.py::_EAGER_OP_MODULES``,
``coordinator_core/op_scopes.py::_OP_KEY_SCOPE`` (this op is ``common_dir`` —
see op-classification.tsv), and ``_registry_map.py`` — this module does not
touch those shared surfaces itself.

Spec backlink:
  docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
  state/audits/2026-07-22-command-payload-inventory/op-classification.tsv
    (row: stitch-sidecar-into-summary-file)
  state/audits/2026-07-22-command-payload-inventory/distinct-ops-new.tsv
    (row: stitch-sidecar-into-summary-file, commands/workday-complete.md:644)

Contract (op-classification.tsv, pinned):
    params: {summary_path: str, sidecar_path: str, marker: str}
    -> {stitched: bool, sidecar_removed: bool}

Scope verdict: ``common_dir`` — the daily summary and its sidecar live in the
CALLER's main-worktree-rooted ``state/week-changelog/`` tree, shared across
any linked worktree of that repo (op-classification.tsv). ``repo_root`` is
therefore required; the worktree is derived via
``coordinator_core.ops.fleet._common.main_worktree_root`` — never inlined.

Negative-spec:
  - Does NOT resolve MACHINE or perform any machine-identity lookup — the
    oracle fence's machine-resolution preamble is unrelated plumbing around
    the actual two-step op and is out of this op's scope by construction.
  - Does NOT accept an absolute or relative path outside
    ``<worktree>/state/week-changelog/`` for either ``summary_path`` or
    ``sidecar_path`` — both are containment-checked via
    ``coordinator_core.ops._path_guard.contained_path``.
  - Does NOT append when ``sidecar_path`` does not exist on disk — that is
    reported as a no-op (``stitched: False, sidecar_removed: False``), not an
    error, since it is indistinguishable from "already fully stitched and
    cleaned up by a prior successful call" (or "no sidecar was ever
    produced today").
  - Does NOT append silently when ``marker`` is not found within the
    sidecar's own content — that would make the content-idempotence
    invariant this op depends on unenforceable on a future rerun, so this op
    fails loud (exit_code 1, no write, sidecar untouched) instead.
  - Does NOT git-commit. Pure file mutation only (append + delete).
  - Does NOT retry indefinitely on file lock contention — surfaces
    ``LockTimeout`` as an exit_code 1 error, same as sibling locked_rmw ops.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("workday.stitch_sidecar_into_summary")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "workday.stitch_sidecar_into_summary" handler.

    Port of the ``commands/workday-complete.md:644`` ``cat >> summary && rm
    sidecar`` fence, made crash-safe per the module docstring's DEC-7 note:
    the append is content-idempotent (marker-checked) so a crash between the
    append and the delete resolves cleanly on rerun instead of duplicating.

    Params:
        summary_path (str) — path (absolute, or relative to the caller's main
                             worktree root) to the day's summary file being
                             appended to. Required. Created if it does not yet
                             exist (locked_rmw's ``missing_ok=True``).
        sidecar_path (str) — path (absolute, or relative to the caller's main
                             worktree root) to the sidecar file being
                             concatenated onto ``summary_path`` and then
                             deleted. Required.
        marker       (str) — a substring expected to be present verbatim in
                             ``sidecar_path``'s content. Used both (a) to
                             detect "already stitched" on the SUMMARY side
                             (skip the append if already present — the
                             content-idempotence invariant) and (b) as a
                             precondition check on the SIDECAR side (refuse
                             to append if the marker is not actually part of
                             the sidecar's own content, since that would make
                             (a) meaningless on a future rerun). Required,
                             non-empty.

    Both paths are containment-checked against
    ``<worktree>/state/week-changelog/`` — see module docstring, Scope
    verdict.

    Returns a dict with keys:
        exit_code       (int)  — 0 ok / 1 error.
        stitched        (bool) — True if, at the end of this call, ``marker``
                                 is present in the summary file (whether
                                 freshly appended by this call or already
                                 present from a prior call). False when
                                 ``sidecar_path`` did not exist at call time
                                 (nothing to stitch), or on error.
        sidecar_removed (bool) — True if, at the end of this call, the
                                 sidecar file is gone (whether deleted by
                                 this call or already absent). False when
                                 ``sidecar_path`` did not exist at call time,
                                 or on error.
        message         (str)  — present on exit_code 0; human-readable
                                 outcome description.
        error           (str)  — present on exit_code 1; human-readable
                                 reason.

    Exit-code contract:
        exit_code 1 — missing/empty param; repo_root missing; either path
                      escapes ``state/week-changelog/``; marker not found in
                      sidecar content; malformed summary read/write I/O
                      error; lock timeout.
        exit_code 0 — sidecar not found (no-op); marker already present in
                      summary and sidecar deleted (idempotent-rerun
                      completion); or fresh append + delete.

    Negative-spec: see module docstring.
    """
    summary_path_raw: str = params.get("summary_path") or ""
    sidecar_path_raw: str = params.get("sidecar_path") or ""
    marker: str = params.get("marker") or ""

    if not summary_path_raw:
        return _err("missing required param: summary_path")
    if not sidecar_path_raw:
        return _err("missing required param: sidecar_path")
    if not marker:
        return _err("missing required param: marker")

    if repo_root is None:
        return _err(
            "workday.stitch_sidecar_into_summary: repo_root is required "
            "(no founding root available — handler called without "
            "socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    summary_p = Path(summary_path_raw)
    if not summary_p.is_absolute():
        summary_p = worktree / summary_p
    sidecar_p = Path(sidecar_path_raw)
    if not sidecar_p.is_absolute():
        sidecar_p = worktree / sidecar_p

    allowed_roots = [worktree / "state" / "week-changelog"]

    summary_p = contained_path(summary_p, allowed_roots)
    if summary_p is None:
        return _err(
            f"summary_path escapes state/week-changelog/: {summary_path_raw!r}"
        )
    sidecar_p = contained_path(sidecar_p, allowed_roots)
    if sidecar_p is None:
        return _err(
            f"sidecar_path escapes state/week-changelog/: {sidecar_path_raw!r}"
        )

    if not sidecar_p.is_file():
        # Nothing to stitch — either this is the normal no-sidecar-today case,
        # or a prior call already completed the full transaction (append +
        # delete) and this is a redundant rerun. Both are safe no-ops.
        _LOG.debug(
            "workday.stitch_sidecar_into_summary: sidecar not found — no-op: %s",
            sidecar_p,
        )
        return {
            "exit_code": 0,
            "stitched": False,
            "sidecar_removed": False,
            "message": (
                f"sidecar not found — nothing to stitch: {sidecar_path_raw}"
            ),
        }

    try:
        sidecar_text = sidecar_p.read_text(encoding="utf-8")
    except OSError as exc:
        return _err(f"cannot read sidecar file: {exc}")

    if marker not in sidecar_text:
        return _err(
            f"marker {marker!r} not found in sidecar content ({sidecar_path_raw}) — "
            "refusing to append: the content-idempotence invariant this op "
            "relies on for crash-safety requires marker to be part of the "
            "appended content"
        )

    _applied = [False]

    def _mutate(old_text: str) -> str:
        if marker in old_text:
            # Already stitched — return unchanged so locked_rmw's
            # byte-identical check skips the write (genuine zero-write
            # no-op). This is the crash-window-rerun safety net: if a prior
            # call appended but crashed before deleting the sidecar, this
            # branch fires on rerun instead of appending a duplicate.
            return old_text
        separator = "" if (old_text == "" or old_text.endswith("\n")) else "\n"
        _applied[0] = True
        return f"{old_text}{separator}{sidecar_text}"

    try:
        await asyncio.to_thread(
            locked_rmw, summary_p, _mutate, repo_root=repo_root, missing_ok=True
        )
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _err(f"cannot read/write summary file: {exc}")

    try:
        sidecar_p.unlink()
        sidecar_removed = True
    except FileNotFoundError:
        # Benign race: something else removed it between our is_file() check
        # and this unlink. The end state (no sidecar) is what we wanted.
        sidecar_removed = True
    except OSError as exc:
        _LOG.warning(
            "workday.stitch_sidecar_into_summary: sidecar stitched but delete "
            "failed: %s (%s)", sidecar_p, exc,
        )
        sidecar_removed = False

    if _applied[0]:
        message = (
            f"stitched sidecar into {summary_path_raw} and removed "
            f"{sidecar_path_raw}" if sidecar_removed else
            f"stitched sidecar into {summary_path_raw}; sidecar delete failed "
            f"(will retry): {sidecar_path_raw}"
        )
    else:
        message = (
            f"marker already present in {summary_path_raw} — skipped append "
            f"(idempotent); removed stale sidecar {sidecar_path_raw}"
            if sidecar_removed else
            f"marker already present in {summary_path_raw} — skipped append "
            f"(idempotent); sidecar delete failed (will retry): {sidecar_path_raw}"
        )

    _LOG.info("workday.stitch_sidecar_into_summary: %s", message)
    return {
        "exit_code": 0,
        "stitched": True,
        "sidecar_removed": sidecar_removed,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Error-shape helper
# ---------------------------------------------------------------------------


def _err(msg: str) -> dict:
    """Return an exit_code=1 error reply dict."""
    _LOG.warning("workday.stitch_sidecar_into_summary: %s", msg)
    return {
        "exit_code": 1,
        "stitched": False,
        "sidecar_removed": False,
        "error": msg,
    }
