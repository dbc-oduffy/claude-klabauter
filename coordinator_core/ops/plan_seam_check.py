"""
coordinator_core.ops.plan_seam_check — JSON-RPC "plan.seam_check" and "plan.seam_record".

Purpose: the set-level seam check over plans someone means to run together. It
reports five finding classes (capability-without-ui-consumer, capabilities-undeclared,
unpromised-export, writes-collision, drifted-contract) and a verdict. The read/write
pair mirrors ``plan.prep_gate`` / ``plan.stamp_prepped``: ``plan.seam_check`` evaluates
and writes nothing; ``plan.seam_record`` evaluates inside the write so the reply and the
files describe one tree, writes one ``docs/plans/<plan-stem>.seam.yaml`` per plan,
atomically, and never commits.

Wire params (both ops):
    plans (list[str], required) — repo-relative plan paths, at least one.
    phase ("prep" | "fire" | "wave-boundary", required)
    named_set (bool, required) — True when someone chose the set; False only for
        mise-prep-run's own default set. Decides which collision and
        capabilities-undeclared findings block.
    landed_range ("<base-sha>..<head-sha>"), landed_rows ([{plan, row}]), wave (int)
        — required at wave-boundary, refused otherwise.

Reply fields:
    {"verdict": "CLEAN"|"REFUSED"|"DRIFT", "set": [plan], "per_plan": {plan: verdict},
     "findings": [{"plan", "class", "blocking", "counterpart_plan", "capability",
                   "missing_consumer", "path", "row", "detail"}],
     "phase", "wave", "checked_at_sha"}
    plan.seam_record adds "sidecars": [repo-relative paths written].

Negative-spec:
  - plan.seam_check opens no file for write.
  - Neither op commits or accepts a caller-supplied root.
  - Git is asked twice at most per call: one ``cat-file --batch-check`` for HEAD's
    sha and every path-tracked question, and at wave-boundary one name-only diff.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import yaml

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.cross_plan_write_overlap import _write_paths_from_rows
from coordinator_core.ops.dispatch_emit.spine_read import (
    UNDECLARED,
    SpineReadError,
    frontmatter_plan_edges,
    load_frontmatter_doc,
    load_rows_memo,
    read_spine,
    with_canonical_disposition,
)
from coordinator_core.ops.dispatch_emit.wave_map import _normalize_path


_DUMPER = getattr(yaml, "CSafeDumper", yaml.SafeDumper)

GENERATES: list = []
MUTATES = ["docs/plans/*.seam.yaml"]

SCHEMA_VERSION = "1.0.0"
_PHASES = ("prep", "fire", "wave-boundary")
_RANGE_RE = re.compile(r"^([0-9A-Fa-f]{7,64})\.\.([0-9A-Fa-f]{7,64})$")
_GIT_TIMEOUT_SECS = 0.4

CAP = "capability-without-ui-consumer"
UNDECLARED_CAPS = "capabilities-undeclared"
EXPORT = "unpromised-export"
COLLISION = "writes-collision"
DRIFT_CLASS = "drifted-contract"


def _finding(plan, cls, blocking, detail, *, counterpart=None, capability=None,
             missing=None, path=None, row=None) -> dict:
    return {
        "plan": plan, "class": cls, "blocking": bool(blocking), "counterpart_plan": counterpart,
        "capability": capability, "missing_consumer": missing, "path": path, "row": row,
        "detail": detail,
    }


def _ancestors_or_self(norm: str):
    yield norm
    cur = norm
    while True:
        parent = posixpath.dirname(cur)
        if not parent or parent == cur:
            break
        yield parent
        cur = parent
    if norm != ".":
        yield "."


class _Plan:
    """One plan of the set (or a plan it names): text, frontmatter, raw rows, live rows."""

    def __init__(self, rel: str, root: Path, path: Optional[Path] = None):
        self.rel = rel
        self.path = path if path is not None else root / rel
        self.text = self.path.read_text(encoding="utf-8")
        split = split_frontmatter(self.text)
        doc = None
        if split is not None:
            try:
                doc = load_frontmatter_doc(split.fm_text)
            except yaml.YAMLError:
                doc = None
        self.fm: dict = doc if isinstance(doc, dict) else {}
        loaded = load_rows_memo(self.text)
        self.raw_rows: List[dict] = list(loaded.rows) if loaded.status is LocateStatus.LOCATED else []
        self.raw_by_id: Dict[str, dict] = {
            r["id"]: with_canonical_disposition(r) for r in self.raw_rows if r.get("id")
        }
        self._live = None
        self._promised: Optional[tuple] = None
        self.unreadable: Optional[str] = None

    @property
    def live_rows(self) -> list:
        return self.load_rows()

    def load_rows(self) -> list:
        """`read_spine`'s rows; empty, with `unreadable` set, when it rejects the spine."""
        if self._live is None:
            try:
                self._live = read_spine(self.path, schema_preflight=False)
            except SpineReadError as exc:
                self._live = []
                self.unreadable = str(exc)
        return self._live

    def edge_plans(self) -> set:
        edges = list(frontmatter_plan_edges(self.text) or [])
        for row in self.raw_rows:
            edges.extend(row.get("depends_on_plan") or [])
        targets = {
            posixpath.normpath(str(e["plan"]).replace("\\", "/"))
            for e in edges
            if isinstance(e, dict) and isinstance(e.get("plan"), str) and e["plan"].strip()
        }
        return {t for t in targets if ".." not in t.split("/") and not t.startswith("/")}

    def promised_paths(self) -> tuple:
        """(exact normalized paths, normalized prefixes) of every row not closed as wont_do/voided."""
        if self._promised is None:
            paths: set = set()
            prefixes: set = set()
            for r in self.raw_rows:
                if r.get("disposition") not in ("wont_do", "voided"):
                    p, q = _raw_promises(r)
                    paths |= p
                    prefixes |= q
            self._promised = (paths, prefixes)
        return self._promised


