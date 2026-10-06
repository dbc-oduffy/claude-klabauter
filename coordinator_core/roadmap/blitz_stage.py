"""
coordinator_core.roadmap.blitz_stage — Phase 2 of a roadmap blitz, as one call.

Purpose: turn a finished roadmap (clusters.md + reconciliation.md under
``state/roadmap/<roadmap_id>/``) into numbered, scaffolded ``roadmap-baton``
stubs, report their size against the M/L band, run the audit-roadmap gate in
process, and freeze the gate report plan-blitz consumes.

Reuses, never re-implements: ``coordinator-doc-new``'s ``_scaffold_roadmap_baton``
(loaded in process), ``ops.mint_deliverable_id.mint``, ``roadmap.graph.topo_number``
and ``roadmap.number_stubs`` edge parsing, ``roadmap.audit.run_audit``, and
the ``roadmap.plan_gate`` op handler.

Roadmap format read (the shape ``roadmap-planning`` Phase 1 emits):
  - ``clusters.md``: one ``## <id> — <title>`` section per cluster; a section's
    ``**blocked_by:**`` / ``**blocks:**`` lines are the cluster dependency edges.
    An optional ``**loe:** <XS|S|M|L|XL|XXL>`` line is the cluster's declared size.
    An optional ``Stub slug prefix: `<x>``` line names the stub-id prefix;
      a date-led roadmap id with no such line is refused (the default prefix would be the year).
  - ``reconciliation.md``: the KEEP verdict table; absent -> every cluster is KEEP.

Gate report contract: the BARE ``roadmap.plan_gate`` result for the roadmap, serialized by
``ops.roadmap_plan_gate.bare_text`` (byte-identical to ``coordinator-invoke --bare
roadmap.plan_gate '{"roadmap_id": ...}'``), frozen at
``state/plan-blitz/<roadmap_id>/wave-1.gate-report.json``. ``waves`` are plan_gate's own: lists of
baton ids, numbered from 0.

Fold rule (roadmap-planning Step 2.1.6, sized by ``sizing_assemble.TSHIRT_WEIGHT``): see
``FOLD_RULE``.

Each newly scaffolded stub with a fold-sized t-shirt gets its OWN sizing object under
the sizings home (``estimate.tshirt`` and ``route`` from ``sizing_assemble.route``, premise
``read`` with the clusters.md evidence, deliverable_id shared with the stub), linked from the
stub's ``sizing_object:``. 

Negative-spec:
  - Commits only the paths it wrote (stubs, then the gate report), via commit_v2. plan_gate
    withholds an untracked baton, so with ``commit=False`` the report is deferred
    (``gate_report_path: null``) until the stubs are tracked.
  - Does NOT invent a size or split a stub: a cluster with no ``**loe:**`` refuses the whole call,
    naming every such cluster; XL/XXL are flagged.
  - Does NOT rewrite a frozen gate report or a staged stub.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.roadmap.audit import parse_keep_cluster_ids, run_audit, validate_run_id
from coordinator_core.roadmap.graph import RoadmapCycleError, topo_number
from coordinator_core.session import record_homes
from coordinator_core.roadmap.number_stubs import (
    _stamp_author_sprints,
    _validate_sprint_axis_order,
    derive_nodes,
    parse_edges_file,
)
from coordinator_core.session.claimed_write import create_exclusive
from coordinator_core.sizing_assemble import TSHIRT_ORDER, TSHIRT_WEIGHT

# Creates authored handoff stubs and a frozen gate report once per run; none is a regenerable stamped artifact.
GENERATES = []

_DOC_NEW = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "coordinator-doc-new.py"
_BAND = frozenset({"M", "L"})
_SIZES = ("XS", "S", "M", "L", "XL", "XXL")
_CLUSTER_HEAD_RE = re.compile(
    r"^##\s+(?P<id>[A-Za-z]{1,6}-?\d+[A-Za-z0-9]*)\s*[—–:\-]+\s*(?P<title>.+?)\s*$"
)
_ID_TOKEN_RE = re.compile(r"\b[A-Za-z]{1,6}-?\d+[A-Za-z0-9]*\b")
_BLOCKED_BY_RE = re.compile(r"\*\*blocked_by:\*\*([^*\n]*)", re.IGNORECASE)
_BLOCKS_RE = re.compile(r"\*\*blocks:\*\*([^*\n]*)", re.IGNORECASE)
_LOE_RE = re.compile(r"\*\*(?:loe|size):\*\*\s*(XXL|XL|XS|S|M|L)\b", re.IGNORECASE)
_PREFIX_RE = re.compile(r"Stub slug prefix:\s*`?([a-z0-9][a-z0-9-]*)`?", re.IGNORECASE)
_FM_ROADMAP_ID_RE = re.compile(r"^roadmap_id:\s*['\"]?([a-z0-9][a-z0-9-]*)", re.MULTILINE)

GATE_REPORT_NAME = "wave-1.gate-report.json"


_DATE_LED_RE = re.compile(r"\d{4}(?:-\d{2}){0,2}(?:-|$)")


class BlitzStageRefused(ValueError):
    """The roadmap cannot be staged; the message names the single cause."""


@functools.cache
def _load_doc_new():
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_for_blitz_stage", _DOC_NEW)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _resolve_run_dir(roadmap: Path) -> Tuple[Path, str]:
    run_dir = roadmap if roadmap.is_dir() else roadmap.parent
    roadmap_id = run_dir.name
    overview = run_dir / "OVERVIEW.md"
    if overview.is_file():
        head = overview.read_text(encoding="utf-8")[:4000]
        m = _FM_ROADMAP_ID_RE.search(head)
        if m:
            roadmap_id = m.group(1)
    err = validate_run_id(roadmap_id)
    if err:
        raise BlitzStageRefused(err)
    return run_dir, roadmap_id


def parse_clusters(text: str) -> List[Dict[str, Any]]:
    """Cluster sections of ``clusters.md`` in file order, each with its
    declared ``blocked_by``/``blocks`` tokens and optional ``loe``."""
    sections: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        head = _CLUSTER_HEAD_RE.match(line)
        if head:
            current = {"id": head.group("id"), "title": head.group("title"), "body": []}
            sections.append(current)
        elif line.startswith("## "):
            current = None
        elif current is not None:
            current["body"].append(line)
    for sec in sections:
        body = "\n".join(sec.pop("body"))
        sec["blocked_by_tokens"] = [t for m in _BLOCKED_BY_RE.finditer(body) for t in _ID_TOKEN_RE.findall(m.group(1))]
        sec["blocks_tokens"] = [t for m in _BLOCKS_RE.finditer(body) for t in _ID_TOKEN_RE.findall(m.group(1))]
        loe = _LOE_RE.search(body)
        sec["loe"] = loe.group(1).upper() if loe else None
    return sections


def _cluster_edges(sections: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    known = {s["id"] for s in sections}
    seen = set()
    edges: List[Dict[str, str]] = []

    def add(blocked: str, blocker: str) -> None:
        if blocked in known and blocker in known and blocked != blocker and (blocked, blocker) not in seen:
            seen.add((blocked, blocker))
            edges.append({"from": blocked, "to": blocker})

    for sec in sections:
        for tok in sec["blocked_by_tokens"]:
            add(sec["id"], tok)
        for tok in sec["blocks_tokens"]:
            add(tok, sec["id"])
    return edges


def number_clusters(
    labels: List[str], edges: List[Dict[str, str]], author_sprints: Optional[Dict[str, int]] = None
) -> Dict[str, Dict[str, Any]]:
    """``{label: {number, sprint, wave}}`` in dependency order, via the same
    ``topo_number`` the roadmap-number-stubs CLI runs."""
    try:
        result = topo_number(labels, edges)
    except RoadmapCycleError as err:
        raise BlitzStageRefused(f"dependency cycle among clusters: {', '.join(err.cycle)}") from err
    order, number = result["order"], result["number"]
    sprint_wave = result["sprintWave"]
    if author_sprints:
        sprint_wave = _stamp_author_sprints(order, sprint_wave, author_sprints, edges)
        check = _validate_sprint_axis_order(order, number, sprint_wave, edges)
        if not check["ok"]:
            raise BlitzStageRefused("author-assigned sprint tags are not dependency-monotone")
    return {
        label: {"number": number[label], "sprint": sprint_wave[label]["sprint"], "wave": sprint_wave[label]["wave"]}
        for label in order
    }


def _find_staged(repo_root: Path, stub_id: str) -> Optional[Path]:
    tail = f"roadmap-{stub_id}.md"
    for base in (Path(record_homes.home_dir(str(repo_root), "handoffs")), repo_root / "archive" / "handoffs"):
        if base.is_dir():
            for hit in sorted(base.rglob(f"*{tail}")):
                if hit.name == tail or hit.name.endswith("_" + tail):
                    return hit
    return None


def _patch_frontmatter(
    content: str, *, sprint: int, wave: int, loe: Optional[str], blocked_by: List[str]
) -> str:
    out: List[str] = []
    fences = 0
    for line in content.split("\n"):
        if line == "---":
            fences += 1
        if fences == 1:
            if line.startswith("sprint:"):
                line = f"sprint: {sprint}"
            elif line.startswith("wave:"):
                line = f"wave: {wave}"
            elif line.startswith("loe:"):
                if loe is None:
                    continue
                line = f"loe: {loe}"
            elif line == "blocked_by: []" and blocked_by:
                out.append("blocked_by:")
                out.extend(f"  - {b}" for b in blocked_by)
                continue
            elif line.startswith("blocking_notes: PLACEHOLDER") and blocked_by:
                continue
        out.append(line)
    return "\n".join(out)


_M_WEIGHT = TSHIRT_WEIGHT["M"]
_XL_WEIGHT = TSHIRT_WEIGHT["XL"]

FOLD_RULE = (
    "roadmap-planning Step 2.1.6 as ruled by the APM: XS/S never ship alone. A stub under M folds "
    "only along a declared edge, into its sole direct dependent, else its sole direct dependency, "
    "until the merged weight (TSHIRT_WEIGHT) reaches M; a merge that would reach XL is refused. A "
    "stub with no edge, or with several candidates in both directions, is `unfoldable-small`. "
    "Nothing is split; XL is flagged `xl-review`, XXL `mis-made`. Same-tier stubs are never "
    "collapsed: scope disjointness is unknown at staging, so collapse is a no-op."
)


def _tshirt_for_weight(weight: int) -> str:
    """Largest t-shirt whose weight does not exceed ``weight`` (never overstates)."""
    fit = [t for t in TSHIRT_ORDER if TSHIRT_WEIGHT[t] <= weight]
    return fit[-1] if fit else TSHIRT_ORDER[0]


def fold_units(
    order: List[str], sizes: Dict[str, Optional[str]], edges: List[Dict[str, str]]
) -> Dict[str, Any]:
    """Fold sub-M clusters along a declared edge. ``order`` is dependency order (blockers first);
    every cluster is sized. Returns ``{"units": {label: {sources, weight, loe}}, "blocked_by":
    {...}, "merged": [...], "flagged": [...]}``; the unit label is the absorbing cluster's."""
    units: Dict[str, Dict[str, Any]] = {}
    for label in order:
        tshirt = sizes.get(label)
        if tshirt not in TSHIRT_WEIGHT:
            raise BlitzStageRefused(f"cluster {label} declares size {tshirt!r}; expected one of {TSHIRT_ORDER}")
        units[label] = {"sources": [label], "weight": TSHIRT_WEIGHT[tshirt], "loe": tshirt}
    blockers: Dict[str, set] = {label: set() for label in order}
    dependents: Dict[str, set] = {label: set() for label in order}
    for e in edges:
        if e["from"] in units and e["to"] in units:
            blockers[e["from"]].add(e["to"])
            dependents[e["to"]].add(e["from"])

    merged: List[Dict[str, Any]] = []
    flagged: List[Dict[str, Any]] = []
    for label in order:
        unit = units.get(label)
        if unit is None or unit["weight"] >= _M_WEIGHT:
            continue
        if len(dependents[label]) == 1:
            target = next(iter(dependents[label]))
        elif len(blockers[label]) == 1:
            target = next(iter(blockers[label]))
        else:
            reason = "no edge" if not dependents[label] and not blockers[label] else "several candidates"
            flagged.append({"cluster": label, "reason": f"unfoldable-small: {reason}"})
            continue
        host = units[target]
        if host["weight"] + unit["weight"] >= _XL_WEIGHT:
            flagged.append({"cluster": label, "reason": "unfoldable-small: merge would reach XL"})
            continue
        into_dependent = target in dependents[label]
        host["sources"] = (
            unit["sources"] + host["sources"] if into_dependent else host["sources"] + unit["sources"]
        )
        host["weight"] += unit["weight"]
        host["loe"] = _tshirt_for_weight(host["weight"])
        for b in blockers[label]:
            dependents[b].discard(label)
        for d in dependents[label]:
            blockers[d].discard(label)
        if into_dependent:
            blockers[target] = (blockers[target] | blockers[label]) - {label}
            for b in blockers[label]:
                dependents[b].add(target)
        else:
            for d in dependents[label]:
                blockers[d].add(target)
                dependents[target].add(d)
        dependents[target].discard(label)
        blockers[target].discard(label)
        del units[label], blockers[label], dependents[label]
        merged.append({"into": target, "absorbed": label})
    for label, unit in units.items():
        if unit["weight"] >= TSHIRT_WEIGHT["XXL"]:
            flagged.append({"cluster": label, "reason": "mis-made: XXL"})
        elif unit["weight"] >= _XL_WEIGHT:
            flagged.append({"cluster": label, "reason": "xl-review"})
    return {"units": units, "blocked_by": blockers, "merged": merged, "flagged": flagged}


