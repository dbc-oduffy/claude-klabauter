"""
coordinator_core.ops.dispatch_emit.run_close_completion -- the inline ``completion-entry`` leg.

Purpose: write the run's completion entry from the terminal judge's ``completion_summary``,
once per run id, for ``dispatch.terminal_commit``'s close block.

Negative-spec:
  - No process spawn. File writes only, through ``completion_entry``'s locked helpers.
  - No fallback prose: no summary, or a criterion that is not ``met``, is ``failed`` and writes
    nothing. The summary is copied verbatim, never composed from the plan or the verdict.
  - The returned paths are for the caller's ``also_commit``; a caller whose commit does not
    land removes them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.ops.dispatch_emit.run_close import LegResult

AUTHORED_BY = "terminal-judge"
_COMPLETED_DIR = "archive/completed"


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _plan_title(root: Path, plan_rel: str, run_id: str) -> str:
    from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

    try:
        split = split_frontmatter((Path(root) / plan_rel).read_text(encoding="utf-8"))
        raw = read_fm_field(split.fm_text, "title") if split else None
    except OSError:
        raw = None
    title = (raw or "").split("  #")[0].strip().strip("\"'")
    return title or run_id


def _existing_entry(root: Path, run_id: str, title: str) -> Optional[str]:
    from coordinator_core.ops.ceremony.completion_entry import _slug_from_title

    marker = f"\nrun_id: {run_id}\n"
    slug = _slug_from_title(title)
    for path in sorted((Path(root) / _COMPLETED_DIR).glob(f"*/*-{slug}.md")):
        try:
            if marker in path.read_text(encoding="utf-8"):
                return path.relative_to(root).as_posix()
        except OSError:
            continue
    return None


def _stamp_run_id(root: Path, entry_rel: str, run_id: str) -> bool:
    from coordinator_core.frontmatter.primitives import rebuild, split_frontmatter
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    def _mutate(text: str) -> str:
        split = split_frontmatter(text)
        if split is None:
            raise MutateAbort(f"no frontmatter in {entry_rel}")
        fm = split.fm_text if split.fm_text.endswith("\n") else split.fm_text + "\n"
        return rebuild(split, fm + f"run_id: {run_id}\n")

    try:
        locked_rmw(Path(root) / entry_rel, _mutate, repo_root=Path(root))
    except (LockTimeout, MutateAbort, OSError):
        return False
    return True


def completion_entry_leg(
    root: Path, run_id: str, plan_rel: str, criterion: Optional[Mapping[str, Any]]
) -> tuple[LegResult, list[str]]:
    """Return ``(LegResult, written_paths)`` for the completion-entry leg.

    ``failed`` when *criterion* is not ``met`` or carries no ``completion_summary``;
    ``discharged`` when this run id's entry already exists; otherwise ``ran`` naming the
    new entry, which is also the single written path.
    """
    criterion = criterion or {}
    status = str(criterion.get("status") or "absent")
    summary = criterion.get("completion_summary")
    if status != "met":
        return LegResult("failed", f"criterion {status}"), []
    if not isinstance(summary, str) or not summary.strip():
        return LegResult("failed", "no judge summary"), []

    title = _plan_title(root, plan_rel, run_id)
    existing = _existing_entry(root, run_id, title)
    if existing:
        return LegResult("discharged", f"entry {existing}"), []

    from coordinator_core.ops.ceremony.completion_entry import (
        AuthoredSurfaceMissing,
        fill_authored_surfaces,
        fill_completion_entry_residues,
        scaffold_completion_entry,
    )

    scaffold = scaffold_completion_entry(Path(root), title)
    entry_rel = _posix(scaffold["completion_entry_path"])
    if scaffold["failed"] or not entry_rel:
        return LegResult("failed", "entry scaffold failed"), []
    if not scaffold["acted"]:
        return LegResult("failed", f"entry path {entry_rel} taken by another run"), []

    def _fail(reason: str) -> tuple[LegResult, list[str]]:
        try:
            (Path(root) / entry_rel).unlink()
        except OSError:
            pass
        return LegResult("failed", reason), []

    try:
        filled = fill_authored_surfaces(
            Path(root), entry_rel, title=title, nature=None, prose=summary
        )
    except AuthoredSurfaceMissing as exc:
        return _fail(str(exc))
    if not filled["prose"]:
        return _fail("entry prose not written")
    fill_completion_entry_residues(
        Path(root), entry_rel, "", chain_terminal=False, sid=AUTHORED_BY
    )
    if not _stamp_run_id(root, entry_rel, run_id):
        return _fail("entry run_id not stamped")
    return LegResult("ran", f"entry {entry_rel}"), [entry_rel]