def _declared(raw: dict):
    writes = raw.get("writes")
    return writes if isinstance(writes, list) and writes else UNDECLARED


def _raw_promises(raw: dict) -> tuple:
    """(exact paths, prefixes) one raw row declares."""
    return _paths_and_prefixes([SimpleNamespace(writes=_declared(raw), writes_under=raw.get("writes_under") or ())])


def _paths_and_prefixes(rows) -> tuple:
    paths = _write_paths_from_rows(rows)
    prefixes = {
        _normalize_path(p)
        for r in rows
        for p in (getattr(r, "writes_under", ()) or ())
        if isinstance(p, str) and p
    }
    return paths, prefixes


def _covers(paths: set, prefixes: set, norm: str) -> bool:
    """True when ``norm`` is written exactly, sits under a written prefix, or is a directory a write sits under."""
    if norm in paths or any(a in prefixes for a in _ancestors_or_self(norm)):
        return True
    stem = norm + "/"
    return any(p.startswith(stem) for p in paths) or any(p.startswith(stem) for p in prefixes)


class _Tree:
    """HEAD's sha and 'is this path in the tree at <rev>', answered by ONE batched cat-file."""

    def __init__(self, root: Path, head_rev: str):
        self.root = root
        self.head_rev = head_rev
        self.wanted: set = set()
        self.present: Optional[set] = None
        self.sha: Optional[str] = None

    def want(self, path: str) -> None:
        self.wanted.add(path)

    def resolve(self) -> None:
        specs = sorted(self.wanted)
        lines = ["HEAD"] + [f"HEAD:{p}" for p in specs]
        if self.head_rev != "HEAD":
            lines += [f"{self.head_rev}:{p}" for p in specs]
        result = run_git(
            ["-C", str(self.root), "cat-file", "--batch-check"],
            input=("\n".join(lines) + "\n").encode("utf-8"),
            timeout=_GIT_TIMEOUT_SECS,
        )
        out = result.stdout.split("\n")
        self.present = set()
        self.sha = None
        if not result.ok or len(out) < len(lines):
            return
        first = out[0].split()
        if len(first) >= 2 and first[1] == "commit":
            self.sha = first[0]
        for line, spec in zip(out[1:], lines[1:]):
            if not line.endswith(" missing") and not line.endswith(" ambiguous"):
                self.present.add(spec)

    def at(self, rev: str, path: str) -> bool:
        return f"{rev}:{path}" in (self.present or ())


