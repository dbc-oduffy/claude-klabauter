"""
coordinator_core.roadmap.blitz_stage — Phase 2 of a roadmap blitz, as one call.

Purpose: turn a finished roadmap (clusters.md + reconciliation.md under
``state/roadmap/<roadmap_id>/``) into numbered, scaffolded ``roadmap-baton``
stubs, report their size against the M/L band, run the audit-roadmap gate in
process, and freeze the gate report plan-blitz consumes.

Reuses, never re-implements: ``coordinator-doc-new``'s ``_scaffold_roadmap_baton``
(loaded in process), ``ops.mint_deliverable_id.mint``, ``roadmap.graph.topo_number``
and ``roadmap.number_stubs`` edge parsing, ``roadmap.audit.run_audit``, and
nothing else.

Roadmap format read (the shape ``roadmap-planning`` Phase 1 emits):
  - ``clusters.md``: one ``## <id> — <title>`` section per cluster; a section's
    ``**blocked_by:**`` / ``**blocks:**`` lines are the cluster dependency edges.
    An optional ``**loe:** <XS|S|M|L|XL|XXL>`` line is the cluster's declared size.
    An optional ``Stub slug prefix: `<x>``` line names the stub-id prefix;
      without it a date-led id derives a letters-only prefix from the id after its date head
      (an id with no letter refuses), and any derived prefix already staged by another roadmap refuses.
  - ``reconciliation.md``: the KEEP verdict table; absent -> every cluster is KEEP.

Gate report contract: ``gate_v2.compute_gate_report`` is frozen to
``state/plan-blitz/<roadmap_id>/wave-1.gate-report.json`` (``frozen-now``). A report already there
is returned (``already-frozen``) and wins; a failed audit defers it (``deferred-audit-failed``) and
an untracked staged stub defers it (``deferred-untracked``); ``waves`` is [] when deferred. A
``GateReadRefused`` raises ``BlitzStageRefused`` with the gate's own text.

Fold rule (roadmap-planning Step 2.1.6, sized by ``sizing_assemble.TSHIRT_WEIGHT``): see
``FOLD_RULE``.

Each newly scaffolded stub with a fold-sized t-shirt gets its OWN sizing object under
the sizings home (``estimate.tshirt`` and ``route`` from ``sizing_assemble.route``, premise
``read`` with the clusters.md evidence, deliverable_id shared with the stub), linked from the
stub's ``sizing_object:``. 

Negative-spec:
  - Commits only the paths it wrote (stubs), via commit_v2.
  - Does NOT invent a size or split a stub: a cluster with no ``**loe:**`` refuses the whole call,
    naming every such cluster; XL/XXL are flagged.
  - Does NOT rewrite a frozen gate report or a staged stub.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
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


_DATE_HEAD_RE = re.compile(r"\d{4}(?:-\d{2}){0,2}(?:-|$)")


def _derive_date_led_prefix(roadmap_id: str) -> Optional[str]:
    """Letters-only stub prefix of a date-led id: the first remaining token's letters, else the
    initials of the tokens that have letters; None when no letter is left."""
    tokens = [t for t in _DATE_HEAD_RE.sub("", roadmap_id, count=1).split("-") if t]
    letters = ["".join(ch for ch in t.lower() if "a" <= ch <= "z") for t in tokens]
    if not any(letters):
        return None
    return letters[0] or "".join(t[0] for t in letters if t)


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


def _staged_other_roadmap(repo_root: Path, prefix: str, roadmap_id: str) -> Optional[str]:
    """The roadmap_id of a staged ``roadmap-<prefix>-NN.md`` baton owned by a different roadmap.
    Scans the same homes as ``_has_staged``; one pass could answer both."""
    for base in (Path(record_homes.home_dir(str(repo_root), "handoffs")), repo_root / "archive" / "handoffs"):
        if base.is_dir():
            for hit in sorted(base.rglob(f"*roadmap-{prefix}-[0-9]*.md")):
                head = hit.read_text(encoding="utf-8", errors="replace")[:2000]
                found = _FM_ROADMAP_ID_RE.search(head)
                owner = found.group(1) if found else None
                if owner == roadmap_id or (owner is None and roadmap_id in head):
                    continue
                return owner or hit.name
    return None


def _has_staged(repo_root: Path, prefix: str, roadmap_id: str) -> bool:
    """True when any baton of this roadmap is already staged: a resume must reproduce the first
    staging's fold, so first-staging-only rules are off."""
    for base in (Path(record_homes.home_dir(str(repo_root), "handoffs")), repo_root / "archive" / "handoffs"):
        if base.is_dir():
            for hit in base.rglob(f"*roadmap-{prefix}-[0-9]*.md"):
                if roadmap_id in hit.read_text(encoding="utf-8", errors="replace")[:2000]:
                    return True
    return False


