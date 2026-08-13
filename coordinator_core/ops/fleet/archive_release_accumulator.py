"""
coordinator_core.ops.fleet.archive_release_accumulator — fleet.archive_release_accumulator op handler.

Purpose: git-mv the most recent state/week-changelog/*-pending-release.md
"accumulator" file into archive/release-notes/ under a tag-suffixed name, if one
exists.  Ports the merging-to-main SKILL.md ("Archive the pending-release
accumulator, if one exists (best-effort, non-gating)") shell step to a named,
registered op — no new move/rename logic: this is a thin candidate-discovery
layer over ops/fleet/_common.py::archive_and_commit, which already implements
the DR-211 D3/D4 git-mv + scoped-pathspec commit.

Self-registration: @register_op fires at import time.  coordinator_core/ops/__init__.py
imports this module so the registration fires at start_server().

Response shape (contract — see state/audits/2026-07-22-command-payload-inventory/
op-classification.tsv row archive-accumulator-file-git-mv):
    {"archived": bool, "dest": str | None, "already_archived": bool}

Tri-state encoding (only two booleans on the wire, three outcomes):
    archived=True,  already_archived=False -> this call moved the accumulator; dest set.
    archived=False, already_archived=True  -> no accumulator file found (never existed,
                                               or a prior call already moved it) — vacuous
                                               no-op, dest is None.
    archived=False, already_archived=False -> an accumulator file WAS found but the git-mv
                                               attempt failed (dry_run:false only); dest is
                                               None, reason logged daemon-side (best-effort,
                                               non-gating per the SKILL.md source step — a
                                               failure here never raises).

dry_run:true is a preview: never mutates, always reports archived=False.  When an
accumulator is found it reports the dest it WOULD move to (already_archived=False);
when none is found it reports already_archived=True — same predicate act would use.

Idempotency (AC7, oracle-rated low): a second dry_run:false call with identical
params after the accumulator has already been moved finds no source file on disk
(Path.exists() check, not a presence-of-variable check) and returns the vacuous
no-op shape above rather than raising on a missing git-mv source.

Spec backlinks:
  - Plan: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Wave 1 C1b
  - Fence source: coordinator/skills/merging-to-main/SKILL.md § Step 5 "Archive the
    pending-release accumulator" (coordinator-claude tree; cross-repo, read-only reference)
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D1-D4, five bounds)

Negative-spec:
  - Does NOT implement its own git-mv/commit logic — delegates entirely to
    archive_and_commit (DEC in plan § Wave 1 C1b: "Write zero new move/rename logic").
  - Does NOT use blocking subprocess — archive_and_commit is the only git call site
    and it is async throughout (DR-211 D4).
  - Does NOT use params.repo_root as the worktree source — worktree is derived via
    main_worktree_root(common_dir), the engine-supplied socket-authoritative value.
  - Does NOT treat a git-mv failure as fatal — this step is best-effort/non-gating
    per its SKILL.md source; a failure returns the failure-shaped envelope and logs,
    it never raises out of the handler.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    Move,
    archive_and_commit,
    check_repo_root,
    main_worktree_root,
    rel_id,
)

_LOG = logging.getLogger(__name__)

_ACCUMULATOR_GLOB = "*-pending-release.md"
_ACCUMULATOR_SUFFIX = "-pending-release.md"


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------

def _find_accumulator(worktree_root: Path) -> Optional[Path]:
    """Return the most recent state/week-changelog/*-pending-release.md, or None.

    "Most recent" is the lexicographically-last match: accumulator filenames are
    date-prefixed (YYYY-MM-DD-pending-release.md), so sorted() order is chronological.
    Returns None when the directory is absent or holds no matching file — this is
    the disk-truth check (Path.exists()-shaped) that closes the idempotency hazard:
    a rerun after the file has already been moved simply finds nothing here.
    """
    changelog_dir = worktree_root / "state" / "week-changelog"
    if not changelog_dir.is_dir():
        return None
    candidates = sorted(changelog_dir.glob(_ACCUMULATOR_GLOB))
    return candidates[-1] if candidates else None


def _dest_for(accumulator: Path, worktree_root: Path, tag: str) -> Path:
    """Derive the archive/release-notes/ destination for an accumulator file.

    Mirrors the SKILL.md shell step exactly: strip the trailing
    "-pending-release.md" from the basename, re-append "-<tag>-pending-release.md".
    """
    stem = accumulator.name
    if stem.endswith(_ACCUMULATOR_SUFFIX):
        stem = stem[: -len(_ACCUMULATOR_SUFFIX)]
    return worktree_root / "archive" / "release-notes" / f"{stem}-{tag}-pending-release.md"


def _result(archived: bool, dest: Optional[str], already_archived: bool) -> dict:
    """Assemble the frozen three-field response envelope."""
    return {"archived": archived, "dest": dest, "already_archived": already_archived}


def _setup_error(reason: str) -> dict:
    """Log a setup/validation error and return the three-field failure envelope.

    This op's wire contract is {archived, dest, already_archived} only — it does
    NOT share the batch fleet.* envelope (mode/candidates/acted/skipped/failed),
    so setup errors here are NOT routed through _common.build_setup_error_result,
    which would return a shape this op never otherwise emits.  The reason is
    logged daemon-side and written to stderr (mirrors _common._setup_error's
    dual-channel rationale: a bare dict has no room for a top-level reason field
    without expanding the frozen envelope).
    """
    _LOG.error("fleet.archive_release_accumulator setup error: %s", reason)
    print(f"fleet.archive_release_accumulator setup error: {reason}", file=sys.stderr, flush=True)
    return _result(archived=False, dest=None, already_archived=False)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_op("fleet.archive_release_accumulator")
async def _handler(params: dict, repo_root=None) -> dict:
    """fleet.archive_release_accumulator — archive the pending-release accumulator file.

    params: {repo_root: str (optional, D3 consistency check), tag: str (required),
             dry_run: bool (required)}
    returns: {archived: bool, dest: str | None, already_archived: bool}

    repo_root arg: the git common dir delivered by _OP_KEY_SCOPE="common_dir".
                   Handlers MUST NOT use ctx.repo_root (None in the global service).
                   Worktree root is derived via main_worktree_root(repo_root).
                   params.repo_root is the D3 consistency check only — NOT the
                   worktree-root resolution source.
    """
    tag = params.get("tag")
    dry_run = params.get("dry_run")

    if not isinstance(tag, str) or not tag:
        return _setup_error(f"tag must be a non-empty str, got {tag!r}")
    if not isinstance(dry_run, bool):
        return _setup_error(f"dry_run must be bool, got {type(dry_run).__name__!r}")

    if repo_root is not None:
        common_dir = Path(repo_root)
        mismatch = check_repo_root(params.get("repo_root"), common_dir)
        if mismatch:
            return _setup_error(mismatch)
        worktree = main_worktree_root(common_dir)
    else:
        # repo_root is None only in unit tests that bypass the keying table.
        # Fail loudly in production; test fixtures supply an explicit value.
        return _setup_error(
            "repo_root is None; cannot derive worktree root (keying-table misconfiguration) — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production"
        )

    accumulator = _find_accumulator(worktree)
    if accumulator is None or not accumulator.exists():
        # Vacuous no-op: never existed, or a prior call already moved it.
        return _result(archived=False, dest=None, already_archived=True)

    dest = _dest_for(accumulator, worktree, tag)

    if dry_run:
        return _result(archived=False, dest=rel_id(dest, worktree), already_archived=False)

    candidate_id = rel_id(accumulator, worktree)
    moves: List[Move] = [Move(src=accumulator, dst=dest, candidate_id=candidate_id)]
    commit_subject = f"release: archive pending-release accumulator for {tag} [fleet.archive_release_accumulator]"
    acted, failed = await archive_and_commit(worktree, moves, commit_subject)

    if acted:
        return _result(archived=True, dest=rel_id(dest, worktree), already_archived=False)

    # Best-effort/non-gating per the SKILL.md source step: log and report the
    # failure shape rather than raising.
    if failed:
        _LOG.error(
            "fleet.archive_release_accumulator: git-mv failed for %s -> %s: %s",
            accumulator, dest, failed[0].get("reason"),
        )
    return _result(archived=False, dest=None, already_archived=False)