def _contained_rel(raw: str, root: Path) -> Optional[str]:
    """``raw`` as a normalized repo-relative posix path, or None when it escapes ``root``."""
    path = Path(raw.strip())
    if not path.is_absolute():
        path = root / path
    resolved = contained_path(path, [root])
    if resolved is None:
        return None
    return resolved.relative_to(contained_path(root, [root]) or root).as_posix()


def _parse_params(params: dict, root: Path, *, record: bool = False) -> dict:
    plans_raw = params.get("plans")
    if not isinstance(plans_raw, list) or not plans_raw or not all(
        isinstance(p, str) and p.strip() for p in plans_raw
    ):
        raise ValueError("plans must be a non-empty list of repo-relative plan paths")
    plans: List[str] = []
    passed: Dict[str, str] = {}
    for raw in plans_raw:
        rel = _contained_rel(raw, root)
        if rel is None:
            raise ValueError(f"plan escapes the resolved worktree: {raw!r}")
        if not (root / rel).is_file() or not rel.endswith(".md"):
            raise ValueError(f"no such plan: {raw!r}")
        if record and posixpath.dirname(rel) != "docs/plans":
            raise ValueError(f"plan.seam_record writes beside docs/plans/ plans only: {raw!r}")
        if rel not in plans:
            plans.append(rel)
            passed[rel] = raw.strip()
    phase = params.get("phase")
    if phase not in _PHASES:
        raise ValueError(f"phase must be one of {', '.join(_PHASES)}")
    named_set = params.get("named_set")
    if not isinstance(named_set, bool):
        raise ValueError("named_set must be a bool")
    wave_keys = ("landed_range", "landed_rows", "wave")
    present = [k for k in wave_keys if params.get(k) is not None]
    out = {"plans": plans, "passed": passed, "phase": phase, "named_set": named_set,
           "landed_range": None, "landed_rows": [], "wave": None}
    if phase != "wave-boundary":
        if present:
            raise ValueError(f"{', '.join(present)} are accepted only at phase wave-boundary")
        return out
    missing = [k for k in wave_keys if params.get(k) is None]
    if missing:
        raise ValueError(f"{', '.join(missing)} required at phase wave-boundary")
    if not isinstance(params["landed_range"], str) or not _RANGE_RE.match(params["landed_range"]):
        raise ValueError("landed_range must be '<base-sha>..<head-sha>'")
    wave = params["wave"]
    if isinstance(wave, bool) or not isinstance(wave, int):
        raise ValueError("wave must be an integer")
    rows = params["landed_rows"]
    if not isinstance(rows, list) or not all(
        isinstance(r, dict) and isinstance(r.get("plan"), str) and isinstance(r.get("row"), str)
        for r in rows
    ):
        raise ValueError("landed_rows must be a list of {plan, row}")
    landed = []
    for r in rows:
        rel = _contained_rel(r["plan"], root)
        if rel is None:
            raise ValueError(f"landed row plan escapes the resolved worktree: {r['plan']!r}")
        landed.append((rel, r["row"]))
    out.update(landed_range=params["landed_range"], landed_rows=landed, wave=wave)
    return out


