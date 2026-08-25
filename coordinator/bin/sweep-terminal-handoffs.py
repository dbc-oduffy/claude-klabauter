# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""sweep-terminal-handoffs.py — capped CLI fire for fleet.archive_completed_handoffs.

Purpose: on-demand + detached-fire trigger over the native
`fleet.archive_completed_handoffs` op (coordinator_core/ops/fleet/
archive_terminal_handoffs.py). Unlike its predecessor
(`sweep-shipped-handoffs.py`, deleted 2026-08-25 C1b when the op it fired,
`fleet.archive_shipped_handoffs`, was SUBSUMED into this op), this script
does NOT re-implement any scanning/frontmatter/terminality logic of its
own — the rebuilt op does its own Branch A/B classification, DR-324-narrowed
childlessness check, and live-claim check internally (see that module's
`_scan_terminal`). This script is a thin two-phase (T1 preview -> T3 act)
caller, exactly the confirm-then-act shape
`coordinator_core.ops.ceremony.tail_ops.run_fleet_op_two_phase` already
uses in-process for the SAME op key — this CLI exists only because
`fire_archive_sweeps_detached` needs an on-disk script to spawn detached,
not an in-process awaitable.

Cap: this op takes a REQUIRED per-invocation move cap (absent/non-positive
is a setup error — no unbounded default, per the plan's C0 cap-axis
decision, state/audits/2026-08-25-the-handoff-archive-op-earns-its-way-
back.md § C0). This script passes the op module's own recommended value,
`_RECOMMENDED_CAP_CHOICE` (150 as of this writing) — see
coordinator_core/ops/fleet/archive_terminal_handoffs.py for the live value
and its own rationale; this script does not re-derive or duplicate that
rationale, only cites the constant by name so the two never silently drift
apart unnoticed (a future bump there is expected to be echoed here).

Usage:
    python3 sweep-terminal-handoffs.py

Exit codes:
    0 — normal (including zero-candidates, all-retained/contended, and a
        fully successful dispatch).
    1 — the underlying fleet.archive_completed_handoffs dispatch failed:
        either a caught transport RuntimeError (no bash fallback) or the op
        itself reporting exit_code == 2 (partial failure). Candidates are
        retained for the next sweep either way.
    2 — internal error (not inside a git repo).

Big-bang cutover (2026-07-19 Windows de-bash campaign, Wave F1): no legacy
bash fallback — a genuinely seam-absent install surfaces as a transport
failure (RuntimeError), caught below and logged (best-effort ceremony).

Liveness stamp: mirrors the retired predecessor's own liveness contract --
every completion that reaches the sweep-processing tail (exit 0, including
zero-candidates/all-retained/contended, AND exit 1, dispatch failure)
stamps the shared `archive_sweeps` housekeeping-liveness key
(`coordinator_core.ops.ceremony.housekeeping_liveness.stamp_liveness`).
The internal-error path (exit 2 -- not a git repo) returns before reaching
the tail and never stamps.

Index-lock disposition (staff-eng F3, C4): this script's ``fleet.archive_
completed_handoffs`` dispatch is the ONLY tracked-worktree-mutating call this
CLI makes -- it never runs `git add`/`git commit` itself. The op's own act
path (`coordinator_core.ops.fleet.archive_terminal_handoffs._handle_act` ->
`coordinator_core.ops.fleet._common.archive_and_commit`) already carries a
bounded exponential-backoff retry against transient `.git/index.lock`
contention (`_update_index_with_retry` / `_INDEX_RETRY_*`, sized against an
empirical repro) -- this script adds no second retry layer of its own, it
reuses the op's. `_ARCHIVE_SWEEP_SCRIPTS` (coordinator_core/ops/ceremony/
tail_ops.py) now carries exactly ONE detached self-committing member again
(the sibling `sweep-shipped-handoffs.py` this seam used to share the loop
with was deleted outright, C1b) -- the ORIGINAL two-member `.git/index.lock`
hazard that module's docstring records cannot recur from two members of
THIS loop, because there is only one; what CAN still happen on a
~50-concurrent-session box is this op's commit racing an UNRELATED peer's
own git operation on the same worktree, which is exactly the transient
contention `_update_index_with_retry`'s backoff is sized to absorb.

Spec backlink: docs/plans/2026-08-25-the-handoff-auto-archive-comes-back-capped.md § C4
Wraps: fleet.archive_completed_handoffs (native op, bulk primitive)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import cc_invoke  # noqa: E402  # pyright: ignore[reportMissingImports] — added to sys.path at runtime by the _LIB_DIR injection above, not statically resolvable
from repo_identity import resolve_checked_repo_root  # noqa: E402  # pyright: ignore[reportMissingImports] — same runtime _LIB_DIR sys.path injection as cc_invoke above

# Mirrors coordinator_core/ops/fleet/archive_terminal_handoffs.py's own
# `_RECOMMENDED_CAP_CHOICE` -- see module docstring "Cap" section for why
# this is a cited literal, not an import of that module (this script has
# no other reason to import the op module directly; cc_invoke.route()
# resolves and registers it on the engine side).
_CAP = 150

_OP_KEY = "fleet.archive_completed_handoffs"


