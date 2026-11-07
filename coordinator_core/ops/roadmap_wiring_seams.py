"""
coordinator_core.ops.roadmap_wiring_seams — "roadmap.wiring_seams" op (COMPUTE_ONLY).

Purpose: the cross-plan twin of ``wiring_contract.unfed_consumes``. For each roadmap
baton's plan, every ``consumes_external`` entry that names another plan in the roadmap
is judged: ``external-unproduced`` when that plan's ``wiring:`` has no part that
``produces`` the artifact, ``external-not-upstream`` when that plan's baton is not in
the consumer baton's transitive ``blocked_by`` closure.

Entry forms: ``<plan-id>:<artifact>`` (both checks), a bare ``<plan-id>`` (upstream
check only), ``<repo>:<path>@<sha>`` (reported unchecked, ``repo-path-sha``). A
``<plan-id>`` is the plan's ``plan_id``/``deliverable_id`` or its baton's id. An entry
naming no plan of the roadmap is reported unchecked, ``plan-not-in-roadmap``.

Params (all optional): ``roadmap_id`` (keep only batons carrying it; default every live
baton that carries one).

Returns ``{exit_code, ok, count, findings, unchecked}``; ``findings`` rows are
``{code, part, plan, entry, artifact, detail}``, ``unchecked`` rows
``{part, plan, entry, reason}``.

Negative-spec:
  - Reads files only; spawns nothing and writes nothing.
  - Does NOT edit ``wiring_contract.py``; ``unfed_consumes`` stays single-plan.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from coordinator_core.dag import _parse_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.wiring_contract import _seam_parts
from coordinator_core.roadmap import plan_gate as pg

CODE_UNPRODUCED = "external-unproduced"
CODE_NOT_UPSTREAM = "external-not-upstream"
REASON_REPO_REF = "repo-path-sha"
REASON_UNKNOWN_PLAN = "plan-not-in-roadmap"

_REPO_REF_RE = re.compile(r"@[0-9a-fA-F]{7,40}$")


def _closure(start: list[str], graph: Mapping[str, list[str]]) -> set[str]:
    """Every id reachable from `start` over `graph` (id -> blocked_by ids), `start` included."""
    seen: set[str] = set()
    stack = list(start)
    while stack:
        ident = stack.pop()
        if ident in seen:
            continue
        seen.add(ident)
        stack.extend(graph.get(ident, ()))
    return seen


def cross_plan_seams(
    units: list[Mapping[str, Any]], graph: Mapping[str, list[str]]
) -> tuple[list[dict], list[dict]]:
    """`(findings, unchecked)` for the `consumes_external` entries across `units`.

    Each unit is `{name, keys, baton_ids, blocked_by, fm}`: `keys` are the ids the unit's
    plan answers to in an entry, `baton_ids` the ids its baton answers to in a `blocked_by`
    edge, `fm` the plan frontmatter. `graph` maps every baton id to its `blocked_by` ids.
    """
    by_key: dict[str, Mapping[str, Any]] = {}
    for unit in units:
        for key in unit["keys"]:
            by_key.setdefault(key, unit)
    produced = {
        unit["name"]: {e for _pid, _c, produces, _x in _seam_parts(unit["fm"]) for e in produces}
        for unit in units
    }
    findings: list[dict] = []
    unchecked: list[dict] = []
    for unit in units:
        upstream = _closure(list(unit["blocked_by"]), graph)
        for part, _consumes, _produces, external in _seam_parts(unit["fm"]):
            for entry in external:
                if _REPO_REF_RE.search(entry):
                    unchecked.append({"part": part, "plan": unit["name"], "entry": entry, "reason": REASON_REPO_REF})
                    continue
                plan_ref, sep, artifact = entry.partition(":")
                target = by_key.get(plan_ref)
                if target is None:
                    unchecked.append({"part": part, "plan": unit["name"], "entry": entry, "reason": REASON_UNKNOWN_PLAN})
                    continue
                if target is unit:
                    continue
                row = {"part": part, "plan": unit["name"], "entry": entry, "artifact": artifact if sep else None}
                if sep and artifact not in produced[target["name"]]:
                    findings.append({**row, "code": CODE_UNPRODUCED,
                                     "detail": f"{target['name']} has no wiring part that produces {artifact!r}"})
                if not upstream & set(target["baton_ids"]):
                    findings.append({**row, "code": CODE_NOT_UPSTREAM,
                                     "detail": f"baton of {target['name']} is not upstream of {unit['name']} via blocked_by"})
    return findings, unchecked


def _plan_fm(worktree: Path, rel: str) -> Optional[dict]:
    path = worktree / rel
    try:
        fm = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return fm if isinstance(fm, dict) else None


def _plan_path(rec: Mapping[str, Any], index_for: Callable[[], Any]) -> Optional[str]:
    hits, _basis = pg.link_plans(rec["_fm"], index_for())
    top = pg._tied_top_hits(hits)
    return top[0]["path"] if top else None


def _units(worktree: Path, records: list[dict]) -> list[dict]:
    cache: list[Any] = []

    def index_for() -> Any:
        if not cache:
            cache.append(pg.build_plan_index(worktree))
        return cache[0]

    units, seen = [], set()
    for rec in records:
        rel = _plan_path(rec, index_for)
        if not rel or rel in seen:
            continue
        fm = _plan_fm(worktree, rel)
        if fm is None:
            continue
        seen.add(rel)
        keys = [str(k) for k in (fm.get("plan_id"), fm.get("deliverable_id")) if k] + list(rec["ids"])
        units.append({"name": rel, "keys": keys, "baton_ids": list(rec["ids"]),
                      "blocked_by": list(rec["blocked_by"]), "fm": fm})
    return units


@register_op("roadmap.wiring_seams")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Return ``{exit_code, ok, count, findings, unchecked}`` for the roadmap's cross-plan seams."""
    if repo_root is None:
        return {"exit_code": 1, "error": "roadmap.wiring_seams: repo_root is required"}
    worktree = main_worktree_root(repo_root)
    wanted = str(params.get("roadmap_id") or "").strip()
    by_id, records = pg.scan_batons(worktree)
    mine = [
        r for r in records
        if r["live"] and r.get("roadmap_id") and (not wanted or r["roadmap_id"] == wanted)
    ]
    graph = {ident: list(rec["blocked_by"]) for ident, rec in by_id.items()}
    findings, unchecked = cross_plan_seams(_units(worktree, mine), graph)
    return {"exit_code": 0, "ok": not findings, "count": len(findings),
            "findings": findings, "unchecked": unchecked}
