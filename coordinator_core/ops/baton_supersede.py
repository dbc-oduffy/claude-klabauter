"""
coordinator_core.ops.baton_supersede — "baton.supersede" op.

Purpose: the sanctioned writer for ``superseded_by`` on a live
``state/handoffs/*.md`` baton, recording that its work was folded into another
baton. ``roadmap.plan_gate`` reads the field and treats the baton as terminal.

Params: ``baton`` (id of the baton to mark: stub_id, handoff_id or filename stem),
``superseded_by`` (id of the baton it was folded into). Both required.

Refuses: a missing/unknown ``baton`` (live batons only), a ``superseded_by`` that
resolves to no baton (live or archived), and a self-supersede. Idempotent: the
same target already recorded is an ``applied: False`` success.

Self-registration: importing this module fires ``@register_op("baton.supersede")``.

Negative-spec:
  - Does NOT git-commit. Frontmatter write only, under ``locked_rmw``; the record is not
    re-validated, so a legacy baton that predates required fields can still be folded.
  - Does NOT touch any field other than ``superseded_by``.
  - Does NOT write archived batons (archive is immutable history).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.roadmap.plan_gate import _resolve_archived, scan_batons

MUTATES = ["state/handoffs/*.md"]


def _err(message: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": f"baton.supersede: {message}"}


@register_op("baton.supersede")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Stamp ``superseded_by: <target>`` onto the live baton ``baton``.

    Returns ``{exit_code, applied, message}`` on success (exit_code 0;
    ``applied`` False when already recorded) or ``{exit_code: 1, applied: False,
    error}``.
    """
    baton = str(params.get("baton") or "").strip()
    target = str(params.get("superseded_by") or "").strip()
    if not baton:
        return _err("missing required param: baton")
    if not target:
        return _err("missing required param: superseded_by")
    if repo_root is None:
        return _err("repo_root is required")

    worktree = main_worktree_root(repo_root)
    by_id, records = scan_batons(worktree, include_archived=False)

    source = by_id.get(baton)
    if source is None:
        return _err(f"baton not found among live batons: {baton!r}")
    target_record = by_id.get(target)
    if target_record is None:
        _resolve_archived(worktree, {target}, by_id, records)
        target_record = by_id.get(target)
    if target_record is None:
        return _err(f"superseded_by names no baton (live or archived): {target!r}")
    if target_record is source or target in source["ids"]:
        return _err(f"a baton cannot supersede itself: {baton!r}")

    path = worktree / source["path"]
    state = {"applied": False}

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no valid YAML frontmatter block in: {source['path']}")
        current = read_fm_field_unquoted(split.fm_text, "superseded_by")
        if current == target:
            return old_text
        if current is None:
            fm = insert_fm_field(split.fm_text, "superseded_by", target)
        else:
            fm = replace_fm_field(split.fm_text, "superseded_by", target)
        state["applied"] = True
        return rebuild(split, fm)

    try:
        locked_rmw(path, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _err(f"lock timeout: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _err(f"cannot read/write baton: {exc}")

    return {
        "exit_code": 0,
        "applied": state["applied"],
        "message": (
            f"{source['path']} superseded_by {target}"
            if state["applied"]
            else f"{source['path']} already superseded_by {target} - no-op"
        ),
    }
