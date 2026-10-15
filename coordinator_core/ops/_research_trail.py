"""coordinator_core.ops._research_trail -- the research trail record as data.

Purpose: build and render the docs/research/trail/<run-id>.yaml record every research close
leaves behind. Pure: no I/O, no op registration.

Negative-spec: writes and commits nothing; a record never carries an absolute path, a
local_root value or output content, and a local-only record carries no `paths` key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops._path_guard import safe_id

TRAIL_DIR = Path("docs/research/trail")

_KEYS = ("run_id", "sizing", "value_class", "tier", "destination", "output_count", "paths")


def trail_rel(run_id: str) -> str:
    """Repo-relative POSIX path of the trail record for ``run_id``; ValueError on an unsafe id."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        raise ValueError(f"run_id not a safe id: {run_id!r}")
    return f"{TRAIL_DIR.as_posix()}/{run_id}.yaml"


def _check_rel(path: str, what: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError(f"{what} must be a non-empty string")
    if "\\" in path:
        raise ValueError(f"{what} carries a backslash: {path!r}")
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise ValueError(f"{what} is absolute: {path!r}")
    if ".." in path.split("/"):
        raise ValueError(f"{what} carries a '..' segment: {path!r}")
    return path


def record(
    *,
    run_id: str,
    sizing: str | None,
    value_class: str,
    tier: str,
    destination: str,
    paths: list[str] | tuple[str, ...] | None,
    output_count: int | None = None,
) -> dict[str, Any]:
    """Build a trail record. ``paths`` is the repo-relative artifact list (repo only);
    ``output_count`` is the caller's count for a local-only run and ignored for repo."""
    trail_rel(run_id)
    if destination not in rc.DESTINATIONS:
        raise ValueError(f"destination not in {rc.DESTINATIONS}: {destination!r}")
    if not isinstance(value_class, str) or not safe_id(value_class):
        raise ValueError(f"value_class not a safe id: {value_class!r}")
    if tier not in rc.TIERS:
        raise ValueError(f"tier not in {rc.TIERS}: {tier!r}")
    if sizing is not None:
        _check_rel(sizing, "sizing")
    rec: dict[str, Any] = {
        "run_id": run_id,
        "sizing": sizing,
        "value_class": value_class,
        "tier": tier,
        "destination": destination,
    }
    if destination == "local-only":
        if paths:
            raise ValueError("a local-only record carries no paths")
        if not isinstance(output_count, int) or isinstance(output_count, bool) or output_count < 0:
            raise ValueError("a local-only record needs a non-negative integer output_count")
        rec["output_count"] = output_count
        return rec
    checked = [_check_rel(p, "path") for p in (paths or ())]
    rec["output_count"] = len(checked)
    rec["paths"] = checked
    return rec


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def render(rec: dict[str, Any]) -> str:
    """Deterministic YAML text (fixed key order, LF endings) for a record from ``record``."""
    lines: list[str] = []
    for key in _KEYS:
        if key not in rec:
            continue
        if key == "paths":
            if rec[key]:
                lines.append("paths:")
                lines.extend(f"  - {_scalar(p)}" for p in rec[key])
            else:
                lines.append("paths: []")
        else:
            lines.append(f"{key}: {_scalar(rec[key])}")
    return "\n".join(lines) + "\n"
