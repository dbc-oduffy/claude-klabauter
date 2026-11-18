"""coordinator_core.ops.skill_preconditions -- "skill.preconditions" op (COMPUTE_ONLY).

Returns one verdict per precondition check the plugin's ``skill-preconditions.yaml``
registers for a skill. The one check shape the yaml carries is a cache read: the
check's ``reads_cache`` file (relative to the repo root, written by the async
SessionStart freshness leg) holds a example-retrieval-repo verdict and a ``checked_epoch``.

Invariants:
  * Read-only: no process is spawned, no refresh is queued, no file is written.
    ``fallback_command`` is never run; ``on_stale`` is reported, never actuated.
  * Verdicts: ``not_applicable`` (the repo has no registered example-retrieval-repo slug),
    ``absent`` (no readable cache, or the cached verdict is absent), ``stale``
    (cache older than ``max_age_hours``, or a verdict that is not ``current``),
    ``fresh``, and ``unsupported`` for a check outside the cache-read shape.
  * An unknown skill or an unreadable registry returns an empty ``checks`` list.

Wire params: ``skill`` (str, required), ``repo_root`` (str, the consumer repo).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

REGISTRY_FILENAME = "skill-preconditions.yaml"
CACHE_VERSION = 1
SECONDS_PER_HOUR = 3600.0


def _plugin_roots() -> list[Path]:
    from coordinator_core.trusted_root_guard import resolve_plugin_root

    own_root = str(Path(__file__).resolve().parents[2])
    return [Path(resolve_plugin_root(own_root))]


def _load_registry(plugin_roots: list[Path]) -> Optional[dict]:
    """The ``skills`` mapping of the first readable registry yaml, else None."""
    import yaml

    for root in plugin_roots:
        for candidate in (root / REGISTRY_FILENAME, root / "coordinator" / REGISTRY_FILENAME):
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError, yaml.YAMLError):
                continue
            skills = data.get("skills") if isinstance(data, dict) else None
            if isinstance(skills, dict):
                return skills
    return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_cache(repo: Path, rel: str) -> Optional[dict]:
    """The cache blob, or None when missing, unparseable or of another version. ``rel`` is
    already confined to the repo by ``_check_verdict``."""
    try:
        blob = json.loads((repo / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or blob.get("cache_version") != CACHE_VERSION:
        return None
    return blob


def _check_verdict(check: dict, repo: Path, applicable: bool, now: float) -> dict:
    out: dict = {"id": check.get("id"), "on_stale": check.get("on_stale")}
    rel, max_age = check.get("reads_cache"), check.get("max_age_hours")
    rel_path = Path(rel) if isinstance(rel, str) and rel else None
    if (
        rel_path is None
        or rel_path.is_absolute()
        or rel_path.anchor
        or ".." in rel_path.parts
        or isinstance(max_age, bool)
        or not isinstance(max_age, (int, float))
        or max_age <= 0
    ):
        out["verdict"] = "unsupported"
        return out
    try:
        (repo / rel_path).resolve().relative_to(repo.resolve())
    except (OSError, ValueError, RuntimeError):
        out["verdict"] = "unsupported"
        return out
    if not applicable:
        out["verdict"] = "not_applicable"
        return out
    blob = _read_cache(repo, rel)
    try:
        checked = float(blob["checked_epoch"]) if blob else None
    except (KeyError, TypeError, ValueError):
        checked = None
    if checked is None:
        out["verdict"] = "absent"
        return out
    age_hours = max(0.0, now - checked) / SECONDS_PER_HOUR
    out["checked_at"] = _iso(checked)
    out["age_hours"] = round(age_hours, 2)
    cached = blob.get("verdict")
    if cached == "absent":
        out["verdict"] = "absent"
    elif cached != "current" or age_hours > max_age:
        out["verdict"] = "stale"
    else:
        out["verdict"] = "fresh"
    if blob.get("reason"):
        out["reason"] = blob["reason"]
    return out


def _has_index(repo: Path) -> bool:
    from coordinator_core.ops.rag_freshness import _slugs_for

    try:
        return bool(_slugs_for(str(repo)))
    except Exception:  # noqa: BLE001 -- an unreadable registry reads as no index
        return False


def skill_preconditions(
    skill: str,
    repo_root: str,
    *,
    plugin_roots: Optional[list[Path]] = None,
    now: Optional[float] = None,
) -> dict:
    """``{"skill", "checks": [...]}`` with one verdict dict per registered check; ``checks``
    is empty for an unknown skill or when no registry yaml is readable."""
    registry = _load_registry(_plugin_roots() if plugin_roots is None else plugin_roots)
    entries = registry.get(skill) if registry else None
    checks = [c for c in entries if isinstance(c, dict)] if isinstance(entries, list) else []
    if not checks:
        return {"skill": skill, "checks": [], "registry_found": registry is not None}
    repo = Path(repo_root)
    applicable = _has_index(repo)
    stamp = time.time() if now is None else now
    return {
        "skill": skill,
        "checks": [_check_verdict(c, repo, applicable, stamp) for c in checks],
        "registry_found": True,
    }


@register_op("skill.preconditions")
def _skill_preconditions(params: dict, repo_root: Path | None = None) -> dict:
    """JSON-RPC "skill.preconditions" handler: one verdict per registered check for ``skill``."""
    skill = params.get("skill")
    if not isinstance(skill, str) or not skill:
        return {"ok": False, "error": "skill.preconditions requires a non-empty string 'skill'"}
    root = params.get("repo_root") or repo_root
    if not root:
        return {"ok": False, "error": "skill.preconditions requires 'repo_root'"}
    return {"ok": True, **skill_preconditions(skill, str(root))}