def _capability_findings(plan: _Plan, pset: Dict[str, _Plan], ctx: dict, tree: _Tree, deferred: list) -> list:
    findings: list = []
    if "capabilities" not in plan.fm:
        blocking = ctx["phase"] == "fire" or (ctx["phase"] == "prep" and ctx["named_set"])
        findings.append(_finding(plan.rel, UNDECLARED_CAPS, blocking,
                                 "plan frontmatter declares no capabilities key"))
        return findings
    caps = plan.fm["capabilities"]
    if not isinstance(caps, list):
        return [_finding(plan.rel, CAP, True, "capabilities is not a list", missing="capabilities")]
    for entry in caps:
        if not isinstance(entry, dict):
            findings.append(_finding(plan.rel, CAP, True, "capability entry is not a mapping",
                                     missing="malformed entry"))
            continue
        cap_id = str(entry.get("id") or "")
        role, click = entry.get("role"), entry.get("click_path")
        label = f"{click or '<no click_path>'} (role {role or '<no role>'})"
        carve, consumer = entry.get("ui_carve_out"), entry.get("ui_consumer")
        if not (isinstance(role, str) and role.strip()) or not (isinstance(click, str) and click.strip()):
            findings.append(_finding(plan.rel, CAP, True, "role or click_path is empty",
                                     capability=cap_id, missing=label))
            continue
        if carve is not None:
            if consumer is not None or not isinstance(carve, str) or not carve.strip():
                findings.append(_finding(plan.rel, CAP, True,
                                         "exactly one of ui_consumer and a non-empty ui_carve_out is required",
                                         capability=cap_id, missing=label))
            else:
                findings.append(_finding(plan.rel, CAP, False, f"ui_carve_out: {carve.strip()}",
                                         capability=cap_id, missing=label))
            continue
        problem = _consumer_problem(plan, consumer, pset, ctx, tree, deferred)
        if problem is not None:
            findings.append(_finding(plan.rel, CAP, True, problem[1], capability=cap_id,
                                     missing=f"{label}: {problem[0]}"))
    return findings


def _consumer_problem(plan, consumer, pset, ctx, tree, deferred):
    """(reference, reason) when the consumer does not resolve, else None. A ``shipped`` path
    is queued on ``deferred`` and judged after the batched tree read."""
    if not isinstance(consumer, dict):
        return "no ui_consumer or ui_carve_out", "capability declares neither ui_consumer nor ui_carve_out"
    if isinstance(consumer.get("shipped"), str) and consumer["shipped"].strip():
        path = consumer["shipped"].strip().replace("\\", "/")
        tree.want(path)
        deferred.append((plan, consumer, path))
        return None
    chunk = consumer.get("chunk")
    if not isinstance(chunk, str) or not chunk:
        return "ui_consumer", "ui_consumer names neither shipped nor a chunk"
    rel = str(consumer.get("plan") or plan.rel).replace("\\", "/")
    ref = f"{rel} {chunk}"
    if ".." in rel.split("/") or rel.startswith("/"):
        return ref, "ui_consumer plan path escapes the repo"
    target = pset.get(rel)
    in_set = target is not None
    if target is None:
        target = _load_outside(ctx, rel)
    if target is None:
        return ref, f"consumer plan {rel} does not exist"
    row = target.raw_by_id.get(chunk)
    if row is None:
        return ref, f"no row {chunk} in {rel}"
    disposition = row.get("disposition")
    if disposition in ("wont_do", "voided"):
        return ref, f"consumer row {chunk} is {disposition}"
    if not in_set and disposition != "coded":
        return ref, f"consumer plan {rel} is outside the set and row {chunk} is not coded"
    writes = row.get("writes")
    under = row.get("writes_under")
    if not (isinstance(writes, list) and writes) and not (isinstance(under, list) and under):
        return ref, f"consumer row {chunk} declares no write"
    return None


def _load_outside(ctx: dict, rel: str) -> Optional[_Plan]:
    cache = ctx["outside"]
    if rel not in cache:
        root: Path = ctx["root"]
        path = root / rel
        if not path.is_file():
            from coordinator_core.roadmap.plan_gate import archived_plan_path

            path = archived_plan_path(root, rel) or path
        cache[rel] = _Plan(rel, root, path) if path.is_file() else None
    return cache[rel]


def _promised_by(rel: str, pset: Dict[str, _Plan], ctx: dict) -> tuple:
    plan = pset.get(rel) or _load_outside(ctx, rel)
    return plan.promised_paths() if plan is not None else (set(), set())


def _consumed(plan: _Plan, row) -> set:
    """Normalized paths the raw row declares under ``consumes:`` (``row.reads`` also folds in legacy ``reads:``)."""
    raw = plan.raw_by_id.get(row.id) or {}
    items = raw.get("consumes")
    return {_normalize_path(p) for p in items if isinstance(p, str) and p} if isinstance(items, list) else set()


