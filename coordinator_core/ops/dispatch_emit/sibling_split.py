"""Sibling-workflow split: partition a plan DAG wider than the per-workflow runtime cap.

The Workflow runtime caps concurrent ``agent()`` calls per workflow at
``min(16, max(2, CPUs-2))``; sibling workflows each get their own cap. Pure functions:
``plan_split`` (DAG -> stages of sibling row sets), ``build_manifest`` (the firing order),
``merge_digests`` (sibling wake digests -> one schema-valid digest).

Partition rule: the DAG's depth range is cut into stages greedily; a stage is as many
consecutive dependency levels as keep every connected component of the stage's induced
subgraph no wider than the cap. Components (independent branches) are first-fit packed into
siblings of width <= cap; siblings of one stage fire together, stage N+1 fires on the
completion notices of every stage N sibling. A DAG of independent branches is one stage.
The last sibling carries the single terminal-commit marker, scoped to every row.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit.commit_request import SIBLINGS_MERGED_PARAM

MANIFEST_SCHEMA = "sibling-manifest"
RUNTIME_CAP_LABEL = "min(16, CPUs-2)"
_LIST_CAPS = {"em_may_think_differently": 10, "unresolved": 10}


def runtime_cap(cpus: Optional[int] = None) -> int:
    """The runtime's per-workflow agent cap: ``min(16, max(2, CPUs-2))``."""
    n = cpus if cpus is not None else (os.cpu_count() or 1)
    return min(16, max(2, n - 2))


@dataclass(frozen=True)
class Sibling:
    index: int
    stage: int
    rows: tuple
    width: int
    carrier: bool


@dataclass(frozen=True)
class SplitPlan:
    cap: int
    plan_width: int
    achieved_width: int
    critical_path_rows: int
    siblings: tuple

    @property
    def stages(self) -> list:
        out: dict = {}
        for sib in self.siblings:
            out.setdefault(sib.stage, []).append(sib)
        return [out[k] for k in sorted(out)]


class WidthOverCapError(Exception):
    """Raised by ``emit_script(auto_split=True)`` when the DAG is wider than the cap."""

    def __init__(self, split: SplitPlan) -> None:
        super().__init__(
            f"plan width {split.plan_width} exceeds the per-workflow cap {split.cap}; "
            f"{len(split.siblings)} sibling workflows"
        )
        self.split = split


def sibling_id(index: int) -> str:
    return f"sib{index}"


def _asap(ids: Sequence[str], after: Mapping[str, Sequence[str]], within: set) -> dict:
    depth: dict = {}
    for rid in ids:
        depth[rid] = 1 + max((depth[a] for a in after[rid] if a in within), default=-1)
    return depth


def _width(depth: Mapping[str, int]) -> int:
    counts: dict = {}
    for d in depth.values():
        counts[d] = counts.get(d, 0) + 1
    return max(counts.values(), default=0)


