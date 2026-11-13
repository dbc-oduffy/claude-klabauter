"""coordinator_core.ops._research_trail -- the research trail record as data.

Purpose: build and render the <run dir>/trail.yaml record every research close leaves behind
(run dir = docs/research/<topic>/<date>-<run-slug>), and find a run's trail by run id across the
run-dir layout and the legacy docs/research/trail/<run-id>.yaml. Pure except ``locate_trail``,
which only reads; no op registration.

Negative-spec: writes and commits nothing; a record never carries an absolute path, a
local_root value or output content, and a local-only record carries no `paths` key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops._path_guard import safe_id

LEGACY_TRAIL_DIR = Path("docs/research/trail")
TRAIL_NAME = "trail.yaml"

_KEYS = ("run_id", "sizing", "value_class", "tier", "destination", "output_count", "paths")


def trail_rel(run_dir_rel: str) -> str:
    """Repo-relative POSIX path of the trail record inside ``run_dir_rel`` (a repo-relative run dir)."""
    if not isinstance(run_dir_rel, str) or not run_dir_rel:
        raise ValueError(f"run dir must be a non-empty string: {run_dir_rel!r}")
    return _check_rel(run_dir_rel, "run dir").rstrip("/") + "/" + TRAIL_NAME


def locate_trail(worktree: Path, run_id: str) -> Optional[str]:
    """Repo-relative POSIX path of ``run_id``'s trail record, or None when none is on disk.

    Checks the legacy ``docs/research/trail/<run_id>.yaml`` with one stat, then globs
    ``docs/research/*/*/trail.yaml`` for a ``run_id:`` line naming it. ValueError on an unsafe id.
    """
    if not isinstance(run_id, str) or not safe_id(run_id):
        raise ValueError(f"run_id not a safe id: {run_id!r}")
    legacy = f"{LEGACY_TRAIL_DIR.as_posix()}/{run_id}.yaml"
    if (worktree / legacy).is_file():
        return legacy
    want = f"run_id: {json.dumps(run_id)}"
    for path in sorted((worktree / "docs" / "research").glob(f"*/*/{TRAIL_NAME}")):
        try:
            with path.open(encoding="utf-8") as fh:
                head = fh.readline().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if head == want:
            return path.relative_to(worktree).as_posix()
    return None


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
    if not isinstance(run_id, str) or not safe_id(run_id):
        raise ValueError(f"run_id not a safe id: {run_id!r}")
    if destination not in rc.DESTINATIONS:
        raise ValueError(f"destination not in {rc.DESTINATIONS}: {destination!r}")
    if not isinstance(value_class, str) or not safe_id(value_class):
        raise ValueError(f"value_class not a safe id: {value_class!r}")
    tier = rc.LEGACY_TIER_ALIASES.get(tier, tier)
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