def _export_findings(plan: _Plan, pset, ctx, tree: _Tree, pending: list) -> None:
    own_paths, own_prefixes = plan.promised_paths()
    # Every plan's promises in one index, built once per call: a path the
    # plan's own rows cover is skipped before this is read, so the union
    # answers "another plan in the set writes it".
    if "set_promised" not in ctx:
        all_paths: set = set()
        all_prefixes: set = set()
        for other in pset.values():
            paths, prefixes = other.promised_paths()
            all_paths |= paths
            all_prefixes |= prefixes
        ctx["set_promised"] = (all_paths, all_prefixes)
    set_paths, set_prefixes = ctx["set_promised"]
    if "siblings" not in ctx:
        from coordinator_core.roadmap.prep_gate import fleet_siblings

        ctx["siblings"] = {n.casefold() for n in fleet_siblings(ctx["root"])}
    dep_plans = plan.edge_plans()
    for row in plan.live_rows:
        consumed = _consumed(plan, row)
        for raw in row.reads or ():
            if not isinstance(raw, str) or not raw:
                continue
            norm = _normalize_path(raw)
            # A sibling repo's path is a cross-repo read: the per-plan bar's
            # external-gate leg owns it, and this tree cannot answer for it.
            if norm.split("/", 1)[0].casefold() in ctx["siblings"]:
                continue
            if _covers(own_paths, own_prefixes, norm):
                continue
            if _covers(set_paths, set_prefixes, norm) or any(
                _covers(*_promised_by(d, pset, ctx), norm) for d in dep_plans
            ):
                continue
            tree.want(raw)
            pending.append((plan, row, raw, norm not in consumed))


def _edge_closure(pset: Dict[str, _Plan]) -> Dict[str, set]:
    """Per plan, every set member it reaches through ``depends_on_plan`` edges, transitively."""
    direct = {rel: {d for d in p.edge_plans() if d in pset} for rel, p in pset.items()}
    closure: Dict[str, set] = {}
    for rel in pset:
        seen: set = set()
        stack = list(direct[rel])
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(direct[cur])
        closure[rel] = seen
    return closure


def _collision_findings(pset: Dict[str, _Plan], ctx: dict) -> list:
    findings: list = []
    sets = {rel: _paths_and_prefixes(p.live_rows) for rel, p in pset.items()}
    exact: Dict[str, set] = {}
    prefix: Dict[str, set] = {}
    for rel, (paths, prefixes) in sets.items():
        for p in paths:
            exact.setdefault(p, set()).add(rel)
        for p in prefixes:
            prefix.setdefault(p, set()).add(rel)
    ordered = _edge_closure(pset)
    seen: set = set()
    for rel, (paths, prefixes) in sets.items():
        for item in sorted(paths | prefixes):
            hits = set(exact.get(item, ()))
            for anc in _ancestors_or_self(item):
                hits |= prefix.get(anc, set())
            if item in prefixes:
                hits |= prefix.get(item, set())
            for other in hits - {rel}:
                if other in ordered[rel] or rel in ordered[other]:
                    continue
                key = (rel, other, item)
                if key in seen:
                    continue
                seen.add(key)
                seen.add((other, rel, item))
                for a, b in ((rel, other), (other, rel)):
                    findings.append(_finding(
                        a, COLLISION, ctx["named_set"],
                        f"{a} and {b} both declare writes to {item} with no depends_on_plan edge between them",
                        counterpart=b, path=item))
    if not ctx["named_set"]:
        findings = _grouped_collisions(findings)
    for rel, plan in pset.items():
        for row in plan.live_rows:
            if row.writes is UNDECLARED:
                findings.append(_finding(rel, COLLISION, False, "undetermined", row=row.id))
    return findings


def _grouped_collisions(findings: list) -> list:
    """One record per (plan, path) over the default set, naming the first
    other writer and counting the rest.

    Nothing blocks there, and one finding per pair is quadratic in the
    writers of a shared file: ~45k records over a 300-plan backlog.
    """
    others: Dict[tuple, list] = {}
    for f in findings:
        others.setdefault((f["plan"], f["path"]), []).append(f["counterpart_plan"])
    grouped = []
    for (plan, path), writers in others.items():
        writers = sorted(set(writers))
        detail = f"{len(writers) + 1} plans declare writes to {path} with no depends_on_plan edge between them"
        if len(writers) > 1:
            detail += f"; also {', '.join(writers[1:])}"
        grouped.append(_finding(plan, COLLISION, False, detail, counterpart=writers[0], path=path))
    return grouped


