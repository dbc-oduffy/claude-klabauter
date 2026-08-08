"""
coordinator_core.ops.reap_chain_ancestry_waivers — bounded-scope, fail-closed
deletion of chain-ancestry waiver files whose minting chain has itself reached
a TERMINAL "closed" disposition (`coordinator_core.chain_ancestry_waivers.
chain_reached_terminal_close`).

Purpose (§ Problem, measured): `state/review-trail/chain-ancestry-waivers/`
accumulates one permanent, idempotent per-(sha, chain_id) waiver file per
gate-minted waiver (868 tracked files across 60 chain directories at last
measurement) and grows without bound — nothing ever removes a waiver once its
minting chain is done needing it. `coverage_gate.py::_stage_chain_ancestry_
waivers`'s own docstring named the absence of a reaper explicitly and called
not fixing it deliberate; that acceptance is what the PM ruling behind this
plan reversed. This module is the reaper. W4 (next wave) corrects that
docstring to point here.

Fail-closed, REMOVE-ONLY (§ Anti-scope 24) — the single load-bearing safety
property of this module: it ONLY ever deletes a waiver file, or a chain
subdirectory once every waiver file under it is gone. It must NEVER write,
mint, merge, or edit a waiver record — that authority belongs solely to
`chain_ancestry_waivers.record_chain_ancestry_waiver` (the coverage gate at
HALT), and this module does not import or call it. Deleting the WRONG waiver
degrades to "commits stay uncredited, the coverage gate stays strict" —
never to wider crediting, because the read side
(`coverage.py::_narrow_foreign_session_scope` /
`chain_ancestry_waived_shas`) treats an absent waiver file identically to one
that was never minted. That asymmetry — a reap mistake can only make the gate
MORE conservative, never less — is the entire reason this tool is safe to
build and run, and is why it stops short of ever creating or altering a
record itself.

Idempotent: re-running against an already-reaped chain (its subdirectory
absent, or every remaining sibling chain already terminal with nothing left
to remove) reports zero removals and raises nothing — a no-op, not an error.
Re-running against a chain that still has waiver files but is NOT terminal
is also a no-op for that chain (reported under `skipped`), every single run,
until `chain_reached_terminal_close` itself reports terminal.

Single-chain-scoped per removal: this module iterates
`chain_ancestry_waivers.chain_root_dir(cwd)`'s immediate subdirectories, each
one exactly one `chain_id`'s own waiver directory. Every removal decision
(and every filesystem mutation) is made and applied against exactly ONE such
subdirectory at a time; nothing here ever globs, aggregates, or acts across
more than one chain directory in a single removal. A subdirectory name that
fails `chain_ancestry_waivers._CHAIN_ID_RE` (the same directory-name-safety
shape check `chain_waiver_dir` itself enforces) is NEVER touched — reported
under `skipped` with `reason: "invalid_chain_id"` and left exactly as found,
never opened, listed, or descended into beyond the one `is_dir()`/name check
needed to classify it.

Reports, never removes silently: `reap` returns
``{"removed": [...], "skipped": [...], "errors": [...]}`` — each `removed`
entry is ``{"chain_id": str, "file_count": int}`` (the count of waiver files
deleted for that chain, not merely "the directory is gone"), each `skipped`
entry is ``{"chain_id": str, "reason": str}``, and each `errors` entry is
``{"chain_id": str, "error": str}`` for an OSError encountered mid-removal
(the chain directory is left as far as the reap got — a partial removal is
never treated as fully reaped and never crashes the whole run). This is the
artifact a future audit row or an operator reads to see what was reaped
without re-deriving it from git history.

Read scope: `coordinator_core.chain_ancestry_waivers` — `chain_root_dir`,
`chain_reached_terminal_close`, and the module's own `_CHAIN_ID_RE` (read
directly rather than re-derived, per that module's own docstring naming this
copy as deliberately NOT shared/unified elsewhere). Write scope: THIS module
only — no edit to `chain_ancestry_waivers.py`'s read/write primitives; that
module's own anti-scope forbids it becoming a shared, parameterised store,
and this module consumes it, it does not extend it.

No git spawn is expected for the reap step itself — directory listing,
frontmatter/JSON reads (indirectly, via `chain_reached_terminal_close`'s own
call into the terminal-disposition classifier) and file removal are all pure
filesystem operations from this module's own vantage point. Windows
first-class throughout: only `pathlib.Path` operations are used for removal,
no shell-out.

DO NOT RUN THIS AGAINST THE LIVE TREE. `state/review-trail/chain-ancestry-
waivers/` in THIS repo holds real, tracked waiver files as of this writing.
Exercise `reap` against fixtures only.

Spec backlink: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-
amplification-gate.md § Tasks, row W2.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.chain_ancestry_waivers import (
    _CHAIN_ID_RE,
    chain_reached_terminal_close,
    chain_root_dir,
)
from coordinator_core.ipc import register_op


def reap_chain_ancestry_waivers(cwd: str) -> Dict[str, List[Dict[str, Any]]]:
    """Reap (delete) every waiver file under a chain-ancestry-waiver
    subdirectory whose minting `chain_id` has reached a TERMINAL "closed"
    disposition, then remove that now-empty subdirectory.

    Iterates `chain_root_dir(cwd)`'s immediate children only — never
    recurses beyond one level, never touches `chain_root_dir` itself.

    Returns ``{"removed": [...], "skipped": [...], "errors": [...]}`` — see
    the module docstring for each entry's shape. Always returns this shape,
    even when `chain_root_dir(cwd)` does not exist (idempotent no-op: empty
    `removed`/`skipped`/`errors`, no exception).
    """
    removed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    root = chain_root_dir(cwd)
    try:
        if not root.is_dir():
            return {"removed": removed, "skipped": skipped, "errors": errors}
        entries = sorted(root.iterdir())
    except OSError as exc:
        errors.append({"chain_id": None, "error": f"could not list {root}: {exc}"})
        return {"removed": removed, "skipped": skipped, "errors": errors}

    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError as exc:
            errors.append({"chain_id": entry.name, "error": str(exc)})
            continue
        if not is_dir:
            # Not a chain subdirectory (e.g. a stray file directly under
            # chain_root_dir) — never a reap target, per the single-
            # chain-subdirectory scope this module operates within.
            skipped.append({"chain_id": entry.name, "reason": "not_a_directory"})
            continue

        chain_id = entry.name
        if not _CHAIN_ID_RE.match(chain_id):
            # Directory-name-safety shape check failed — never opened,
            # listed, or descended into beyond this name check.
            skipped.append({"chain_id": chain_id, "reason": "invalid_chain_id"})
            continue

        if not chain_reached_terminal_close(cwd, chain_id):
            skipped.append({"chain_id": chain_id, "reason": "not_terminal"})
            continue

        removed_count = 0
        removal_error: Optional[str] = None
        try:
            files = sorted(entry.iterdir())
        except OSError as exc:
            errors.append({"chain_id": chain_id, "error": f"could not list {entry}: {exc}"})
            continue

        for waiver_file in files:
            try:
                if waiver_file.is_file():
                    waiver_file.unlink()
                    removed_count += 1
            except OSError as exc:
                removal_error = str(exc)
                break

        if removal_error is not None:
            errors.append({"chain_id": chain_id, "error": removal_error})
            continue

        try:
            entry.rmdir()
        except OSError as exc:
            # Waiver files are gone (removed_count reflects that), but the
            # directory itself could not be removed (e.g. a non-waiver
            # entry still present, or a transient OS-level lock) — report
            # both facts rather than hiding the partial state.
            errors.append({"chain_id": chain_id, "error": f"could not remove {entry}: {exc}"})
            if removed_count:
                removed.append({"chain_id": chain_id, "file_count": removed_count})
            continue

        removed.append({"chain_id": chain_id, "file_count": removed_count})

    return {"removed": removed, "skipped": skipped, "errors": errors}


@register_op("chain_ancestry_waivers.reap")
def _chain_ancestry_waivers_reap(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'chain_ancestry_waivers.reap' handler.

    Params:
        cwd (str, optional) — repo/worktree root to reap under. Defaults to
            `repo_root` (the per-request resolved repo root) when omitted;
            `repo_root` itself defaults to the current working directory if
            neither is available.

    DO NOT invoke this op against a live tree holding real waiver records —
    see the module docstring. Exercise via fixtures only.
    """
    cwd = params.get("cwd") if isinstance(params, dict) else None
    if not cwd:
        cwd = str(repo_root) if repo_root is not None else "."
    return reap_chain_ancestry_waivers(cwd)
