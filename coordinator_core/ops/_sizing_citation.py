"""Resolve a plan's `sizing_object:` citation against disk, live-then-archive.

Purpose: `sizing_object:` is spelled `state/sizings/<id>.yaml` in every plan
that carries one, but `fleet.archive_terminal_sizings` moves a terminal sizing
to `archive/sizings/<month>/<id>.yaml` and — by its own negative spec —
rewrites no citation. Every consumer that resolves the citation literally
therefore breaks the moment the record ships: `assert_plan_sizing_citation`
loudly (29 dangling on the 2026-08-20 corpus), `dispatch_emit.derive_review_
tier` silently (a `None` tier composes no review phase at all).

The FK is archive-agnostic by design, exactly as `plan.schema.json`'s
`predecessor_handoff` description already states for the handoff FK — "a value
resolving only under `archive/` is correct, not broken". This module is the
sizings sibling of the mechanism that discharges that for handoffs,
`coordinator_core.sibling_fact._resolve_archive_handoffs_fallback`, and
mirrors its shape deliberately: live always wins, the archive probe fires only
on a miss at the literal path, and an ambiguous multi-match is treated exactly
like no match rather than guessed at.

NEGATIVE SPEC — READ-ONLY, AND NEVER A REWRITE: this module resolves a
citation; it never edits a plan, a sizing, or a schema. Widening the readers
is the whole fix — a mechanism that rewrote citing plans would have to rewrite
immutable `archive/specs/` records to keep the invariant true.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.ops._path_guard import contained_path

_ARCHIVE_SIZINGS_SUBDIR = ("archive", "sizings")


def _archive_sizings_fallback(repo_root: Path, cited: str) -> Optional[Path]:
    archive_root = repo_root.joinpath(*_ARCHIVE_SIZINGS_SUBDIR)
    if not archive_root.is_dir():
        return None
    basename = os.path.basename(cited.replace("\\", "/"))
    if not basename:
        return None
    matches = sorted(p for p in archive_root.rglob(basename) if p.is_file())
    if len(matches) != 1:
        return None
    return matches[0]


def resolve_sizing_citation(repo_root: str | os.PathLike[str], cited: str) -> Optional[Path]:
    root = Path(repo_root)
    resolved = contained_path(root / cited, [root])
    if resolved is not None and resolved.exists():
        return resolved
    archived = _archive_sizings_fallback(root, cited)
    if archived is None:
        return None
    return contained_path(archived, [root])