def _landed_writes(ctx: dict, pset) -> tuple:
    """Rows named landed: their declared paths and prefixes, and the first landing plan per exact path."""
    paths, prefixes = set(), set()
    exact_by_path: Dict[str, str] = {}
    for rel, row_id in ctx["landed_rows"]:
        plan = pset.get(rel) or _load_outside(ctx, rel)
        raw = plan.raw_by_id.get(row_id) if plan is not None else None
        if raw is None:
            continue
        p, q = _raw_promises(raw)
        paths |= p
        prefixes |= q
        for item in p:
            exact_by_path.setdefault(item, rel)
    return paths, prefixes, exact_by_path


def _drift_findings(pset, ctx, tree: _Tree, touched: list, landed_head: str) -> list:
    findings: list = []
    landed = set(ctx["landed_rows"])
    land_paths, land_prefixes, landed_by_path = _landed_writes(ctx, pset)
    touched_norm = {_normalize_path(t): t for t in touched}
    for rel, plan in pset.items():
        for row in plan.live_rows:
            if (rel, row.id) in landed:
                continue
            consumed = _consumed(plan, row)
            for raw in row.reads or ():
                if not isinstance(raw, str) or not raw:
                    continue
                norm = _normalize_path(raw)
                promiser = landed_by_path.get(norm) if norm in consumed else None
                if promiser is not None and not tree.at(landed_head, raw):
                    findings.append(_finding(
                        rel, DRIFT_CLASS, True,
                        f"{raw} was promised by a landed row of {promiser} and is absent from the tree",
                        counterpart=promiser, path=raw, row=row.id))
            row_writes, row_prefixes = _paths_and_prefixes([row])
            row_reads = {_normalize_path(p) for p in (row.reads or ()) if isinstance(p, str) and p}
            row_head = {_normalize_path(p) for p in (row.reads_at_head or ()) if isinstance(p, str) and p}
            for tnorm, traw in touched_norm.items():
                if tnorm in land_paths or any(a in land_prefixes for a in _ancestors_or_self(tnorm)):
                    continue
                blocking_hit = _hits(tnorm, row_writes | row_reads, row_prefixes)
                head_hit = _hits(tnorm, row_head, set())
                if blocking_hit or head_hit:
                    findings.append(_finding(
                        rel, DRIFT_CLASS, bool(blocking_hit),
                        f"the landed range touched {traw}, which no landed row declared"
                        + ("" if blocking_hit else " (row only reads_at_head)"),
                        path=traw, row=row.id))
    return findings


def _hits(touched: str, paths: set, prefixes: set) -> bool:
    return any(a in paths or a in prefixes for a in _ancestors_or_self(touched))