def _fold_report(stubs: List[Dict[str, Any]], fold: Dict[str, Any]) -> Dict[str, Any]:
    from coordinator_core.sizing_assemble import route

    by_label = {s["label"]: s for s in stubs}
    return {
        "merged": [
            {"stub_id": s["stub_id"], "sources": s["covers"], "loe": s["loe"]}
            for s in stubs
            if len(s["covers"]) > 1
        ],
        "split": [],
        "flagged": [
            {"stub_id": by_label[f["cluster"]]["stub_id"], "cluster": f["cluster"], "reason": f["reason"]}
            for f in fold["flagged"]
            if f["cluster"] in by_label
        ],
        "in_band": [s["stub_id"] for s in stubs if s["loe"] in _BAND],
        "routes": {
            s["stub_id"]: route(estimate={"tshirt": s["loe"]}).get("route") for s in stubs if s["loe"]
        },
        "rule": FOLD_RULE,
    }


def _write_sizing(
    repo_root: Path, doc_new: Any, title: str, stub_id: str, deliverable_id: str, unit: Dict[str, Any], source: str
) -> str:
    """Mint this stub's own sizing object from its fold result and return its repo-relative path.
    The sizing carries the stub's deliverable_id; premise provenance is `read` (the roadmap's
    own cluster text), its evidence naming the clusters read."""
    from coordinator_core.sizing_assemble import route, write_back

    decision = route(estimate={"tshirt": unit["loe"]}, premise_provenance="read")
    evidence = f"{source} § {', '.join(unit['sources'])}"
    rel = Path(record_homes.record_path("", "sizings", f"{doc_new._today()}-{stub_id}.yaml")).as_posix()
    text = doc_new._scaffold_sizing(
        title=title,
        deliverable_id=deliverable_id,
        tshirt=unit["loe"],
        route=decision["route"],
        premise="read",
        premise_evidence=evidence,
        detents=list(decision["detents"]),
    )
    target = repo_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    create_exclusive(target, text if text.endswith("\n") else text + "\n")
    write_back(repo_root, rel, decision, premise_provenance="read", premise_evidence=evidence)
    return rel


