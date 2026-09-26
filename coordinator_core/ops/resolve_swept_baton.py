
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.artifact_basename import md_fallback_candidates
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import ARCHIVE_ROOT_SUBDIRS, main_worktree_root

_LOG = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 15

# coordinator_core.ops.fleet._common.ARCHIVE_ROOT_SUBDIRS — this module was
_ARCHIVE_SUBDIRS = ARCHIVE_ROOT_SUBDIRS


def _find_first_match(worktree_root: Path, basename: str) -> Optional[Path]:
    """Return the first basename match across `_ARCHIVE_SUBDIRS`, or None.

    Each dir is walked with `rglob` (recursive) so flat, month-nested
    (`YYYY-MM/`), and mixed layouts are all tolerated identically. Ties within
    one dir resolve to the lexicographically-first path.

    Bare-slug fallback (2026-07-28 defect fix, same shape as
    `pickup_assemble._search_dirs_for_basename`): when `basename` carries no
    suffix, every artifact this op resolves is stored as `<slug>.md` on
    disk, so a literal `rglob(basename)` alone would never match. Both the
    literal `basename` and `basename + ".md"` are searched per dir, deliberately
    narrowed to `.md` (never a wildcard) since this op's archive dirs hold only
    markdown batons/memos. Candidate-form generation is delegated to
    `coordinator_core.artifact_basename.md_fallback_candidates` (shared with
    `pickup_assemble` and `handoff_stamp` — see that module's docstring);
    this function remains the one place that WALKS.

    Tie-break note: on a same-dir collision between an extensionless file
    and its `.md` sibling (both matched by this function's dual-form
    search), the lexicographic sort below deterministically prefers the
    extensionless form, because its path string is always a strict prefix
    of the `.md` sibling's path string and a prefix always sorts first.
    This function is first-wins by design (unlike `pickup_assemble`'s
    detect-then-fail-loud multi-hit contract — that difference is
    deliberate and is not "fixed" here), so the collision is silently
    resolved rather than surfaced — correct and deterministic today, but
    an emergent property of string comparison, not a chosen preference.
    """
    basenames = md_fallback_candidates(basename)
    for subdir in _ARCHIVE_SUBDIRS:
        archive_dir = worktree_root / subdir
        if not archive_dir.is_dir():
            continue
        matches = sorted(
            (
                p
                for candidate_basename in basenames
                for p in archive_dir.rglob(candidate_basename)
                if p.is_file()
            ),
            key=lambda p: str(p),
        )
        if matches:
            return matches[0]
    return None


def _read_frontmatter(fpath: Path) -> dict:
    try:
        raw = fpath.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as exc:
        _LOG.warning(
            "baton.resolve_swept_in_archive: could not read %s: %s", fpath, exc
        )
        return {}

    if not raw.startswith("---\n"):
        return {}

    parts = raw.split("---\n", 2)
    if len(parts) < 2:
        return {}

    try:
        fm = yaml.safe_load(parts[1])
    except Exception as exc:  # noqa: BLE001 — quarantine parse errors, never raise
        _LOG.warning(
            "baton.resolve_swept_in_archive: frontmatter parse error in %s: %s",
            fpath,
            exc,
        )
        return {}

    return fm if isinstance(fm, dict) else {}


def _archiving_commit(worktree_root: Path, fpath: Path) -> Optional[str]:
    try:
        rel = fpath.relative_to(worktree_root)
    except ValueError:
        rel = fpath
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_root), "log", "-1", "--format=%H", "--", str(rel)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOG.warning(
            "baton.resolve_swept_in_archive: git log failed for %s: %s", fpath, exc
        )
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


@register_op("baton.resolve_swept_in_archive")
async def _resolve_swept_baton_in_archive(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "baton.resolve_swept_in_archive" handler (see module docstring).

    Required params: basename (str) — the bare filename to look for, e.g.
    "2026-07-02_230112_roadmap-pcore-12.md".

    Returns {found, archive_path, frontmatter, archiving_commit} always — this
    op never returns a bare {"error": ...} shape; a not-found basename or an
    absent repo_root both resolve to found:false with the other fields at
    their empty defaults, since "swept baton not found" is an expected,
    common outcome for a pickup-flow fallback probe, not an exceptional one.
    """
    basename = params.get("basename")
    empty = {
        "found": False,
        "archive_path": None,
        "frontmatter": {},
        "archiving_commit": None,
    }

    if not basename or not isinstance(basename, str):
        _LOG.warning(
            "baton.resolve_swept_in_archive: missing/invalid basename param"
        )
        return dict(empty)

    if repo_root is None:
        _LOG.warning(
            "baton.resolve_swept_in_archive: no repo_root resolved — returning not-found"
        )
        return dict(empty)

    worktree_root = main_worktree_root(repo_root)
    match = _find_first_match(worktree_root, basename)
    if match is None:
        return dict(empty)

    return {
        "found": True,
        "archive_path": str(match),
        "frontmatter": _read_frontmatter(match),
        "archiving_commit": await asyncio.to_thread(_archiving_commit, worktree_root, match),
    }
