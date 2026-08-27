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

#: Where an archived sizing lands. Month-nested, hence `rglob` below.
_ARCHIVE_SIZINGS_SUBDIR = ("archive", "sizings")


def _archive_sizings_fallback(repo_root: Path, cited: str) -> Optional[Path]:
    """Probe `<repo_root>/archive/sizings/**` for a same-basename record.

    Returns the single matching path, or `None` when the archive subtree is
    absent, holds no match, or holds MORE THAN ONE same-basename file. The
    live corpus resolves 29/29 by basename with zero collisions today, but
    that is a property of this corpus and not a guarantee — a second match
    means the resolver cannot say which record the plan meant, so it says
    nothing and the citation stays unresolved.
    """
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
    """Resolve `cited` under `repo_root`, falling back to `archive/sizings/**`.

    Returns the path that actually answered — the literal `repo_root / cited`
    when it exists, else the single same-basename record under
    `archive/sizings/**` — or `None` when neither resolves. A citation that
    resolves at NEITHER path is a genuine dangling citation and stays one:
    this fallback repairs the archived-record case only, it never turns a
    never-written record green.

    Containment is checked post-`resolve()` (`_path_guard.contained_path`), so
    a citation escaping the repo via `..` or an absolute path resolves to
    `None` rather than reaching outside the tree. Callers keep their own
    is-file/is-dir discipline; this returns whichever path exists.
    """
    root = Path(repo_root)
    resolved = contained_path(root / cited, [root])
    if resolved is not None and resolved.exists():
        return resolved
    archived = _archive_sizings_fallback(root, cited)
    if archived is None:
        return None
    return contained_path(archived, [root])
