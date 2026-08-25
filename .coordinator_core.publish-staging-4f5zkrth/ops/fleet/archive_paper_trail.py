"""
coordinator_core.ops.fleet.archive_paper_trail — fleet.archive_paper_trail op.

Purpose: archive a research-session paper-trail workdir
(docs/research/{run_id}-workdir) into the caller's own archive tree
(docs/research/archive/{YYYY-MM-DD}-{topic_slug}) under a preview/act
(dry_run:true / dry_run:false) contract.

Unifies the two call sites the fence inventory found describing this same
canonical_op — coordinator/skills/staff-session/SKILL.md:218 (a `cp -r`
paper-trail archive step) and coordinator/commands/notebooklm-research.md:316
(a git-mv paper-trail archive step) — into ONE module, standardised on the
atomic-rename shape per the oracle's own recommendation
(state/audits/2026-07-22-command-payload-inventory/distinct-ops-new.tsv).
The `cp -r` variant is NOT ported: re-running it over an existing archive dir
silently merges/overwrites rather than failing loud, which is exactly the
hazard the atomic-rename shape (this module) closes.

This is a THIN candidate-discovery layer over
ops/fleet/_common.py::archive_and_commit — it contains zero new move/rename
logic of its own; the git-mv + single-commit mechanics (private-index
isolation, pathspec-both-sides, disk-rename reversal on failure) all live in
_common and are reused unchanged (plan
docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § C1b).

The workdir being archived is a directory tree, not a single file, so the
"candidate discovery" this module performs is enumerating every file under
docs/research/{run_id}-workdir/ and building one _common.Move per file
(dst mirrors the same relative path under the destination dir) — the
per-file git-mv + single combined commit is still 100% archive_and_commit's
mechanics. After a fully successful move, the now-empty leftover directories
under the source are removed with plain os.rmdir (never rmtree — rmdir
raises OSError on anything non-empty, so a partially-failed move can never
have its still-populated remainder silently deleted). This is bookkeeping
over already-vacated directories, not move/rename logic: git itself never
tracks empty directories, so the deleted-in-git rename does not remove them,
and leaving them behind would make a later src.exists() re-run check see a
stale, git-invisible directory and misreport the vacuous-no-op condition.

DEC-7 idempotency note (op-classification.tsv rates idempotency-hazard low /
platform-hazard medium for this row): a second invocation with identical
params is a safe no-op because the source dir is gone after the first
successful archive — dry_run:false detects the missing source and returns
archived:false, already_archived:true instead of attempting (and failing,
or worse silently re-merging) a second move. The archive destination name
embeds the archival date (YYYY-MM-DD, computed at call time, not sourced
from run_id), so a same-topic rerun on a LATER calendar day is detected via
a glob over docs/research/archive/*-{topic_slug} rather than assuming the
original dest name — this is what makes the no-op detection date-independent.
The op-classification row's platform-hazard note additionally floats porting
the mv itself to a raw os.rename + os.stat().st_dev cross-device fallback;
that suggestion is superseded here by the plan's binding C1b design note
("write zero new move/rename logic" — reuse archive_and_commit), whose
git-mv mechanics are already cross-platform (git owns the rename, not a
POSIX-only `cp -r` or a hand-rolled os.rename) and carry no bash/coreutils
dependency of their own.

Spec backlinks:
  - Plan (C1b): docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § C1b
  - Oracle: state/audits/2026-07-22-command-payload-inventory/distinct-ops-new.tsv
            (archive-paper-trail-to-archive-dir)
  - Manifest: state/audits/2026-07-22-command-payload-inventory/op-classification.tsv
              (archive-paper-trail-to-archive-dir → fleet.archive_paper_trail)
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D1-D4, five bounds)

Negative-spec:
  - Does NOT port the cp -r staff-session variant — see rationale above.
  - Does NOT write any new move/rename logic — delegates entirely to
    ops/fleet/_common.py::archive_and_commit (Move descriptor + async git mv +
    single scoped-pathspec commit).
  - Does NOT use params.repo_root as the worktree source — see
    _common.check_repo_root / main_worktree_root (params.repo_root is a D3
    caller-side consistency assertion only).
  - Does NOT use git add -A / git add . (DR-211 D3 Invariant 4 — enforced
    inside _common.archive_and_commit, not re-implemented here).
  - Does NOT register any op key scope — that is the shared _registry_map.py /
    op_scopes.py surface, owned by a separate serial tail pass.
  - Does NOT overwrite an existing destination — a genuine same-day collision
    (dest already present while src also still exists) is left untouched and
    logged rather than force-merged; this module does not carry the `cp -r`
    variant's silent-overwrite hazard.
"""