def _commit_paths(repo_root: Path, paths: List[str], message: str) -> Dict[str, Any]:
    """The engine's scoped commit over explicit paths (``ceremony.commit_v2``'s handler)."""
    from coordinator_core.ops.ceremony.commit_v2 import _handler

    return _handler({"paths": paths, "message": message}, repo_root=repo_root)


def _commit(repo_root: Path, paths: List[str], message: str) -> Tuple[Optional[str], Optional[str]]:
    """``(sha, None)`` on success, ``(None, reason)`` when the commit did not land."""
    try:
        reply = _commit_paths(repo_root, paths, message)
    except Exception as exc:  # noqa: BLE001 -- a failed commit leaves the stubs staged on disk, reported
        return None, f"{type(exc).__name__}: {exc}"
    if reply.get("committed"):
        return reply.get("sha"), None
    return None, str(reply.get("error") or reply)


def _freeze_gate_report(
    repo_root: Path, roadmap_id: str, stub_paths: List[str], audit_passed: bool
) -> Dict[str, Any]:
    """The report is write-once (0444), so a failed audit defers it: freezing over stubs the
    audit could not find would pin a report no rerun can correct."""
    rel = Path(record_homes.record_path("", "plan-blitz", f"{roadmap_id}/{GATE_REPORT_NAME}")).as_posix()
    target = repo_root / rel
    if target.is_file():
        return {"gate_report_path": rel, "gate_report_state": "already-frozen"}
    if not audit_passed:
        return {"gate_report_path": None, "gate_report_state": "deferred-audit-failed"}
    from coordinator_core.ops.roadmap_plan_gate import _handler as plan_gate_op, bare_text

    report = plan_gate_op({"roadmap_id": roadmap_id}, repo_root=repo_root)
    staged = set(stub_paths)
    untracked = [row["path"] for row in report.get("untracked") or [] if row.get("path") in staged]
    if untracked:
        return {
            "gate_report_path": None,
            "gate_report_state": "deferred-untracked",
            "untracked_stubs": untracked,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    create_exclusive(target, bare_text(report))
    os.chmod(target, 0o444)
    return {"gate_report_path": rel, "gate_report_state": "frozen-now"}


def stage_roadmap(
    repo_root: Path,
    roadmap: str,
    *,
    edges_path: Optional[str] = None,
    branch: str = "main",
    commit: bool = True,
) -> Dict[str, Any]:
    """Stage the roadmap at ``roadmap`` (its directory or any file in it).
    Returns ``{gate_report_path, stubs, numbering, folds, audit, commits, ...}``."""
    repo_root = Path(repo_root)
    roadmap_path = Path(roadmap)
    if not roadmap_path.is_absolute():
        roadmap_path = repo_root / roadmap_path
    if not roadmap_path.exists():
        raise BlitzStageRefused(f"roadmap path does not exist: {roadmap}")
    run_dir, roadmap_id = _resolve_run_dir(roadmap_path)
    clusters_file = run_dir / "clusters.md"
    if not clusters_file.is_file():
        raise BlitzStageRefused(f"no clusters.md under {run_dir}")
    clusters_text = clusters_file.read_text(encoding="utf-8")
    sections = parse_clusters(clusters_text)
    if not sections:
        raise BlitzStageRefused(f"clusters.md names no `## <id> — <title>` cluster sections: {clusters_file}")

    recon = run_dir / "reconciliation.md"
    keep = parse_keep_cluster_ids(recon.read_text(encoding="utf-8")) if recon.is_file() else []
    if keep:
        sections = [s for s in sections if s["id"] in keep]
        if not sections:
            raise BlitzStageRefused("no cluster section matches a KEEP verdict in reconciliation.md")
    by_id = {s["id"]: s for s in sections}

    author_sprints: Dict[str, int] = {}
    if edges_path:
        edges_file = Path(edges_path)
        if not edges_file.is_absolute():
            edges_file = repo_root / edges_file
        if not edges_file.is_file():
            raise BlitzStageRefused(f"edges_path does not exist: {edges_path}")
        parsed = parse_edges_file(edges_file.read_text(encoding="utf-8"))
        edges = parsed["edges"]
        author_sprints = parsed.get("sprints", {})
        nodes = list(by_id)
        for label in derive_nodes(edges, parsed["isolatedNodes"]):
            if label not in nodes:
                nodes.append(label)
        edge_source = "edges_path"
    else:
        edges = _cluster_edges(sections)
        nodes = list(by_id)
        edge_source = "clusters.md"

    missing = [c for c in nodes if c not in by_id or by_id[c]["loe"] is None]
    if missing:
        raise BlitzStageRefused(
            "clusters missing `**loe:** <XS|S|M|L|XL|XXL>` in clusters.md: " + ", ".join(missing)
        )
    cluster_numbering = number_clusters(nodes, edges, author_sprints)
    cluster_order = sorted(cluster_numbering, key=lambda c: cluster_numbering[c]["number"])
    fold = fold_units(cluster_order, {c: by_id[c]["loe"] for c in cluster_order}, edges)
    unit_labels = [c for c in cluster_order if c in fold["units"]]
    unit_edges = [
        {"from": a, "to": b} for a in unit_labels for b in sorted(fold["blocked_by"][a], key=cluster_order.index)
    ]
    numbering = number_clusters(unit_labels, unit_edges, {})
    width = max(2, len(str(max(v["number"] for v in numbering.values()))))
    prefix_match = _PREFIX_RE.search(clusters_text)
    if prefix_match:
        prefix = prefix_match.group(1)
    elif _DATE_LED_RE.match(roadmap_id):
        raise BlitzStageRefused(
            f"roadmap id {roadmap_id!r} is date-led and clusters.md has no `Stub slug prefix: `<x>`` line; "
            "the default prefix would be the year and collide with every other date-led roadmap's stub ids"
        )
    else:
        prefix = roadmap_id.split("-")[0]
    for label, info in numbering.items():
        info["stub_id"] = f"{prefix}-{str(info['number']).zfill(width)}"
        info["covers"] = fold["units"][label]["sources"]

    blocked_by_of = {
        label: sorted(numbering[b]["stub_id"] for b in fold["blocked_by"][label]) for label in unit_labels
    }
    blocks_of: Dict[str, List[str]] = {label: [] for label in unit_labels}
    for label in unit_labels:
        for b in fold["blocked_by"][label]:
            blocks_of[b].append(numbering[label]["stub_id"])

    doc_new = _load_doc_new()
    from coordinator_core.ops.mint_deliverable_id import mint

    handoffs = Path(record_homes.home_dir(str(repo_root), "handoffs"))
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stubs: List[Dict[str, Any]] = []
    for label in sorted(unit_labels, key=lambda c: numbering[c]["number"]):
        info = numbering[label]
        stub_id = info["stub_id"]
        unit = fold["units"][label]
        title = " + ".join(by_id[c]["title"] for c in unit["sources"] if c in by_id) or label
        existing = _find_staged(repo_root, stub_id)
        row: Dict[str, Any] = {
            "stub_id": stub_id,
            "label": label,
            "cluster": label,
            "covers": unit["sources"],
            "title": title,
            "loe": unit["loe"],
            "wave": info["wave"],
            "blocked_by": blocked_by_of[label],
        }
        if existing is not None:
            row.update(path=existing.relative_to(repo_root).as_posix(), created=False)
            stubs.append(row)
            continue
        deliverable_id, _ = mint(stub_id=stub_id)
        sizing_rel = _write_sizing(
            repo_root, doc_new, title, stub_id, deliverable_id, unit,
            f"{run_dir.relative_to(repo_root).as_posix()}/clusters.md",
        )
        content = doc_new._scaffold_roadmap_baton(
            title=title,
            branch=branch,
            roadmap_id=roadmap_id,
            stub_id=stub_id,
            deliverable_id=deliverable_id,
            sizing_object=sizing_rel,
            blocks=sorted(blocks_of[label]),
            covers=unit["sources"],
        )
        content = _patch_frontmatter(
            content, sprint=info["sprint"], wave=info["wave"], loe=unit["loe"], blocked_by=row["blocked_by"]
        )
        handoffs.mkdir(parents=True, exist_ok=True)
        written = create_exclusive(handoffs / f"{stamp}_roadmap-{stub_id}.md", content + "\n")
        row.update(
            path=written.relative_to(repo_root).as_posix(),
            created=True,
            deliverable_id=deliverable_id,
            sizing_object=sizing_rel,
        )
        stubs.append(row)

    exit_code, out_lines, err_lines = run_audit(roadmap_id, repo_root, repo_root / "state")
    audit = {"exit_code": exit_code, "passed": exit_code == 0, "stdout": out_lines, "stderr": err_lines}

    commits: List[Dict[str, Any]] = []
    commit_error: Optional[str] = None
    new_paths = [p for s in stubs if s["created"] for p in (s["path"], s.get("sizing_object")) if p]
    if commit and new_paths:
        sha, commit_error = _commit(
            repo_root, new_paths, f"roadmap-blitz-stage: scaffold {len(new_paths)} stub(s) for {roadmap_id}"
        )
        if sha:
            commits.append({"sha": sha, "paths": new_paths})

    frozen = _freeze_gate_report(
        repo_root, roadmap_id, [s["path"] for s in stubs], audit["passed"]
    )
    if commit and frozen["gate_report_state"] == "frozen-now":
        sha, err = _commit(
            repo_root,
            [frozen["gate_report_path"]],
            f"roadmap-blitz-stage: freeze the wave-1 gate report for {roadmap_id}",
        )
        if sha:
            commits.append({"sha": sha, "paths": [frozen["gate_report_path"]]})
        commit_error = commit_error or err
    return {
        "roadmap_id": roadmap_id,
        "edge_source": edge_source,
        **frozen,
        "stubs": stubs,
        "numbering": numbering,
        "cluster_numbering": cluster_numbering,
        "folds": _fold_report(stubs, fold),
        "audit": audit,
        "commits": commits,
        "commit_error": commit_error,
    }


def main(argv: List[str]) -> int:
    """CLI: ``roadmap-blitz-stage <roadmap> [--edges PATH] [--branch B] [--repo-root DIR]``.

    Prints the stage reply as JSON. Exit 0 when staged and the audit passed, 1 when
    the audit failed, 2 on a usage error or a refused roadmap."""
    import argparse

    parser = argparse.ArgumentParser(prog="roadmap-blitz-stage", add_help=True)
    parser.add_argument("roadmap")
    parser.add_argument("--edges", default=None)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--no-commit", action="store_true", help="leave the stubs and report uncommitted")
    parser.add_argument("--repo-root", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.repo_root:
        root = Path(args.repo_root)
    else:
        from coordinator_core.roadmap.audit import resolve_repo_root

        root = resolve_repo_root()
    try:
        reply = stage_roadmap(
            root, args.roadmap, edges_path=args.edges, branch=args.branch,
            commit=not args.no_commit,
        )
    except BlitzStageRefused as exc:
        print(f"roadmap-blitz-stage: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(reply, indent=2, default=str))
    return 0 if reply["audit"]["passed"] else 1
