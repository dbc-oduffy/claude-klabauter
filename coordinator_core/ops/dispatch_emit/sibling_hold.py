"""Sibling-hold leaf: is a halted predecessor closed, and which run replaced a held slice.

``predecessor_closed`` answers from the run's status-file ``closed_by`` mark, else from the plan its
halt record names (directly, or through the halted sizing's ``plan:`` edge) reading ``implemented``.
``mark_superseded`` writes the write-once ``superseded.json`` beside the run's status file.
File reads plus one atomic write; no subprocess, no git.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

HALTED_FILE = "halted.json"
IMPLEMENTED = "implemented"


class SupersedeRefused(Exception):
    """A supersession mark that must not be written; the text is the refusal."""


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _plan_status(path: Path) -> str:
    import yaml

    from coordinator_core.frontmatter.primitives import split_frontmatter

    try:
        split = split_frontmatter(path.read_text(encoding="utf-8"))
        doc = yaml.safe_load(split.fm_text) if split is not None else None
    except (OSError, yaml.YAMLError):
        return ""
    return str(doc.get("status") or "") if isinstance(doc, dict) else ""


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _implemented_plan(root: Path, halted: dict) -> Optional[str]:
    from coordinator_core.ops.fleet import archive_sizings

    plan_path: Optional[Path] = None
    plan_rel = halted.get("plan")
    if isinstance(plan_rel, str) and plan_rel:
        plan_path = archive_sizings._resolve_plan_fk(root, plan_rel)
    if plan_path is None:
        sizing_rel = halted.get("sizing_rel")
        if not isinstance(sizing_rel, str) or not sizing_rel:
            return None
        from coordinator_core.ops._sizing_citation import resolve_sizing_citation

        sizing = resolve_sizing_citation(root, sizing_rel)
        plan_fk = archive_sizings._read_plan_fk(sizing) if sizing is not None else None
        plan_path = archive_sizings._resolve_plan_fk(root, plan_fk) if plan_fk else None
    if plan_path is None or _plan_status(plan_path) != IMPLEMENTED:
        return None
    return _rel(root, plan_path)


def predecessor_closed(root: Any, run_id: str) -> Optional[str]:
    """The evidence that closed halted *run_id* (a plan rel or a ``closed_by`` value), else None.

    The status file's ``closed_by`` first, then the halt record's plan (or its sizing's plan) reading
    ``implemented``; archive fallbacks are walked only on a live miss.
    """
    base = Path(root)
    status = _read_json(base / contract.status_rel(run_id))
    closed = status.get(contract.CLOSED_BY_KEY) if status else None
    if isinstance(closed, str) and closed:
        return closed
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        halted = _read_json(record_dir(base, run_id) / HALTED_FILE)
    except ValueError:
        return None
    return _implemented_plan(base, halted) if halted else None


def supersede_refusal(root: Any, run_id: str, by: str) -> Optional[str]:
    """Refusal text for marking *run_id* superseded by *by*, else None.

    Checks id shape, self-supersession, a landed/running state and a conflicting mark; it does not
    verify *run_id* names a known sibling, so a mistyped id is marked and releases nothing.
    """
    from coordinator_core.ops._path_guard import safe_id

    for label, value in (("run_id", run_id), ("by", by)):
        if not isinstance(value, str) or not safe_id(value):
            return f"{label} {value!r} is not a single safe path segment."
    if run_id == by:
        return f"Run {run_id} cannot supersede itself."
    base = Path(root)
    status = _read_json(base / contract.status_rel(run_id))
    state = status.get("status") if status else None
    if state in (contract.STATUS_LANDED, contract.STATUS_RUNNING):
        return f"Run {run_id} is {state}; a {state} run is not superseded."
    existing = contract.read_superseded_by(base, run_id)
    if existing is not None and existing != by:
        return f"Run {run_id} is already superseded by {existing}."
    return None


def mark_superseded(root: Any, run_id: str, by: str) -> dict:
    """Write the write-once marker; ``{run_id, superseded_by, written}``. Raises ``SupersedeRefused``."""
    from coordinator_core.atomic_replace import atomic_write_bytes

    refusal = supersede_refusal(root, run_id, by)
    if refusal is not None:
        raise SupersedeRefused(refusal)
    base = Path(root)
    if contract.read_superseded_by(base, run_id) == by:
        return {"run_id": run_id, contract.SUPERSEDED_BY_KEY: by, "written": False}
    payload = {contract.SUPERSEDED_BY_KEY: by, "at": datetime.now(timezone.utc).isoformat()}
    target = base / contract.superseded_rel(run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
    return {"run_id": run_id, contract.SUPERSEDED_BY_KEY: by, "written": True}