def _patch_frontmatter(
    content: str,
    *,
    sprint: Optional[int] = None,
    wave: Optional[int] = None,
    loe: Optional[str] = None,
    blocked_by: Optional[List[str]] = None,
    fold_flag: Optional[str] = None,
    set_fields: Optional[Dict[str, str]] = None,
) -> str:
    """Stub mode (``sprint`` given) rewrites the scaffold's sprint/wave/loe/blocked_by lines.
    ``set_fields`` replaces each named top-level key's line, or adds it before the closing
    fence; values are written as JSON-quoted scalars."""
    stub_mode = sprint is not None
    blocked_by = blocked_by or []
    out: List[str] = []
    fences = 0
    pending = dict(set_fields or {})
    for line in content.split("\n"):
        if line == "---":
            fences += 1
            if fences == 2:
                out.extend(f"{k}: {json.dumps(v)}" for k, v in pending.items())
                pending = {}
        if fences == 1:
            key = line.split(":", 1)[0] if ":" in line and not line.startswith((" ", "-")) else None
            if key in pending:
                line = f"{key}: {json.dumps(pending.pop(key))}"
            if not stub_mode:
                pass
            elif line.startswith("sprint:"):
                line = f"sprint: {sprint}"
            elif line.startswith("wave:"):
                line = f"wave: {wave}"
            elif line.startswith("loe:"):
                if loe is None:
                    continue
                line = f"loe: {loe}"
                if fold_flag:
                    out.append(line)
                    line = f"fold_flag: {fold_flag}"
            elif line == "blocked_by: []" and blocked_by:
                out.append("blocked_by:")
                out.extend(f"  - {b}" for b in blocked_by)
                continue
            elif line == "deployment_state: ready_to_fire" and blocked_by:
                line = "deployment_state: awaiting_gate"
        out.append(line)
    return "\n".join(out)


_M_WEIGHT = TSHIRT_WEIGHT["M"]
_XL_WEIGHT = TSHIRT_WEIGHT["XL"]

UNFOLDABLE_SMALL = "unfoldable-small"
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


TIE_BREAK_KEY = "earliest in dependency order, then lowest cluster id"


def fold_units(
    order: List[str],
    sizes: Dict[str, Optional[str]],
    edges: List[Dict[str, str]],
    *,
    tie_break: bool = False,
) -> Dict[str, Any]:
    """Fold sub-M clusters along a declared edge. ``order`` is dependency order (blockers first);
    every cluster is sized. Returns ``{"units": {label: {sources, weight, loe}}, "blocked_by":
    {...}, "merged": [...], "flagged": [...], "tie_breaks": [...]}``; the unit label is the absorbing
    cluster's. With ``tie_break`` a stub with several candidates folds into the earliest one in
    ``order`` (then lowest label) and the choice lands in ``tie_breaks``; the XL refusal still applies."""
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
    tie_breaks: List[Dict[str, Any]] = []
    for label in order:
        unit = units.get(label)
        if unit is None or unit["weight"] >= _M_WEIGHT:
            continue
        if len(dependents[label]) == 1:
            target = next(iter(dependents[label]))
        elif len(blockers[label]) == 1:
            target = next(iter(blockers[label]))
        elif tie_break and (dependents[label] or blockers[label]):
            candidates = sorted(dependents[label] | blockers[label], key=lambda c: (order.index(c), c))
            target = candidates[0]
            tie_breaks.append(
                {"absorbed": label, "into": target, "candidates": candidates, "key": TIE_BREAK_KEY}
            )
        else:
            reason = "no edge" if not dependents[label] and not blockers[label] else "several candidates"
            flagged.append({"cluster": label, "reason": f"{UNFOLDABLE_SMALL}: {reason}"})
            continue
        host = units[target]
        if host["weight"] + unit["weight"] >= _XL_WEIGHT:
            flagged.append({"cluster": label, "reason": f"{UNFOLDABLE_SMALL}: merge would reach XL"})
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
    return {"units": units, "blocked_by": blockers, "merged": merged, "flagged": flagged, "tie_breaks": tie_breaks}


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
        "tie_breaks": [
            {
                "stub_id": by_label[t["absorbed"]]["stub_id"] if t["absorbed"] in by_label else None,
                "absorbed": t["absorbed"],
                "into": t["into"],
                "into_stub_id": by_label[t["into"]]["stub_id"] if t["into"] in by_label else None,
                "candidates": t["candidates"],
                "key": t["key"],
            }
            for t in fold["tie_breaks"]
            if t["into"] in fold["units"] or t["into"] in by_label
        ],
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
    repo_root: Path, doc_new: Any, title: str, stub_id: str, deliverable_id: str, unit: Dict[str, Any], source: str,
    interaction_mode: Optional[str] = None,
) -> str:
    """Mint this stub's own sizing object from its fold result and return its repo-relative path.
    The sizing carries the stub's deliverable_id; premise provenance is `read` (the roadmap's
    own cluster text), its evidence naming the clusters read. A given `interaction_mode` is the run's
    and is recorded on the sizing."""
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
    write_back(
        repo_root, rel, decision, premise_provenance="read", premise_evidence=evidence,
        interaction_mode=interaction_mode,
    )
    return rel