def _ensure_claude_klabauter_on_path() -> str:
    """Idempotently put the engine root on sys.path; returns it.

    The file's ONE claude-klabauter-root path-resolution site, mirroring the retired
    predecessor's own `_ensure_claude_klabauter_on_path` helper.
    """
    return cc_invoke.require_engine_on_path(__file__)


def _import_housekeeping_seam():
    """Resolve the engine root and import `housekeeping_liveness.{stamp_liveness,ARCHIVE_SWEEPS}`.

    Best-effort; returns None on any resolution/import failure -- mirrors
    the retired predecessor's own seam import.
    """
    try:
        _ensure_claude_klabauter_on_path()
        from coordinator_core.ops.ceremony.housekeeping_liveness import (
            ARCHIVE_SWEEPS,
            stamp_liveness,
        )
    except Exception:  # noqa: BLE001 -- best-effort; never let seam-import failure mask the real error
        return None
    return stamp_liveness, ARCHIVE_SWEEPS


def _stamp_archive_sweeps_liveness(repo_root: str) -> None:
    """Best-effort stamp the shared `archive_sweeps` housekeeping-liveness key.

    Called from the sweep-processing tail only (never on the internal-error exit).
    """
    seam = _import_housekeeping_seam()
    if seam is None:
        return
    stamp_liveness, archive_sweeps = seam
    try:
        stamp_liveness(repo_root, archive_sweeps)
    except Exception:  # noqa: BLE001 -- never raise out of a best-effort liveness stamp
        pass


def _no_fallback() -> None:
    raise RuntimeError(
        "fleet.archive_completed_handoffs: native seam required (no bash fallback -- big-bang cutover)"
    )


def main(_argv: "list[str] | None" = None) -> int:
    """`_argv` is unused: this script owns no argv-parsed options and delegates
    everything to `fleet.archive_completed_handoffs` via `cc_invoke.route()`, a
    two-phase (T1 preview -> T3 act) confirm-then-act dance. Kept for call
    signature conformance with the sibling sweep-script test harnesses, which
    call `mod.main(argv if argv is not None else [])` uniformly."""
    git_repo_root, verdict = resolve_checked_repo_root(explicit_root=None)
    if git_repo_root is None:
        print("sweep-terminal-handoffs.py: not inside a git repo", file=sys.stderr)
        return 2
    if verdict["verdict"] == "MISMATCH":
        print(verdict["message"], file=sys.stderr)

    repo_root = git_repo_root
    dispatch_failed = False
    archived = 0
    contended = False

    try:
        preview = cc_invoke.route(
            _OP_KEY,
            {"mode": "already-terminal", "dry_run": True, "cap": _CAP},
            repo_root,
            _no_fallback,
        )
    except RuntimeError as exc:
        print(f"sweep-terminal-handoffs.py: preview dispatch failed: {exc}", file=sys.stderr)
        dispatch_failed = True
        preview = {}

    preview_exit = preview.get("exit_code", 0) if isinstance(preview, dict) else 0
    if not dispatch_failed:
        if isinstance(preview, dict) and preview.get("contended"):
            contended = True
        elif preview_exit != 0:
            print(
                f"sweep-terminal-handoffs.py: preview exit_code={preview_exit}",
                file=sys.stderr,
            )
            dispatch_failed = True
        else:
            candidates = preview.get("candidates", []) if isinstance(preview, dict) else []
            candidate_ids = [
                (c.get("id") if isinstance(c, dict) else c) for c in candidates
            ]
            candidate_ids = [c for c in candidate_ids if c]

            if candidate_ids:
                try:
                    act = cc_invoke.route(
                        _OP_KEY,
                        {
                            "mode": "already-terminal",
                            "dry_run": False,
                            "cap": _CAP,
                            "candidate_ids": candidate_ids,
                        },
                        repo_root,
                        _no_fallback,
                    )
                except RuntimeError as exc:
                    print(f"sweep-terminal-handoffs.py: act dispatch failed; candidates retained: {exc}", file=sys.stderr)
                    dispatch_failed = True
                else:
                    if isinstance(act, dict) and act.get("contended"):
                        contended = True
                    else:
                        acted = act.get("acted", []) if isinstance(act, dict) else []
                        archived = len(acted) if isinstance(acted, list) else 0
                        op_exit = act.get("exit_code", 0) if isinstance(act, dict) else 0
                        if op_exit == 2:
                            print(
                                f"sweep-terminal-handoffs.py: WARN: {_OP_KEY} partial "
                                f"(exit_code=2, acted={archived}) -- check claude-klabauter logs",
                                file=sys.stderr,
                            )
                            dispatch_failed = True

    if contended:
        print("sweep-terminal-handoffs.py: sweep lock contended -- retained for next sweep")
    elif archived == 0:
        print("no terminal handoffs archived")
    else:
        print(f"{archived} terminal handoffs archived")

    _stamp_archive_sweeps_liveness(repo_root)
    if dispatch_failed:
        return 1
    return 0


if __name__ == "__main__":
    _ensure_claude_klabauter_on_path()
    from coordinator_core.cli_entry import recording_declared_writes

    with recording_declared_writes():
        _exit_code = main()
    sys.exit(_exit_code)
