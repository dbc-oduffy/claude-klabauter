"""
coordinator_core.ops.initiatives_serve — JSON-RPC "initiative.serve_set" operation.

Purpose: Read-only resolver that serves the attachable-initiative set
``[{id, label, status, target_date, shape}]`` from ``state/initiatives/*.yaml``
under the main worktree.  The ``shape`` field is derived: ``ongoing`` iff
``target_date`` is null, otherwise ``completion``.

This is the surface the ``resolve-initiative.sh`` client (``~/.claude``) consults
to populate the assignment dropdown — makima serves the set; it does NOT wire
the authoring skills (out of scope, memo carve-out).

Self-registration: importing this module calls
``register_op("initiative.serve_set", _handler)`` as a side-effect.
Add this module to ``coordinator_core/ops/__init__.py`` to trigger registration
at start_server() time.

Read-path: ``_simple_yaml_load`` (scalar-only, no PyYAML dependency) is imported
from ``coordinator_core.ops.emit.sections.initiatives``.  It is correct ONLY for
the flat ``state/initiatives/*.yaml`` shape (no arrays).  Do NOT use it for stub
handoff frontmatter — stub frontmatter carries ``blocks``/``blocked_by`` as YAML
arrays that ``_simple_yaml_load`` cannot parse correctly.

Worktree resolution mirrors ``handoff_children.py``:
  - When ``repo_root`` is provided (router-supplied git common dir), the worktree
    root is derived via ``main_worktree_root(repo_root)``.
  - If ``repo_root`` is absent the op returns an empty list with a logged warning
    rather than raising — empty is safe for a dropdown.

Spec backlink: pln-makima-served-initiative-roadm-8e0492 § C2
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.emit.sections.initiatives import _simple_yaml_load
from coordinator_core.ops.fleet._common import main_worktree_root

_LOG = logging.getLogger(__name__)

# Valid status values — mirrors the emit porter canonical-4.
_VALID_STATUS = frozenset({"active", "paused", "shipped", "abandoned"})


def _collect_initiatives(initiatives_dir: Path) -> List[dict]:
    """Read ``state/initiatives/*.yaml`` and build the serve-set payload.

    Returns a list of ``{id, label, status, target_date, shape}`` dicts, one per
    well-formed initiative file.  Files missing required ``id`` or ``label`` fields
    are skipped with a warning (graceful-absent, mirrors the emit porter quarantine
    contract).

    Shape derivation: ``ongoing`` iff ``target_date`` is null, else ``completion``.

    Negative-spec:
    - Does NOT mutate the store.
    - Does NOT raise on missing/unreadable files — quarantines them with a warning.
    - Does NOT use ``_simple_yaml_load`` for any array-bearing YAML (stub handoffs) —
      this function reads only flat ``state/initiatives/*.yaml`` files.
    """
    results: List[dict] = []

    if not initiatives_dir.is_dir():
        return results

    for fpath in sorted(initiatives_dir.glob("*.yaml")):
        fname = fpath.name
        try:
            content = fpath.read_text(encoding="utf-8")
            fm = _simple_yaml_load(content)
        except Exception as exc:  # noqa: BLE001 — parity with emit porter quarantine
            _LOG.warning("initiative.serve_set: skipping %s — parse error: %s", fname, exc)
            continue

        id_val = fm.get("id")
        label_val = fm.get("label")

        if not isinstance(id_val, str) or not id_val:
            _LOG.warning(
                "initiative.serve_set: skipping %s — missing required field: id", fname
            )
            continue
        if not isinstance(label_val, str) or not label_val:
            _LOG.warning(
                "initiative.serve_set: skipping %s — missing required field: label", fname
            )
            continue

        raw_status = fm.get("status")
        if raw_status in _VALID_STATUS:
            status_val = raw_status
        else:
            # Review: code-reviewer — log a warning for present-but-unrecognised status so
            # callers receiving status:null can distinguish "absent" from "rejected value"
            # (mirrors the missing-id/label quarantine warning pattern in this function).
            if raw_status is not None:
                _LOG.warning(
                    "initiative.serve_set: %s has unrecognised status %r — coercing to null",
                    fname,
                    raw_status,
                )
            status_val = None

        target_date = fm.get("target_date")  # ISO-date string or None

        # Shape derivation: ongoing iff target_date is null, else completion.
        shape = "ongoing" if target_date is None else "completion"

        results.append(
            {
                "id": id_val,
                "label": label_val,
                "status": status_val,
                "target_date": target_date,
                "shape": shape,
            }
        )

    return results


@register_op("initiative.serve_set")
def _handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "initiative.serve_set" handler.

    Returns the attachable-initiative set from ``state/initiatives/*.yaml``.

    Params: (none required)

    Returns:
        {
            "initiatives": [
                {"id": str, "label": str, "status": str|null,
                 "target_date": str|null, "shape": "ongoing"|"completion"},
                ...
            ]
        }

    Shape derivation: ``ongoing`` iff ``target_date`` is null, else ``completion``.

    Worktree resolution (mirrors handoff_children.py):
    - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
    - Neither → return empty list with logged warning (empty is safe for a dropdown)
    """
    # Derive the worktree root from the router-supplied common dir.
    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)  # router common_dir → worktree root
    else:
        _LOG.warning(
            "initiative.serve_set: no repo_root resolved — "
            "repo_root arg absent; returning empty set"
        )
        return {"initiatives": []}

    initiatives_dir = worktree_root / "state" / "initiatives"
    initiatives = _collect_initiatives(initiatives_dir)

    return {"initiatives": initiatives}