def _ignored_untracked_hint(repo_root: Path, paths: List[str]) -> Optional[List[str]]:
    """The untracked ``paths`` a gitignore rule covers, read in process; ``None`` on any gap so
    commit_v2 asks git itself."""
    from coordinator_core.git.git_state import IndexParseError, read_index
    from coordinator_core.git.ignore_match import IgnoreMatcher

    try:
        tracked = read_index(repo_root)
        matcher = IgnoreMatcher(repo_root)
        covered: List[str] = []
        for rel in paths:
            if tracked.get(rel) is not None:
                continue
            verdict = matcher.ignored(rel, False)
            if verdict is None:
                return None
            if verdict:
                covered.append(rel)
        return covered
    except (IndexParseError, OSError):
        return None


def _commit_paths(repo_root: Path, paths: List[str], message: str) -> Dict[str, Any]:
    """The engine's scoped commit over explicit paths (``ceremony.commit_v2``'s handler)."""
    from coordinator_core.ops.ceremony.commit_v2 import _handler

    params: Dict[str, Any] = {"paths": paths, "message": message}
    hint = _ignored_untracked_hint(repo_root, paths)
    if hint is not None:
        params["ignored_untracked"] = hint
    return _handler(params, repo_root=repo_root)


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
    repo_root: Path,
    roadmap_id: str,
    stub_paths: List[str],
    audit_passed: bool,
    unfoldable_small: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The report is write-once (0444), so a failed audit defers it: freezing over stubs the
    audit could not find would pin a report no rerun can correct. A ``GateReadRefused`` from
    ``compute_gate_report`` surfaces as ``BlitzStageRefused`` with the gate's own text."""
    rel = Path(record_homes.record_path("", "plan-blitz", f"{roadmap_id}/{GATE_REPORT_NAME}")).as_posix()
    target = repo_root / rel
    if target.is_file():
        return {
            "gate_report_path": rel,
            "gate_report_state": "already-frozen",
            "_report": json.loads(target.read_text(encoding="utf-8")),
        }
    if not audit_passed:
        return {"gate_report_path": None, "gate_report_state": "deferred-audit-failed"}
    from coordinator_core.roadmap import gate_v2

    try:
        report = gate_v2.compute_gate_report(repo_root, roadmap_id=roadmap_id)
    except gate_v2.GateReadRefused as exc:
        raise BlitzStageRefused(str(exc)) from exc
    if unfoldable_small:
        report["unfoldable_small"] = sorted(unfoldable_small)
    staged = set(stub_paths)
    untracked = [row["path"] for row in report.get("untracked") or [] if row.get("path") in staged]
    if untracked:
        return {
            "gate_report_path": None,
            "gate_report_state": "deferred-untracked",
            "untracked_stubs": untracked,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    create_exclusive(target, json.dumps(report, indent=2, default=str) + "\n")
    os.chmod(target, 0o444)
    return {"gate_report_path": rel, "gate_report_state": "frozen-now", "_report": report}


def _wave_baton_row(row: Dict[str, Any], baton_id: str) -> Dict[str, Any]:
    """One wave entry from a report row (requirement-9 nested fields)."""
    plan = row.get("plan") or {}
    gate = row.get("execution_gate") or {}
    return {
        "id": baton_id,
        "path": row.get("path"),
        "title": row.get("title"),
        "sized": row.get("sized"),
        "planPath": plan.get("path"),
        "executionOpen": gate.get("open"),
        "sizingObjects": row.get("sizing_objects"),
    }


_HOLD_REASON_RE = re.compile(r"^plan_blitz_hold_reason:[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def _hold_reason(repo_root: Path, rel: str) -> Optional[str]:
    """The ``plan_blitz_hold_reason`` text of a baton the gate already classified held."""
    try:
        head = (repo_root / rel).read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    found = _HOLD_REASON_RE.search(head)
    return found.group(1).strip("'\"") or None if found else None


def _held_of(repo_root: Path, report: Optional[Dict[str, Any]], stubs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Staged stubs the gate report lists under ``held`` (gate_v2's classification, not a second
    detector). Marks each stub row ``held`` with its ``hold_reason``; a held baton stays staged."""
    held_paths = set((report or {}).get("held") or [])
    held: List[Dict[str, Any]] = []
    for stub in stubs:
        stub["held"] = stub.get("path") in held_paths
        if stub["held"]:
            stub["hold_reason"] = _hold_reason(repo_root, stub["path"])
            held.append({"stub_id": stub["stub_id"], "path": stub["path"], "reason": stub["hold_reason"]})
    return held


def _waves_of(report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The report's own waves, in order, each baton id resolved to its report row, held batons
    dropped; none when the report was deferred."""
    if not report:
        return []
    held_paths = set(report.get("held") or [])
    rows = {b.get("id"): b for b in report.get("batons") or []}
    waves = [
        [bid for bid in wave if rows.get(bid, {}).get("path") not in held_paths]
        for wave in report.get("waves") or []
    ]
    return [
        {"index": i, "batons": [_wave_baton_row(rows.get(bid, {}), bid) for bid in wave]}
        for i, wave in enumerate(waves)
    ]


def _record_approval(repo_root: Path, approval: Dict[str, Any], trail_dir: str) -> Dict[str, str]:
    """Write ``<trail_dir>/approval.json`` once; a resume finds and returns the first record."""
    rel = f"{trail_dir.rstrip('/')}/approval.json"
    target = repo_root / rel
    if target.is_file():
        first = json.loads(target.read_text(encoding="utf-8"))
        return {"path": rel, "recorded_at": first["recorded_at"]}
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    target.parent.mkdir(parents=True, exist_ok=True)
    create_exclusive(target, json.dumps({**approval, "recorded_at": recorded_at}, indent=2) + "\n")
    return {"path": rel, "recorded_at": recorded_at}


def _changed_since_index(repo_root: Path, rels: List[str]) -> List[str]:
    """``rels`` minus tracked paths whose blob already equals the index's; an unreadable index
    filters nothing. Reads the index in process."""
    from coordinator_core.git.git_state import IndexParseError, read_index

    try:
        snapshot = read_index(repo_root)
    except (IndexParseError, OSError):
        return rels
    kept = []
    for rel in rels:
        entry = snapshot.get(rel)
        if entry is not None:
            data = (repo_root / rel).read_bytes()
            if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == entry.sha:
                continue
        kept.append(rel)
    return kept


def stage_roadmap(
    repo_root: Path,
    roadmap: str,
    *,
    edges_path: Optional[str] = None,
    branch: str = "main",
    commit: bool = True,
    approval: Optional[Dict[str, Any]] = None,
    trail_dir: Optional[str] = None,
    interaction_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Stage the roadmap at ``roadmap`` (its directory or any file in it).
    Returns ``{gate_report_path, stubs, numbering, folds, audit, commits, waves, ...}``.
    With ``approval`` the verdict is recorded under ``trail_dir`` first; a declined verdict stages
    nothing, an approved one stamps OVERVIEW.md and commits the roadmap directory with the stubs.
    ``held`` lists stubs the gate report holds (``{stub_id, path, reason}``); they stay in ``stubs``
    flagged ``held`` and appear in no wave. ``interaction_mode``, the run's, is written onto each
    sizing minted here."""
    repo_root = Path(repo_root)
    roadmap_path = Path(roadmap)
    if not roadmap_path.is_absolute():
        roadmap_path = repo_root / roadmap_path
    if not roadmap_path.exists():
        raise BlitzStageRefused(f"roadmap path does not exist: {roadmap}")
    run_dir, roadmap_id = _resolve_run_dir(roadmap_path)
    approval_record: Optional[Dict[str, str]] = None
    approved = approval is not None and approval.get("verdict") == "approved"
    if approval is not None:
        if not trail_dir:
            raise BlitzStageRefused("approval needs a trail_dir")
        if approved and not (run_dir / "OVERVIEW.md").is_file():
            raise BlitzStageRefused(f"no OVERVIEW.md under {run_dir} to stamp")
        approval_record = _record_approval(repo_root, approval, trail_dir)
        if not approved:
            return {
                "roadmap_id": roadmap_id,
                "approval_record": approval_record,
                "overview_stamped": False,
                "declined": True,
                "stubs": [],
                "commits": [],
                "commit_error": None,
                "waves": [],
            }
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
    prefix_match = _PREFIX_RE.search(clusters_text)
    if prefix_match:
        prefix = prefix_match.group(1)
    else:
        if _DATE_HEAD_RE.match(roadmap_id):
            prefix = _derive_date_led_prefix(roadmap_id)
            if prefix is None:
                raise BlitzStageRefused(
                    f"roadmap id {roadmap_id!r} is date-led and clusters.md has no `Stub slug prefix: `<x>`` line; "
                    "the default prefix would be the year and collide with every other date-led roadmap's stub ids"
                )
        else:
            prefix = roadmap_id.split("-")[0]
        other = _staged_other_roadmap(repo_root, prefix, roadmap_id)
        if other:
            raise BlitzStageRefused(
                f"stub prefix {prefix!r} is already staged by roadmap {other!r} "
                f"(roadmap-{prefix}-NN.md); add a `Stub slug prefix: `<x>`` line to clusters.md"
            )
    first_staging = not _has_staged(repo_root, prefix, roadmap_id)
    fold = fold_units(
        cluster_order, {c: by_id[c]["loe"] for c in cluster_order}, edges, tie_break=first_staging
    )
    unit_labels = [c for c in cluster_order if c in fold["units"]]
    unit_edges = [
        {"from": a, "to": b} for a in unit_labels for b in sorted(fold["blocked_by"][a], key=cluster_order.index)
    ]
    numbering = number_clusters(unit_labels, unit_edges, {})
    width = max(2, len(str(max(v["number"] for v in numbering.values()))))
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

    unfoldable_labels = {f["cluster"] for f in fold["flagged"] if f["reason"].startswith(UNFOLDABLE_SMALL)}
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
            interaction_mode,
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
            workstream=prefix,
        )
        content = _patch_frontmatter(
            content,
            sprint=info["sprint"],
            wave=info["wave"],
            loe=unit["loe"],
            blocked_by=row["blocked_by"],
            fold_flag=UNFOLDABLE_SMALL if label in unfoldable_labels else None,
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

    unfoldable_small = sorted(s["stub_id"] for s in stubs if s["label"] in unfoldable_labels)
    exit_code, out_lines, err_lines = run_audit(
        roadmap_id, repo_root, repo_root / "state", unfoldable_small=unfoldable_small
    )
    audit = {"exit_code": exit_code, "passed": exit_code == 0, "stdout": out_lines, "stderr": err_lines}

    commits: List[Dict[str, Any]] = []
    commit_error: Optional[str] = None
    new_paths = [p for s in stubs if s["created"] for p in (s["path"], s.get("sizing_object")) if p]
    overview_stamped = False
    if approved:
        overview = run_dir / "OVERVIEW.md"
        stamped = _patch_frontmatter(
            overview.read_text(encoding="utf-8"),
            set_fields={
                "status": "final-approved",
                "approver": approval["approver"],
                "approval_utterance": approval.get("utterance") or approval.get("ruling"),
            },
        )
        overview.write_text(stamped, encoding="utf-8")
        overview_stamped = True
        roadmap_files = sorted(
            Path(d, f).relative_to(repo_root).as_posix() for d, _, fs in os.walk(run_dir) for f in fs
        )
        new_paths += [p for p in _changed_since_index(repo_root, roadmap_files) if p not in new_paths]
    if commit and new_paths:
        sha, commit_error = _commit(
            repo_root, new_paths, f"roadmap-blitz-stage: scaffold {len(new_paths)} stub(s) for {roadmap_id}"
        )
        if sha:
            commits.append({"sha": sha, "paths": new_paths})

    frozen = _freeze_gate_report(
        repo_root, roadmap_id, [s["path"] for s in stubs], audit["passed"], unfoldable_small
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
    gate_report = frozen.pop("_report", None)
    approval_fields: Dict[str, Any] = (
        {"approval_record": approval_record, "overview_stamped": overview_stamped, "declined": False}
        if approval is not None
        else {}
    )
    return {
        "roadmap_id": roadmap_id,
        "edge_source": edge_source,
        **frozen,
        **approval_fields,
        "waves": _waves_of(gate_report),
        "held": _held_of(repo_root, gate_report, stubs),
        "stubs": stubs,
        "numbering": numbering,
        "cluster_numbering": cluster_numbering,
        "unfoldable_small": unfoldable_small,
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