def _evaluate(params: dict, root: Path, *, record: bool = False) -> dict:
    ctx = _parse_params(params, root, record=record)
    ctx.update(root=root, outside={})
    pset: Dict[str, _Plan] = {rel: _Plan(rel, root) for rel in ctx["plans"]}
    landed_head = "HEAD"
    if ctx["phase"] == "wave-boundary":
        landed_head = _RANGE_RE.match(ctx["landed_range"]).group(2)
    tree = _Tree(root, landed_head)

    findings: list = []
    deferred: list = []
    pending: list = []
    for plan in pset.values():
        findings += _capability_findings(plan, pset, ctx, tree, deferred)
        _export_findings(plan, pset, ctx, tree, pending)
    findings += _collision_findings(pset, ctx)
    # A spine `read_spine` rejects leaves the plan's writes and consumes
    # unknown, the case the contract records as an undetermined collision.
    # It blocks only in a set someone chose; over the default backlog one
    # malformed plan must not refuse every other plan's certification.
    for plan in pset.values():
        plan.load_rows()
        if plan.unreadable is not None:
            findings.append(_finding(
                plan.rel, COLLISION, ctx["named_set"],
                f"undetermined: spine unreadable: {plan.unreadable}"))

    touched: list = []
    if ctx["phase"] == "wave-boundary":
        diff = run_git(
            ["-C", str(root), "diff", "--name-only", "--no-renames", "-z", ctx["landed_range"]],
            timeout=_GIT_TIMEOUT_SECS,
        )
        if not diff.ok:
            raise ValueError(f"cannot diff landed_range {ctx['landed_range']}")
        touched = [p for p in diff.stdout.split("\0") if p]
        for plan in pset.values():
            for row in plan.live_rows:
                for p in row.reads or ():
                    if isinstance(p, str) and p:
                        tree.want(p)
    tree.resolve()
    if tree.sha is None:
        raise ValueError("cannot resolve HEAD")

    for plan, consumer, path in deferred:
        if not tree.at("HEAD", path):
            cap = next((e for e in plan.fm.get("capabilities", [])
                        if isinstance(e, dict) and e.get("ui_consumer") is consumer), {})
            label = f"{cap.get('click_path')} (role {cap.get('role')}): shipped {path}"
            findings.append(_finding(plan.rel, CAP, True, f"shipped path {path} is not tracked at HEAD",
                                     capability=str(cap.get("id") or ""), missing=label))
    for plan, row, raw, legacy in pending:
        if tree.at("HEAD", raw):
            continue
        source = "reads: (legacy, not consumes:)" if legacy else "consumes"
        findings.append(_finding(
            plan.rel, EXPORT, not legacy,
            f"row {row.id} {source} {raw}, which no row of its plan or of another plan in the set writes "
            "and which is not tracked at HEAD",
            path=raw, row=row.id))
    if ctx["phase"] == "wave-boundary":
        findings += _drift_findings(pset, ctx, tree, touched, landed_head)

    blocked = {f["plan"] for f in findings if f["blocking"]}
    bad = "DRIFT" if ctx["phase"] == "wave-boundary" else "REFUSED"
    per_plan = {rel: (bad if rel in blocked else "CLEAN") for rel in ctx["plans"]}
    findings.sort(key=lambda f: (ctx["plans"].index(f["plan"]), f["class"], f["path"] or "", f["row"] or ""))
    alias = ctx["passed"]
    for f in findings:
        f["plan"] = alias[f["plan"]]
        f["counterpart_plan"] = alias.get(f["counterpart_plan"], f["counterpart_plan"])
    return {
        "verdict": bad if blocked else "CLEAN",
        "set": [alias[r] for r in ctx["plans"]],
        "per_plan": {alias[r]: v for r, v in per_plan.items()},
        "findings": findings,
        "phase": ctx["phase"],
        "wave": ctx["wave"],
        "checked_at_sha": tree.sha,
    }


def _sidecar(reply: dict, rel: str, findings: list) -> dict:
    keys = ("class", "blocking", "counterpart_plan", "capability", "missing_consumer", "path", "row", "detail")
    return {
        "schema": "seam-check",
        "schema_version": SCHEMA_VERSION,
        "plan": rel,
        "phase": reply["phase"],
        "wave": reply["wave"],
        "checked_at_sha": reply["checked_at_sha"],
        "set": reply["set"],
        "verdict": reply["per_plan"][rel],
        "findings": [{k: f[k] for k in keys} for f in findings],
    }


@register_op("plan.seam_check")
def _check_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.seam_check" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.seam_check requires a resolved repo_root")
    return _evaluate(params, Path(main_worktree_root(repo_root)))


@register_op("plan.seam_record")
def _record_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.seam_record" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.seam_record requires a resolved repo_root")
    root = Path(main_worktree_root(repo_root))
    reply = _evaluate(params, root, record=True)
    written: List[str] = []
    by_plan: Dict[str, list] = {}
    for f in reply["findings"]:
        by_plan.setdefault(f["plan"], []).append(f)
    for rel in reply["set"]:
        target = (root / rel).with_name(Path(rel).stem + ".seam.yaml")
        body = yaml.dump(_sidecar(reply, rel, by_plan.get(rel, [])), Dumper=_DUMPER, sort_keys=False, allow_unicode=True)
        atomic_write_bytes(target, body.encode("utf-8"))
        written.append(target.resolve().relative_to(root.resolve()).as_posix())
    return {**reply, "sidecars": written}