from __future__ import annotations

import logging
from datetime import date
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

_RESEARCH_SUBDIR = ("docs", "research")
_ARCHIVE_SUBDIR = ("docs", "research", "archive")


def _validate_component(value: object, field_name: str) -> str:
    """Validate a run_id/topic_slug path component.

    Raises ValueError if value is not a non-empty string, or if it contains a
    path separator or '..' — both of which would let a caller escape the
    docs/research/ tree this op is scoped to build paths under.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(
            f"{field_name}={value!r} must be a bare path component "
            f"(no separators, no '..')"
        )
    return value


def _find_existing_archive(archive_dir: Path, topic_slug: str) -> Optional[Path]:
    """Return the most recent existing archive dir for topic_slug, if any.

    Globs docs/research/archive/*-{topic_slug} rather than assuming the
    dest name this call would compute (today's date) — a prior archive of
    the same topic on an earlier calendar day is still a valid
    already-archived signal (see module docstring DEC-7 note).
    """
    if not archive_dir.is_dir():
        return None
    matches = sorted(archive_dir.glob(f"*-{topic_slug}"))
    return matches[-1] if matches else None


def _compute_dest(archive_dir: Path, topic_slug: str) -> Path:
    """Compute today's archive destination: archive_dir/{YYYY-MM-DD}-{topic_slug}."""
    return archive_dir / f"{date.today().isoformat()}-{topic_slug}"


@register_op("fleet.archive_paper_trail")
async def _handler(params: dict, repo_root=None) -> dict:
    """fleet.archive_paper_trail — archive a research paper-trail workdir.

    Wire contract (manifest row archive-paper-trail-to-archive-dir):
        params: {repo_root: str, run_id: str, topic_slug: str, dry_run: bool}
        ->      {archived: bool, dest: str, already_archived: bool}

    The response MAY additionally carry a `failed` key — a non-empty list of
    `{"id": str, "reason": str}` items from `_common.archive_and_commit` —
    present ONLY when a dry_run:false attempt's per-file git-mv failed for one
    or more files (`archived` is then `False`). This is an ADDITIVE key,
    absent on every other path, so a v1 consumer reading only
    `{archived, dest, already_archived}` is unaffected. See
    `coordinator/bin/archive-paper-trail.py`'s exit-code 4 branch, which is
    the sanctioned consumer of this key.

    dry_run:true  → preview only; mutates nothing.
    dry_run:false → performs the atomic rename (via _common.archive_and_commit)
                    and commits, or detects an already-archived / collision
                    condition and returns without mutating.

    repo_root arg: the git common dir delivered by the engine's
    _OP_KEY_SCOPE="common_dir" keying (wired by the shared registry-map
    surface, not this module). params.repo_root is the D3 consistency check
    only — NEVER the worktree-root resolution source (mirrors every other
    fleet.* handler's Key Decision 5 convention).

    Raises ValueError on malformed params (missing/wrong-typed run_id,
    topic_slug, dry_run, or a path-component value containing a separator or
    '..') — this op's frozen 3-field response has no error-envelope slot to
    return a setup error into, so malformed input fails loud instead.
    """
    run_id = _validate_component(params.get("run_id"), "run_id")
    topic_slug = _validate_component(params.get("topic_slug"), "topic_slug")
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        raise ValueError(f"dry_run must be bool, got {type(dry_run).__name__!r}")

    if repo_root is None:
        raise RuntimeError(
            "fleet.archive_paper_trail: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production; "
            "test fixtures must supply an explicit value"
        )

    common_dir = Path(repo_root)
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        raise RuntimeError(f"fleet.archive_paper_trail: {mismatch}")
    worktree = main_worktree_root(common_dir)

    research_dir = worktree.joinpath(*_RESEARCH_SUBDIR)
    archive_dir = worktree.joinpath(*_ARCHIVE_SUBDIR)
    src = research_dir / f"{run_id}-workdir"
    dest = _compute_dest(archive_dir, topic_slug)

    if not src.exists():
        # Vacuous no-op: the source is already gone (either archived by a
        # prior call, or never existed). Surface the most recent matching
        # archive dir if one is found; else the dest we would have used.
        existing = _find_existing_archive(archive_dir, topic_slug)
        resolved_dest = existing if existing is not None else dest
        return {
            "archived": False,
            "dest": rel_id(resolved_dest, worktree),
            "already_archived": True,
        }

    if dest.exists():
        # Genuine same-day collision (src still present, dest already taken)
        # — left untouched rather than force-merged (see module negative-spec).
        _LOG.error(
            "fleet.archive_paper_trail: dest %s already exists while src %s "
            "still exists — refusing to overwrite; leaving both untouched",
            dest, src,
        )
        return {
            "archived": False,
            "dest": rel_id(dest, worktree),
            "already_archived": False,
        }

    if dry_run:
        return {
            "archived": False,
            "dest": rel_id(dest, worktree),
            "already_archived": False,
        }

    files = sorted(p for p in src.rglob("*") if p.is_file())
    if not files:
        # Nothing git-tracks in an empty workdir tree — remove the leftover
        # dir tree (if any) so a later call sees src as genuinely gone, and
        # report a non-mutating no-op (there was nothing to archive).
        # Review: code-reviewer (F3) — a single flat src.rmdir() fails with
        # OSError whenever src contains empty nested subdirectories (dir not
        # empty), silently stranding src forever across every subsequent
        # call. Mirror the success-path cleanup below: remove nested empty
        # dirs deepest-first before the flat top-level rmdir.
        for leftover_dir in sorted(
            (p for p in src.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                leftover_dir.rmdir()
            except OSError:
                pass
        try:
            src.rmdir()
        except OSError:
            pass
        return {
            "archived": False,
            "dest": rel_id(dest, worktree),
            "already_archived": False,
        }

    moves = [
        Move(src=f, dst=dest / f.relative_to(src), candidate_id=rel_id(f, worktree))
        for f in files
    ]
    subject = (
        f"fleet: archive paper-trail {run_id} -> "
        f"{dest.name} [fleet.archive_paper_trail]"
    )
    acted, failed = await archive_and_commit(worktree, moves, subject)

    if failed:
        _LOG.error(
            "fleet.archive_paper_trail: archive_and_commit reported failures for "
            "%s -> %s: %s",
            src, dest, failed,
        )
        return {
            "archived": False,
            "dest": rel_id(dest, worktree),
            "already_archived": False,
            "failed": failed,
        }

    # Every file moved successfully — clean up the now-empty leftover source
    # directories (git never tracked them; plain rmdir refuses if anything
    # unexpected remains, so a partial-success remainder is never deleted).
    for leftover_dir in sorted(
        (p for p in src.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            leftover_dir.rmdir()
        except OSError:
            pass
    try:
        src.rmdir()
    except OSError:
        pass

    return {
        "archived": True,
        "dest": rel_id(dest, worktree),
        "already_archived": False,
    }
