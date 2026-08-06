"""
coordinator_core.handoff_creation_guard — invariant guard: a handoff whose
identity (filename or ``handoff_id`` frontmatter scalar) already exists under
``archive/handoffs/`` must never be (re)created at a live ``state/handoffs/``
path.

Purpose: close the upstream half of the live/archive handoff duplication hole
documented in ``state/audits/2026-07-26-handoff-live-archive-duplication-
origin.md`` (example-doctrine-repo). That investigation ruled out archival itself, the
stale-index restore fix, and the DR-084 migration's own write primitive as
the creator of a duplicate, and narrowed the remaining candidates to
unconditional-create writers reachable in a shared working tree. It could not
pin the exact writer with certainty, so this guard enforces the INVARIANT at
every known creation seam instead of chasing the specific culprit — the hole
closes regardless of which writer would have opened it.

Enforced at three call sites (the enumeration this module's own audit
produced — see the dispatching sidecar for the full trace):
  - ``coordinator_core.ops.handoff_author_fork`` (``handoff.author_fork`` op)
  - ``coordinator_core.ops.queue_scaffold_baton`` (``handoff.scaffold_from_queue`` op)
  - ``coordinator/bin/coordinator-doc-new`` (``--type handoff|recovery|spinoff|
    spinoff-roadmap`` CLI scaffold path)

These are the only writers found that can materialize a NEW file at a
``state/handoffs/`` path (every other handoff writer routes through
``locked_write.locked_rmw`` WITHOUT ``missing_ok=True``, which requires the
target to already exist — see that module's ``missing_ok`` docstring).

Two independent match bases, either one sufficient to trip the guard:
  - filename: the target's basename already exists somewhere under
    ``archive/handoffs/**`` — this is the literal shape of the confirmed
    incident (``state/handoffs/<f>.md`` alongside ``archive/handoffs/
    2026-07/<f>.md``, same ``<f>.md``).
  - ``handoff_id``: the target's (optionally caller-supplied) ``handoff_id``
    frontmatter scalar already exists on an archived record — the durable,
    path-independent identity ``docs/plans/2026-07-08-lifecycle-vocab-c2-
    durable-links-rollup.md`` § C1 introduced, and the same basis
    ``coordinator/tests/test_handoff_id_uniqueness.py`` (example-doctrine-repo) checks
    after the fact.

Negative-spec: no ``force``/escape parameter. There is no legitimate reason
to create a NEW live handoff sharing an archived record's filename or
handoff_id — continuing archived work always mints a FRESH handoff_id and
filename and cites the archived record via ``predecessor``/
``origin_handoff_id`` (the standing continuation mechanism), it never needs
to reuse the archived record's own identity. An operator who deliberately
wants an archived record back on the live path performs an explicit
``git mv archive/handoffs/... state/handoffs/...`` themselves — that path
does not go through any of this guard's call sites, so it is untouched by
this module. The ``repair-archived-*`` verbs (``archive_stamp.py``'s
``cs_repair_archived_shipped_in``/``cs_repair_archived_deployment_state``)
mutate an ALREADY-EXISTING archived record in place via ``locked_rmw``
without ``missing_ok=True`` — they never create a live-path file, so they
are unaffected by (and do not need to route through) this guard.

Spec backlink: state/subagent-share/41c1917d-53d5-49f9-9e70-cf281768cc5d/
coordinatorexecutor-953c07e8.md (dispatch brief: "Close a hole whose exact
cause could not be pinned").
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Optional, Union

from coordinator_core.ops._fm_util import extract_frontmatter_scalar

PathLike = Union[str, os.PathLike]


class HandoffArchivedTwinError(RuntimeError):
    """Raised when a handoff-creation write would resurrect an already-archived record."""


def _archive_handoffs_dir(repo_root: PathLike) -> Path:
    return Path(repo_root) / "archive" / "handoffs"


def find_archived_twin_by_filename(target_path: PathLike, repo_root: PathLike) -> Optional[Path]:
    """Return the ``archive/handoffs/**/<basename>`` path sharing ``target_path``'s
    basename, or ``None`` if no such file exists.

    Checks both the sharded ``archive/handoffs/<YYYY-MM>/<basename>`` layout
    (the normal case) and a direct ``archive/handoffs/<basename>`` (defensive
    — tolerates an unsharded archive tree).
    """
    basename = Path(target_path).name
    archive_dir = _archive_handoffs_dir(repo_root)
    if not archive_dir.is_dir():
        return None
    direct = archive_dir / basename
    if direct.is_file():
        return direct
    for candidate in sorted(archive_dir.glob("*/" + basename)):
        if candidate.is_file():
            return candidate
    return None


def find_archived_twin_by_handoff_id(handoff_id: Optional[str], repo_root: PathLike) -> Optional[Path]:
    """Return an ``archive/handoffs/`` record whose ``handoff_id`` frontmatter
    scalar matches ``handoff_id``, or ``None`` when absent/not found.

    Shared match basis with ``reap-orphaned-in-flight-handoffs.py``'s
    ``_handoff_id_archived_twin`` -- that CLI-local helper (added first, at
    the reaper/resurrection-prevention layer) now delegates to this function
    via an in-process CLAUDE_KLABAUTER_ROOT import trampoline rather than carrying its
    own duplicate glob-and-compare loop, so the two guards -- this module's
    RAISE-on-match creation guard and the reaper's SKIP-on-match resurrection
    guard -- cannot silently drift apart on what counts as a "twin". Only the
    reaction to a match stays call-site-local; the predicate is now singular.
    """
    if not handoff_id:
        return None
    archive_dir = _archive_handoffs_dir(repo_root)
    if not archive_dir.is_dir():
        return None
    for pattern in ("*.md", os.path.join("*", "*.md")):
        for candidate in glob.glob(str(archive_dir / pattern)):
            if not os.path.isfile(candidate):
                continue
            try:
                text = Path(candidate).read_text(encoding="utf-8")
            except OSError:
                continue
            if extract_frontmatter_scalar(text, "handoff_id") == handoff_id:
                return Path(candidate)
    return None


def assert_no_archived_twin(
    target_path: PathLike,
    repo_root: PathLike,
    handoff_id: Optional[str] = None,
) -> None:
    """Fail loud if ``target_path`` (a live handoff file about to be CREATED)
    shares a filename or ``handoff_id`` with an already-archived record.

    Call this BEFORE any write to ``target_path`` — before acquiring a
    ``locked_rmw`` lock, before ``open(target_path, "w")``. Raises
    ``HandoffArchivedTwinError`` naming the archived twin's path and the
    corrective action; never silently skips or proceeds.

    Negative-spec: does NOT check anything when ``target_path`` itself
    already exists on the live path (that is an ordinary RMW/overwrite,
    outside this guard's remit — this guard is about CREATION only).
    """
    target = Path(target_path)
    root = Path(repo_root)
    twin = find_archived_twin_by_filename(target, root)
    match_basis = "filename"
    if twin is None and handoff_id:
        twin = find_archived_twin_by_handoff_id(handoff_id, root)
        match_basis = "handoff_id"
    if twin is not None:
        raise HandoffArchivedTwinError(
            f"refusing to create {target}: an archived record already exists at "
            f"{twin} (same {match_basis}). A handoff whose identity already exists "
            "under archive/handoffs/ must never be recreated on the live path. "
            "If you intend to revive this work: `git mv` the archived record back "
            "to state/handoffs/ explicitly (a deliberate un-archive), or author a "
            "NEW handoff (fresh handoff_id/filename) that cites the archived one "
            "via predecessor/origin_handoff_id — never recreate this exact identity."
        )