def _components(ids: Sequence[str], after: Mapping[str, Sequence[str]]) -> list:
    """Connected components of the subgraph induced on ``ids`` (topological order kept)."""
    members = set(ids)
    parent = {rid: rid for rid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for rid in ids:
        for a in after[rid]:
            if a in members:
                parent[find(rid)] = find(a)
    groups: dict = {}
    for rid in ids:
        groups.setdefault(find(rid), []).append(rid)
    return list(groups.values())


def _stage_components(ids, after) -> list:
    """``[(rows, width)]`` per connected component of the stage's induced subgraph."""
    out = []
    for comp in _components(ids, after):
        out.append((comp, _width(_asap(comp, after, set(comp)))))
    return out


def plan_split(dag, cap: int) -> Optional[SplitPlan]:
    """Partition ``dag`` into sibling row sets, or ``None`` when it fits under ``cap``."""
    if dag.max_concurrent_rows <= cap:
        return None
    order = [n.row.id for n in dag.nodes]
    after = {n.row.id: tuple(n.after) for n in dag.nodes}
    depth = _asap(order, after, set(order))
    levels: dict = {}
    for rid in order:
        levels.setdefault(depth[rid], []).append(rid)
    max_depth = max(levels)

    stage_rows: list = []
    lo = 0
    while lo <= max_depth:
        hi = lo
        while hi < max_depth:
            trial = [r for d in range(lo, hi + 2) for r in levels[d]]
            trial = [r for r in order if r in set(trial)]
            if all(w <= cap for _, w in _stage_components(trial, after)):
                hi += 1
            else:
                break
        ids = {r for d in range(lo, hi + 1) for r in levels[d]}
        stage_rows.append([r for r in order if r in ids])
        lo = hi + 1

    siblings: list = []
    achieved = 0
    critical = 0
    index = 0
    for stage_no, ids in enumerate(stage_rows, start=1):
        comps = sorted(_stage_components(ids, after), key=lambda cw: (-cw[1], order.index(cw[0][0])))
        bins: list = []
        for rows, width in comps:
            for b in bins:
                if b["width"] + width <= cap:
                    b["rows"].extend(rows)
                    b["width"] += width
                    break
            else:
                bins.append({"rows": list(rows), "width": width})
        stage_width = 0
        stage_cp = 0
        for b in bins:
            index += 1
            rows = tuple(r for r in order if r in set(b["rows"]))
            stage_width += b["width"]
            stage_cp = max(stage_cp, max(_asap(rows, after, set(rows)).values()) + 1)
            siblings.append(Sibling(index=index, stage=stage_no, rows=rows, width=b["width"], carrier=False))
        achieved = max(achieved, stage_width)
        critical += stage_cp
    last = siblings[-1]
    siblings[-1] = Sibling(last.index, last.stage, last.rows, last.width, True)
    return SplitPlan(
        cap=cap,
        plan_width=dag.max_concurrent_rows,
        achieved_width=min(achieved, dag.max_concurrent_rows),
        critical_path_rows=critical,
        siblings=tuple(siblings),
    )


def width_note(split: SplitPlan, sibling: Sibling) -> dict:
    return {
        "index": sibling.index,
        "count": len(split.siblings),
        "stage": sibling.stage,
        "stages": len(split.stages),
        "cap": split.cap,
        "plan_width": split.plan_width,
        "achieved_width": split.achieved_width,
    }


def width_narration(note: Mapping) -> str:
    return (
        f"Sibling workflow {note['index']} of {note['count']} (stage {note['stage']} of "
        f"{note['stages']}): plan width {note['plan_width']} exceeds the per-workflow cap "
        f"{note['cap']}; {note['count']} siblings, achieved width {note['achieved_width']}."
    )


def build_manifest(split: SplitPlan, *, plan_path: str, script_paths: Mapping[int, str]) -> dict:
    """The unambiguous firing order: stages in sequence, siblings within a stage together."""
    stages = []
    for n, group in enumerate(split.stages, start=1):
        stages.append(
            {
                "stage": n,
                "fire_together": [sibling_id(s.index) for s in group],
                "fire_after": (
                    "emit time"
                    if n == 1
                    else f"the completion notice of every stage {n - 1} sibling"
                ),
            }
        )
    carrier = next(s for s in split.siblings if s.carrier)
    return {
        "schema": MANIFEST_SCHEMA,
        "version": 1,
        "plan": plan_path,
        "cap": split.cap,
        "runtime_cap": RUNTIME_CAP_LABEL,
        "plan_width": split.plan_width,
        "achieved_width": split.achieved_width,
        "siblings": [
            {
                "id": sibling_id(s.index),
                "stage": s.stage,
                "rows": list(s.rows),
                "width": s.width,
                "script": script_paths.get(s.index),
                "commit_carrier": s.carrier,
            }
            for s in split.siblings
        ],
        "stages": stages,
        "commit_carrier": sibling_id(carrier.index),
        "after_last_stage": (
            "merge every sibling digest (merge_digests / emit-dispatch-workflow --merge-digests), "
            f"then run the merged next_action (dispatch.terminal_commit) once; only "
            f"{sibling_id(carrier.index)} carries the commit marker, scoped to every row"
        ),
    }


_TESTS_ORDER = ["fail", "error", "degraded", "unstructured", "pass-with-skips", "pass", "not_run"]
_CRITERION_ORDER = ["not_met", "indeterminate", "unstructured", "met", "not_run"]
_DELIVERY_ORDER = ["FAIL", "unstructured", "PASS", "not_run"]
_REVIEW_ORDER = ["unstructured", "integrated", "not_run"]


def _worst(items: Sequence[dict], key: str, order: Sequence[str]) -> dict:
    return min(items, key=lambda d: order.index(d[key]) if d[key] in order else len(order))


def _sum_or_none(values: Sequence) -> Optional[int]:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _first(values: Sequence):
    return next((v for v in values if v is not None), None)


def _cli(params: Optional[dict]) -> Optional[str]:
    """Python port of the emitted ``_terminalCommitCli`` helper."""
    if not params:
        return None
    path = params.get("task_output_path")
    ok = isinstance(path, str) and path and not any(c in path for c in "<>&'\r\n")
    target = f"'{path}'" if ok else "TASK_OUTPUT_PATH"
    return "coordinator-invoke dispatch.terminal_commit --from-task-output " + target


def merge_digests(digests: Mapping[str, dict], manifest: Mapping) -> dict:
    """One wake-digest-schema-valid digest from every sibling's digest.

    ``digests`` is keyed by sibling id and must cover every sibling in ``manifest``. Counts sum,
    statuses take the worst, lists concatenate under the schema's caps, and ``next_action`` is
    the commit carrier's with ``incomplete_chunks``/``held``/``falsifier_broken`` unioned across
    all siblings (the other siblings carry no commit marker). The merged params name every
    sibling merged (``siblings_merged``, fanout order): terminal_commit grades commit-scope rows
    only when that list is present.
    """
    ids = [s["id"] for s in manifest["siblings"]]
    missing = [i for i in ids if i not in digests]
    if missing:
        raise ValueError(f"merge_digests: no digest for sibling(s) {missing}")
    ds = [digests[i] for i in ids]
    carrier = digests[manifest["commit_carrier"]]

    halted = _first([d["halted"] for d in ds])
    outcome = (
        "halted" if any(d["outcome"] == "halted" for d in ds)
        else "incomplete" if any(d["outcome"] == "incomplete" for d in ds)
        else "completed"
    )

    crit = _worst([d["criterion"] for d in ds], "status", _CRITERION_ORDER)
    criterion = {k: crit[k] for k in crit}

    tests_list = [d["tests"] for d in ds]
    worst_t = _worst(tests_list, "status", _TESTS_ORDER)
    builds = [t["build_clean"] for t in tests_list]
    per_rows = [t["per_row"] for t in tests_list]
    tests = {
        **{k: v for k, v in worst_t.items() if k not in ("skipped", "per_row")},
        "run": _sum_or_none([t["run"] for t in tests_list]),
        "failed": _sum_or_none([t["failed"] for t in tests_list]),
        "build_clean": (
            False if any(b is False for b in builds)
            else True if any(b is True for b in builds) else None
        ),
        "per_row": {
            "verified": sum(p["verified"] for p in per_rows),
            "passed": sum(p["passed"] for p in per_rows),
            "failed": [f for p in per_rows for f in p["failed"]][:20],
            "unstructured": [u for p in per_rows for u in p["unstructured"]],
            "skipped": [s for p in per_rows for s in p["skipped"]],
        },
    }
    skipped = [s for t in tests_list for s in t.get("skipped", [])][:20]
    if skipped or any("skipped" in t for t in tests_list):
        tests["skipped"] = skipped

    reviews = [d["review"] for d in ds]
    deliveries = [r["delivery"] for r in reviews]
    bc = [r["brief_conformance"] for r in reviews if r["brief_conformance"]]
    review = {
        "status": _worst(reviews, "status", _REVIEW_ORDER)["status"],
        "slices": _sum_or_none([r["slices"] for r in reviews]),
        "fixes_applied": _sum_or_none([r["fixes_applied"] for r in reviews]),
        "brief_conformance": (
            {k: sum(b[k] for b in bc) for k in ("items", "met", "unmet")} if bc else None
        ),
        "rebuild_decision": _first([r["rebuild_decision"] for r in reviews]),
        "delivery": {
            "verdict": _worst(deliveries, "verdict", _DELIVERY_ORDER)["verdict"],
            "product_files": _sum_or_none([x["product_files"] for x in deliveries]),
            "claims_unbacked": _sum_or_none([x["claims_unbacked"] for x in deliveries]),
        },
        "integration_sidecar": _first([r["integration_sidecar"] for r in reviews]),
    }
    overflow = sum(r["overflow"] for r in reviews)
    for key, cap in _LIST_CAPS.items():
        joined = [a for r in reviews for a in r[key]]
        review[key] = joined[:cap]
        overflow += max(0, len(joined) - cap)
    review["overflow"] = overflow

    pres = [d["predispatch"] for d in ds if d["predispatch"]]
    predispatch = (
        {
            "reviews": [r for p in pres for r in p["reviews"]],
        }
        if pres else None
    )

    chunks: list = []
    for d in ds:
        chunks.extend(c for c in d["chunks"] if c not in chunks)

    next_action = {k: carrier["next_action"][k] for k in carrier["next_action"]}
    params = next_action.get("params")
    if params is not None:
        union = lambda key: [  # noqa: E731
            x for d in ds for x in ((d["next_action"].get("params") or {}).get(key) or [])
        ]
        incomplete = list(dict.fromkeys(union("incomplete_chunks")))
        params = {**params, "incomplete_chunks": incomplete}
        held = union("held")
        if held or "held" in params:
            params["held"] = list({h["chunk"]: h for h in held}.values())
        broken = union("falsifier_broken")
        if broken or "falsifier_broken" in params:
            params["falsifier_broken"] = broken
        params[SIBLINGS_MERGED_PARAM] = list(ids)
        next_action["params"] = params

    width = {
        "rows": sum(d["width"]["rows"] for d in ds),
        "max_concurrent_rows": manifest["achieved_width"],
        "critical_path_rows": sum(
            max(digests[i]["width"]["critical_path_rows"] for i in st["fire_together"])
            for st in manifest["stages"]
        ),
        "runtime_cap": RUNTIME_CAP_LABEL,
        "runtime_cap_on_emitting_host": manifest["cap"],
    }

    merged = {
        "schema": "wake-digest",
        "version": 1,
        "plan": carrier["plan"],
        "outcome": outcome,
        "completed": all(d["completed"] for d in ds),
        "halted": halted,
        "chunks": chunks,
        "criterion": criterion,
        "tests": tests,
        "review": review,
        "deviations": [dev for d in ds for dev in d["deviations"]],
        "run_base_sha": _first([d["run_base_sha"] for d in ds]),
        "width": width,
        "predispatch": predispatch,
        "decision_required": _first([d["decision_required"] for d in ds]),
        "terminal_commit_cli": _cli(next_action.get("params")),
        "next_action": next_action,
    }
    return merged
