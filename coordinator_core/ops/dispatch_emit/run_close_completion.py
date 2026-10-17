"""
coordinator_core.ops.dispatch_emit.run_close_completion -- the inline ``completion-entry`` leg.

Purpose: write the run's completion entry from the terminal judge's ``completion_summary``,
once per run id, for ``dispatch.terminal_commit``'s close block.

Negative-spec:
  - No process spawn. File writes only, through ``completion_entry``'s locked helpers.
  - No composed prose: the prose is the judge's ``completion_summary``, else a ``met``
    criterion's ``observation`` (a recorded re-judge carries only that), copied verbatim, never
    composed from the plan or the verdict. Neither, or a criterion that is not ``met``, is
    ``failed`` and writes nothing.
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


def _remedy(reason: str, title: str) -> str:
    from coordinator_core.ops.ceremony.completion_entry import _completion_entry_rel_path

    entry = _posix(str(_completion_entry_rel_path(title)))
    return (
        f"{reason}; no entry written at {entry}; write it by hand: "
        f"python coordinator/bin/coordinator-complete-entry.py"
    )


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


def _stamp_apply(text: str, entry_rel: str, run_id: str) -> str:
    from coordinator_core.frontmatter.primitives import rebuild, split_frontmatter
    from coordinator_core.locked_write import MutateAbort

    split = split_frontmatter(text)
    if split is None:
        raise MutateAbort(f"no frontmatter in {entry_rel}")
    fm = split.fm_text if split.fm_text.endswith("\n") else split.fm_text + "\n"
    return rebuild(split, fm + f"run_id: {run_id}\n")


def _stamp_run_id(root: Path, entry_rel: str, run_id: str) -> bool:
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    try:
        locked_rmw(
            Path(root) / entry_rel,
            lambda text: _stamp_apply(text, entry_rel, run_id),
            repo_root=Path(root),
        )
    except (LockTimeout, MutateAbort, OSError):
        return False
    return True


def completion_entry_leg(
    root: Path, run_id: str, plan_rel: str, criterion: Optional[Mapping[str, Any]]
) -> tuple[LegResult, list[str]]:
    """Return ``(LegResult, written_paths)`` for the completion-entry leg.

    ``failed`` when *criterion* is not ``met`` or carries neither a ``completion_summary`` nor
    an ``observation``;
    ``discharged`` when this run id's entry already exists; otherwise ``ran`` naming the
    new entry, which is also the single written path.
    """
    criterion = criterion or {}
    status = str(criterion.get("status") or "absent")
    summary = criterion.get("completion_summary")
    title = _plan_title(root, plan_rel, run_id)
    if status != "met":
        return LegResult("failed", _remedy(f"criterion {status}", title)), []
    if not isinstance(summary, str) or not summary.strip():
        summary = criterion.get("observation")
    if not isinstance(summary, str) or not summary.strip():
        return LegResult("failed", _remedy("no judge summary", title)), []

    existing = _existing_entry(root, run_id, title)
    if existing:
        return LegResult("discharged", f"entry {existing}"), []

    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
    from coordinator_core.ops.ceremony.completion_entry import (
        _COMPLETION_NATURE_DEFAULT,
        AuthoredSurfaceMissing,
        _completion_entry_rel_path,
        _scaffold_text,
        authored_surfaces_mutator,
        residue_mutator,
        scaffold_apply,
    )

    entry_rel = _posix(str(_completion_entry_rel_path(title)))
    entry_abs = Path(root) / entry_rel
    if entry_abs.exists():
        return LegResult("failed", f"entry path {entry_rel} taken by another run"), []

    authored, filled = authored_surfaces_mutator(
        entry_rel, title=title, nature=None, prose=summary
    )
    residues, _ = residue_mutator(
        Path(root), entry_rel, "", chain_terminal=False, sid=AUTHORED_BY
    )
    scaffold = _scaffold_text(title, _COMPLETION_NATURE_DEFAULT, None)

    def _compose(old: str) -> str:
        text = scaffold_apply(old, scaffold)
        text = authored(text)
        if not filled["prose"]:
            raise MutateAbort("entry prose not written")
        try:
            text = residues(text)
        except MutateAbort:
            pass
        return _stamp_apply(text, entry_rel, run_id)

    try:
        entry_abs.parent.mkdir(parents=True, exist_ok=True)
        locked_rmw(entry_abs, _compose, repo_root=Path(root), missing_ok=True)
    except AuthoredSurfaceMissing as exc:
        return LegResult("failed", str(exc)), []
    except MutateAbort as exc:
        return LegResult("failed", str(exc)), []
    except (LockTimeout, OSError):
        return LegResult("failed", "entry scaffold failed"), []
    return LegResult("ran", f"entry {entry_rel}"), [entry_rel]
