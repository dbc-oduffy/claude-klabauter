"""
coordinator_core.roadmap.gate_v2 -- the plan-gate report.

Purpose: answers, from disk alone, the two gate questions per roadmap baton (may PLANNING
start, may EXECUTION start), numbers the planning waves, selects the plan-blitz candidates and
links each to its plan. The report shape is requirement 9 of
docs/research/2026-10-18-plan-gate-requirement-decision.md; ``compute_gate_report`` is the
entry point every caller builds against and ``GateReadRefused`` is its refusal.

Read shape (the budget is 500ms process time, zero spawns): header-only reads (first 6KB of a
handoff, first 2KB of a plan) with a full frontmatter parse only for candidates and closure
nodes; a blocker id resolves filename-first (live ids, then archive file stems) and the
archived-header id index is built only on the first miss; the plan-header index is built
lazily, at most once, and only when a baton has no ``governing_plan`` file of its own. The
tracked set is ``git_state.read_index`` (an in-process ``.git/index`` parse).

Invariants:
  - Fail closed. A blocker id naming no record closes BOTH gates and lands in
    ``unresolved_blockers``; an unreadable handoff dir, an unparseable candidate or an
    unresolvable ``subject`` raises ``GateReadRefused``. Never an open gate, an empty wave or
    "no blockers" for a read that could not answer. The one exception is an unreadable git
    index, which withholds nothing on ``untracked`` grounds and says nothing.
  - ``roadmap_id``, ``targets`` and ``subject`` narrow the report and the candidate set, never
    blocker reach: closure and wave numbering run over the whole corpus.
  - Closure stops at a coded blocker: its own blockers are satisfied by definition.
  - No fuzzy plan link: ``plan_gate._PLAN_LINK_ORDER`` is the only basis order.
  - Per-field cleanup is the hot cost. Do not call ``plan_gate._strip_comment``/``_unquote`` on
    every header line; ``_val`` strips a comment only when a ``#`` is present.

Negative-spec: no subprocess import, no import of ``ops.roadmap_plan_gate`` (the op imports
this module, never the reverse), no persisted index. The key sets below are NOT
``plan_gate._BATON_FIELDS`` (the read-side frontmatter field set).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from coordinator_core.git.git_state import IndexParseError, read_index_paths
from coordinator_core.roadmap import plan_gate as pg
from coordinator_core.session.record_homes import home_dir


class GateReadRefused(Exception):
    """The gate cannot answer this read; a caller turns it into a refusal, never an open gate."""


# Nested shapes: plan{path,status,link_basis}, planning_gate{open,blocking[]},
# execution_gate{open,blocking[]}, unlinked_plan_claim{path,cause,status} | None.
BATON_REPORT_KEYS = frozenset({
    "id", "path", "title", "stub_id", "status", "deployment_state", "candidate",
    "plan", "needs_plan", "execution_authorized", "sized", "sizing_objects",
    "planning_gate", "execution_gate", "unlinked_plan_claim", "ambiguous_plan_link",
    "held", "waiting_on_execution",
})

# Nested shape: counts{candidates,planning_open,execution_open,unschedulable}.
REPORT_KEYS = frozenset({
    "batons", "waves", "cycles", "unresolved_blockers", "untracked", "held",
    "counts", "unmatched_targets", "verdict",
})


_HEAD_BYTES = 6144
_PLAN_HEAD_BYTES = 2048
# A frontmatter block that outgrows the first window is read whole up to this cap before the
# slow line scanner is considered; a block past the cap still falls back, so no field drops.
_LONG_HEAD_BYTES = 65536
_HEAD_WANT = frozenset({
    "baton_role", "deployment_state", "status", "roadmap_id", "stub_id", "handoff_id",
    "deliverable_id", "replan_of", "superseded_by",
})
_PLAN_WANT = frozenset({"title", "status", "plan_id", "deliverable_id", "sizing_object", "kind", "plan"})
_ID_WANT = frozenset({"stub_id", "handoff_id", "deliverable_id"})
_TERMINAL_STATUS = frozenset({"claimed", "consumed", "superseded", "closed", "shipped", "done"})
# A sizing route that holds no planning content: the baton waits for its execution gate.
_NO_PLAN_ROUTES = frozenset({"dispatch"})
_KEY_RES: Dict[frozenset, "re.Pattern[str]"] = {}


def _key_re(wanted: frozenset) -> "re.Pattern[str]":
    """A line matcher for the ``wanted`` top-level keys only (a match per unwanted key is the
    cost this avoids); built once per key set."""
    pat = _KEY_RES.get(wanted)
    if pat is None:
        alt = "|".join(re.escape(k) for k in sorted(wanted))
        pat = _KEY_RES[wanted] = re.compile(rf"^({alt}):[ \t]*(.*)$", re.M)
    return pat


# ---------------------------------------------------------------------------
# Record readers
# ---------------------------------------------------------------------------


def _val(raw: str) -> Any:
    """One header value: comment dropped only if a ``#`` is present, quotes removed, null -> ''."""
    v = raw.strip()
    if "#" in v:
        v = pg._strip_comment(v).strip()
    if not v:
        return ""
    if v[0] == "[" and v[-1] == "]":
        return [item for item in (pg._unquote(i) for i in v[1:-1].split(",")) if item]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return "" if v in pg._NULL_SCALARS else v


def _head_fields(path, nbytes: int, wanted: frozenset) -> Dict[str, Any]:
    """The ``wanted`` frontmatter keys from the first ``nbytes`` of ``path``.

    Raises OSError when the file cannot be read. A record whose frontmatter does not start the
    file or does not close inside the window falls back to the full narrow scanner, so a long
    header can never drop a field silently.
    """
    with open(path, "rb") as fh:
        want = nbytes
        data = fh.read(want)
        while data.startswith(b"---") and data.find(b"\n---", 3) == -1 and len(data) == want < _LONG_HEAD_BYTES:
            want = min(want * 4, _LONG_HEAD_BYTES)
            data += fh.read(want - len(data))
    return _fields_from_text(path, data.decode("utf-8", "replace"), wanted)


def _fields_from_text(path, text: str, wanted: frozenset) -> Dict[str, Any]:
    """``_head_fields`` over already-read ``text``; ``path`` is only the slow-scanner fallback."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            out: Dict[str, Any] = {}
            for m in _key_re(wanted).finditer(text, 3, end + 1):
                key = m.group(1)
                if key not in out:
                    raw = m.group(2).lstrip()
                    if raw[:1] in ("|", ">"):
                        return pg._scan_fields(path, wanted)
                    out[key] = _val(raw)
            return out
    return pg._scan_fields(path, wanted)


def _baton_fields(path) -> Dict[str, Any]:
    """``plan_gate._read_baton_fields`` for the common record, without its per-line scan.

    One regex pass over the closed frontmatter block. Any shape the narrow scanner reads
    differently (a repeated key, a bare ``key:`` opening a block list, a block scalar, a quoted
    value with ``''`` or a backslash escape, no leading ``---``, a block past the cap) is handed to
    ``pg._scan_fields`` whole, so the two readers cannot disagree.
    """
    try:
        with open(path, "rb") as fh:
            text = fh.read(_LONG_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            out: Dict[str, Any] = {}
            for m in _key_re(pg._BATON_FIELDS).finditer(text, 3, end + 1):
                key = m.group(1)
                raw = m.group(2).strip()
                cv = pg._strip_comment(raw) if "#" in raw else raw
                if (
                    key in out
                    or not cv
                    or cv[0] in "|>"
                    or (cv[0] in "\"'" and ("''" in cv or "\\" in cv))
                ):
                    return pg._scan_fields(path, pg._BATON_FIELDS)
                out[key] = _val(raw)
            return out
    return pg._scan_fields(path, pg._BATON_FIELDS)


def _s(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _list_md(directory: str, recursive: bool) -> List[Tuple[str, str]]:
    """Sorted ``(dirpath, filename)`` pairs for ``*.md`` under ``directory``; OSError propagates
    for the top directory only (a missing archive is an empty one, decided by the caller)."""
    out: List[Tuple[str, str]] = []
    if not recursive:
        with os.scandir(directory) as it:
            for e in it:
                if e.name.endswith(".md") and e.is_file():
                    out.append((directory, e.name))
        out.sort(key=lambda p: p[1])
        return out
    for dp, dn, fns in os.walk(directory):
        dn.sort()
        for fn in sorted(fns):
            if fn.endswith(".md"):
                out.append((dp, fn))
    return out


class _Rec:
    __slots__ = ("path", "rel", "live")

    def __init__(self, path: Path, rel: str, live: bool) -> None:
        self.path = path
        self.rel = rel
        self.live = live


class _Info:
    """One reported baton: its row plus the gate internals the wave stage needs."""

    __slots__ = ("rec", "row", "planning_blocking", "execution_blocking", "unresolved")

    def __init__(self, rec, row, planning_blocking, execution_blocking, unresolved) -> None:
        self.rec = rec
        self.row = row
        self.planning_blocking = planning_blocking
        self.execution_blocking = execution_blocking
        self.unresolved = unresolved


class _PlanIndexFast(pg.PlanIndex):
    """Live plan index whose archived half is also built with the cheap header reader."""

    def archived(self) -> pg.PlanIndex:
        if self._archived is None:
            idx = pg.PlanIndex()
            if self.worktree_root is not None:
                _fill_plans(idx, self.worktree_root, "archive/specs")
            idx.shared_sizing = self.shared_sizing
            self._archived = idx
        return self._archived


def _fill_plans(idx: pg.PlanIndex, root: Path, subdir: str) -> None:
    base = root.as_posix().rstrip("/") + "/"
    target = base + subdir
    if not os.path.isdir(target):
        return
    for dp, fn in _list_md(target, True):
        rel = (dp.replace("\\", "/") + "/" + fn)[len(base):]
        try:
            fm = _head_fields(base + rel, _PLAN_HEAD_BYTES, _PLAN_WANT)
        except OSError:
            continue
        if fm and pg.is_plan_record(fm):
            idx.add(rel, fm)


class _Gate:
    """Per-call read state: heads, resolution caches and the lazily built indexes."""

    def __init__(self, root: Path, roadmap_id, targets, subject) -> None:
        self.root = root
        self.base = root.as_posix().rstrip("/") + "/"
        self.roadmap_id = roadmap_id or None
        self.targets = [t for t in (targets or []) if isinstance(t, str) and t.strip()]
        self.subject = subject or None
        self.heads: Dict[str, Dict[str, Any]] = {}
        self.recs: List[_Rec] = []
        self.id_live: Dict[str, _Rec] = {}
        self.arch_stems: Dict[str, Path] = {}
        self.arch_ids: Optional[Dict[str, Path]] = None
        self.fms: Dict[str, Dict[str, Any]] = {}
        self.resolved: Dict[str, Optional[_Rec]] = {}
        self.states: Dict[str, str] = {}
        self.disps: Dict[str, str] = {}
        self.links: Dict[str, Tuple[Optional[dict], Optional[dict]]] = {}
        self.infos: Dict[str, _Info] = {}
        self._tracked: Any = False
        self._plans: Optional[_PlanIndexFast] = None
        self._arch_sizings: Optional[Dict[str, str]] = None
        self.replan_sources: Set[str] = set()
        self._scan_live()
        self._scan_archive_names()

    def _rel(self, path: Path) -> str:
        return path.as_posix()[len(self.base):]

    def _scan_live(self) -> None:
        live_dir = Path(home_dir(str(self.root), "handoffs")).as_posix()
        rel_dir = live_dir[len(self.base):]
        try:
            names = _list_md(live_dir, False)
        except OSError as exc:
            raise GateReadRefused(f"live handoff dir unreadable: {exc}") from exc
        for dp, fn in names:
            full = dp + "/" + fn
            path = Path(full)
            rel = f"{rel_dir}/{fn}"
            try:
                head = _head_fields(full, _HEAD_BYTES, _HEAD_WANT)
            except OSError as exc:
                raise GateReadRefused(f"live handoff unreadable: {rel}: {exc}") from exc
            rec = _Rec(path, rel, True)
            self.recs.append(rec)
            self.heads[rel] = head
            self.id_live.setdefault(path.stem, rec)
            for ident in pg._as_list(head.get("stub_id")) + pg._as_list(head.get("handoff_id")) + pg._as_list(head.get("deliverable_id")):
                self.id_live.setdefault(ident, rec)
            src = _s(head.get("replan_of"))
            if src:
                self.replan_sources.add(src.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".md"))

    def _scan_archive_names(self) -> None:
        target = self.base + "archive/handoffs"
        if not os.path.isdir(target):
            return
        for dp, fn in _list_md(target, True):
            self.arch_stems.setdefault(fn[:-3], Path(dp) / fn)

    def ids_of(self, rec: _Rec) -> List[str]:
        head = self.heads[rec.rel]
        out = [rec.path.stem]
        for ident in pg._as_list(head.get("stub_id")) + pg._as_list(head.get("handoff_id")) + pg._as_list(head.get("deliverable_id")):
            if ident not in out:
                out.append(ident)
        return out

    def fm(self, rec: _Rec) -> Dict[str, Any]:
        cached = self.fms.get(rec.rel)
        if cached is None:
            cached = _baton_fields(rec.path)
            self.fms[rec.rel] = cached
        return cached

    def tracked(self):
        """The index snapshot, or None when it is absent or unreadable (withholds nothing)."""
        if self._tracked is False:
            try:
                snap = read_index_paths(self.root)
            except (IndexParseError, OSError, ValueError):
                snap = None
            self._tracked = snap if snap else None
        return self._tracked

    def plans(self) -> _PlanIndexFast:
        if self._plans is None:
            idx = _PlanIndexFast()
            idx.worktree_root = self.root
            _fill_plans(idx, self.root, "docs/plans")
            self._plans = idx
        return self._plans

    def arch_sizings(self) -> Dict[str, str]:
        if self._arch_sizings is None:
            target = self.base + "archive/sizings"
            idx: Dict[str, str] = {}
            if os.path.isdir(target):
                for _dp, _dn, fns in os.walk(target):
                    for fn in fns:
                        idx.setdefault(fn, "")
            self._arch_sizings = idx
        return self._arch_sizings


# ---------------------------------------------------------------------------
# One question each
# ---------------------------------------------------------------------------


def _resolve_blocker(g: _Gate, ident: str) -> Optional[_Rec]:
    """The record ``ident`` names: live ids, then archive file stems, then (on that miss only)
    the archived-header id index. None when nothing answers or the record has no frontmatter."""
    if ident in g.resolved:
        return g.resolved[ident]
    rec = g.id_live.get(ident)
    if rec is None:
        path = g.arch_stems.get(ident)
        if path is None:
            if g.arch_ids is None:
                g.arch_ids = {}
                for stem, p in g.arch_stems.items():
                    try:
                        fm = _head_fields(p, _HEAD_BYTES, _ID_WANT)
                    except OSError:
                        continue
                    for other in pg._as_list(fm.get("stub_id")) + pg._as_list(fm.get("handoff_id")) + pg._as_list(fm.get("deliverable_id")):
                        g.arch_ids.setdefault(other, p)
            path = g.arch_ids.get(ident)
        if path is not None:
            rec = _Rec(path, g._rel(path), False)
    if rec is not None and not g.fm(rec):
        rec = None
    g.resolved[ident] = rec
    return rec


def _link(g: _Gate, rec: _Rec) -> Tuple[Optional[dict], Optional[dict]]:
    """``(plan, ambiguity)`` for one baton by the fixed basis order; plan is
    ``{path,status,link_basis}`` or None."""
    cached = g.links.get(rec.rel)
    if cached is not None:
        return cached
    fm = g.fm(rec)
    hits: List[dict] = []
    basis: Optional[str] = None
    gp = _s(fm.get("governing_plan")).replace("\\", "/")
    if gp and not pg._as_list(fm.get("deliverable_id")) and not pg._as_list(fm.get("deliverable_ids")):
        gp_path = g.root / gp
        if gp_path.is_file():
            try:
                pfm = _head_fields(gp_path, _PLAN_HEAD_BYTES, _PLAN_WANT)
            except OSError:
                pfm = {}
            if pfm and pg.is_plan_record(pfm):
                mini = pg.PlanIndex()
                mini.add(gp, pfm)
                hits, basis = pg._link_plans_in(fm, mini)
    if not hits:
        hits, basis = pg.link_plans(fm, g.plans())
    best = pg._best_plan(hits, basis)
    plan = {"path": best["path"], "status": best["status"], "link_basis": basis} if best else None
    result = (plan, pg._plan_link_ambiguity(hits, basis))
    g.links[rec.rel] = result
    return result


def _unlinked_claim(g: _Gate, gp: str) -> dict:
    """``{path, cause, status}`` for a ``governing_plan`` claim no link basis resolved: the one
    step that refused it. ``plan_absent`` (no file), ``not_a_plan_record`` (a file that is not a
    plan), or ``no_back_link`` (an indexed plan the baton's ``deliverable_id`` contradicts or
    that another basis outranked); ``status`` is the plan's own, else None."""
    path = gp.replace("\\", "/")
    indexed = g.plans().get(path)
    if indexed is not None:
        return {"path": path, "cause": "no_back_link", "status": indexed["status"]}
    cause = "not_a_plan_record" if (g.root / path).is_file() else "plan_absent"
    return {"path": path, "cause": cause, "status": None}


def _disposition(g: _Gate, rec: Optional[_Rec]) -> str:
    """Requirement 4: unresolved | unplanned | plan-drafted | plan-approved | coded."""
    if rec is None:
        return "unresolved"
    cached = g.disps.get(rec.rel)
    if cached is not None:
        return cached
    fm = g.fm(rec)
    if _s(fm.get("deployment_state")) in pg.BATON_CODED_STATES:
        result = "coded"
    else:
        plan, _amb = _link(g, rec)
        if plan is None:
            result = "unplanned"
        elif plan["status"] in pg.PLAN_CODED_STATUSES:
            result = "coded"
        elif plan["status"] in pg.PLAN_APPROVED_STATUSES:
            result = "plan-approved"
        else:
            result = "plan-drafted"
    g.disps[rec.rel] = result
    return result


def _close_over(g: _Gate, blockers: List[str]) -> List[Tuple[str, Optional[_Rec]]]:
    """The transitive blocker set of ``blockers`` as ``(id, record-or-None)``, stopping at coded
    records. A None record is an unresolved id."""
    out: List[Tuple[str, Optional[_Rec]]] = []
    seen_ids: Set[str] = set()
    seen_recs: Set[str] = set()
    stack = list(reversed(blockers))
    while stack:
        ident = stack.pop()
        if ident in seen_ids:
            continue
        seen_ids.add(ident)
        rec = _resolve_blocker(g, ident)
        out.append((ident, rec))
        if rec is None or rec.rel in seen_recs:
            continue
        seen_recs.add(rec.rel)
        if _disposition(g, rec) != "coded":
            stack.extend(reversed(pg._as_list(g.fm(rec).get("blocked_by"))))
    return out


def _gates(g: _Gate, rec: _Rec) -> Tuple[List[dict], List[dict], List[str]]:
    """``(planning_blocking, execution_blocking, unresolved_ids)`` for one baton (requirement 5).
    An unresolved id appears in both lists."""
    planning: List[dict] = []
    execution: List[dict] = []
    unresolved: List[str] = []
    for ident, blocker in sorted(_close_over(g, pg._as_list(g.fm(rec).get("blocked_by"))), key=lambda p: p[0]):
        disp = _disposition(g, blocker)
        if blocker is None:
            unresolved.append(ident)
        if disp not in ("plan-approved", "coded"):
            planning.append({"blocker": ident, "disposition": disp})
        if disp != "coded":
            execution.append({"blocker": ident, "disposition": disp})
    return planning, execution, unresolved


def _classify(g: _Gate, rec: _Rec) -> str:
    """``candidate | held | untracked | withdrawn | not`` for one live record (requirement 2)."""
    cached = g.states.get(rec.rel)
    if cached is not None:
        return cached
    head = g.heads[rec.rel]
    if (
        (_s(head.get("baton_role")) or "work") not in pg._CANDIDATE_ROLES
        or _s(head.get("deployment_state")) not in pg._CANDIDATE_STATES
        or _s(head.get("status")) in _TERMINAL_STATUS
        or _s(head.get("superseded_by"))
    ):
        state = "not"
    else:
        fm = g.fm(rec)
        if not fm:
            raise GateReadRefused(f"candidate unparseable: {rec.rel}")
        tracked = g.tracked()
        if tracked is not None and rec.rel not in tracked:
            state = "untracked"
        elif _is_withdrawn(g, rec, fm):
            state = "withdrawn"
        elif _s(fm.get("plan_blitz_hold_reason")) and not _s(fm.get("plan_blitz_hold_cleared_by")):
            state = "held"
        else:
            state = "candidate"
    g.states[rec.rel] = state
    return state


def _is_withdrawn(g: _Gate, rec: _Rec, fm: Dict[str, Any]) -> bool:
    """Replanned (a live record replaces it) or resurrected (its archived namesake is terminal)."""
    if any(i in g.replan_sources for i in pg._baton_ids(fm, rec.path)):
        return True
    archived = g.arch_stems.get(rec.path.stem)
    if archived is None:
        return False
    try:
        ah = _head_fields(archived, _HEAD_BYTES, _HEAD_WANT)
    except OSError:
        return False
    return _s(ah.get("deployment_state")) in pg.BATON_CODED_STATES or _s(ah.get("status")) in _TERMINAL_STATUS


def _selected(g: _Gate, rec: _Rec) -> bool:
    if g.roadmap_id and _s(g.heads[rec.rel].get("roadmap_id")) != g.roadmap_id:
        return False
    if g.targets and not _target_hits(g, rec):
        return False
    return True


def _norm_target(t: str) -> str:
    t = t.strip().replace("\\", "/")
    return t.rsplit("/", 1)[-1].removesuffix(".md")


def _target_hits(g: _Gate, rec: _Rec) -> Set[str]:
    ids = set(g.ids_of(rec))
    return {t for t in g.targets if t == rec.rel or t in ids or _norm_target(t) in ids}


def _candidates(g: _Gate):
    """``(records_by_state, matched_targets)`` over the selector-narrowed live records."""
    by_state: Dict[str, List[_Rec]] = {"candidate": [], "held": [], "untracked": []}
    matched: Set[str] = set()
    for rec in g.recs:
        if not _selected(g, rec):
            continue
        state = _classify(g, rec)
        if state in by_state:
            by_state[state].append(rec)
            if g.targets:
                matched |= _target_hits(g, rec)
    return by_state, matched


def _info(g: _Gate, rec: _Rec) -> _Info:
    """The report row for one record plus its gate internals (requirements 5, 7, 8)."""
    cached = g.infos.get(rec.rel)
    if cached is not None:
        return cached
    fm = g.fm(rec)
    state = _classify(g, rec) if rec.live else "not"
    plan, ambiguity = _link(g, rec)
    planning, execution, unresolved = _gates(g, rec)
    cites = pg._as_list(fm.get("sizing_object")) + pg._as_list(fm.get("sizing_objects"))
    archive_index: Dict[str, str] = {}
    if any(not (g.root / c).is_file() for c in cites):
        archive_index = g.arch_sizings()
    resolved, _missing = pg._resolve_sizing_citations(g.root, cites, archive_index)
    exec_auth = bool(_s(fm.get("execution_authorized_by"))) or _s(fm.get("handoff_phase")) == "execution"
    waiting = False
    if state == "candidate" and execution and resolved:
        waiting = pg._sizing_route(g.root, resolved) in _NO_PLAN_ROUTES
    candidate = state == "candidate" and not waiting
    plan_approved = plan is not None and plan["status"] in pg.PLAN_APPROVED_STATUSES
    gp = _s(fm.get("governing_plan"))
    row = {
        "id": pg._baton_ids(fm, rec.path)[0],
        "path": rec.rel,
        "title": fm.get("title"),
        "stub_id": fm.get("stub_id") or None,
        "status": _s(fm.get("status")),
        "deployment_state": _s(fm.get("deployment_state")),
        "candidate": candidate,
        "plan": plan,
        "needs_plan": candidate and not plan_approved and not exec_auth,
        "execution_authorized": exec_auth,
        "sized": bool(resolved),
        "sizing_objects": resolved,
        "planning_gate": {"open": not planning, "blocking": planning},
        "execution_gate": {"open": not execution, "blocking": execution},
        "unlinked_plan_claim": _unlinked_claim(g, gp) if gp and plan is None else None,
        "ambiguous_plan_link": ambiguity,
        "held": state == "held",
        "waiting_on_execution": waiting,
    }
    info = _Info(rec, row, planning, execution, unresolved)
    g.infos[rec.rel] = info
    return info


def _waves(g: _Gate, reported: List[_Info]):
    """Requirement 6 over the whole candidate graph. Returns ``(waves, cycles, unschedulable)``
    where waves/cycles name only ``reported`` batons but wave depth counts every needs-plan
    candidate a reported baton waits on, narrowed or not."""
    universe: Dict[str, _Info] = {}
    queue = [i for i in reported if i.row["needs_plan"]]
    while queue:
        info = queue.pop()
        if info.rec.rel in universe:
            continue
        universe[info.rec.rel] = info
        for ent in info.planning_blocking:
            dep = _candidate_dep(g, ent)
            if dep is not None:
                queue.append(dep)
    edges: Dict[str, Set[str]] = {}
    dead: Set[str] = set()
    for rel, info in universe.items():
        deps: Set[str] = set()
        for ent in info.planning_blocking:
            dep = _candidate_dep(g, ent)
            if dep is None:
                dead.add(rel)
            else:
                deps.add(dep.rec.rel)
        edges[rel] = deps
    placed: Dict[str, int] = {}
    depth = 0
    while True:
        wave = [r for r in universe if r not in placed and r not in dead
                and all(d in placed and placed[d] < depth for d in edges[r])]
        if not wave:
            break
        for r in wave:
            placed[r] = depth
        depth += 1
    remaining = {r for r in universe if r not in placed}
    cyclic = {r for r in remaining if _reaches_self(r, edges, remaining)}
    reported_rels = {i.rec.rel for i in reported}
    waves = [
        sorted(universe[r].row["id"] for r in placed if placed[r] == d and r in reported_rels)
        for d in range(depth)
    ]
    while waves and not waves[-1]:
        waves.pop()
    cycles = sorted(universe[r].row["id"] for r in cyclic if r in reported_rels)
    unschedulable = len([r for r in remaining if r in reported_rels])
    return waves, cycles, unschedulable


def _candidate_dep(g: _Gate, entry: dict) -> Optional[_Info]:
    """The needs-plan candidate a blocking entry names, or None (unresolved, archived or not a
    candidate: the dependent can then never be scheduled)."""
    if entry["disposition"] == "unresolved":
        return None
    rec = _resolve_blocker(g, entry["blocker"])
    if rec is None or not rec.live or _classify(g, rec) != "candidate":
        return None
    info = _info(g, rec)
    return info if info.row["needs_plan"] else None


def _reaches_self(start: str, edges: Dict[str, Set[str]], within: Set[str]) -> bool:
    seen: Set[str] = set()
    stack = [d for d in edges[start] if d in within]
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(d for d in edges[node] if d in within)
    return False


def _emit(g, infos, waves, cycles, unschedulable, untracked, held, unmatched) -> dict:
    """The requirement-9 report and nothing else."""
    rows = [i.row for i in infos]
    candidates = [r for r in rows if r["candidate"]]
    unresolved = sorted({u for i in infos for u in i.unresolved})
    verdict = None
    if g.subject and rows:
        verdict = {"planning_gate": rows[0]["planning_gate"], "execution_gate": rows[0]["execution_gate"]}
    return {
        "batons": rows,
        "waves": waves,
        "cycles": cycles,
        "unresolved_blockers": unresolved,
        "untracked": untracked,
        "held": held,
        "counts": {
            "candidates": len(candidates),
            "planning_open": sum(1 for r in candidates if r["planning_gate"]["open"]),
            "execution_open": sum(1 for r in candidates if r["execution_gate"]["open"]),
            "unschedulable": unschedulable,
        },
        "unmatched_targets": unmatched,
        "verdict": verdict,
    }


def compute_gate_report(worktree_root, *, roadmap_id=None, targets=None, subject=None) -> dict:
    """Return the requirement-9 report for the baton corpus under ``worktree_root``.

    ``roadmap_id``, ``targets`` and ``subject`` narrow the report and the candidate set,
    never blocker reach. ``subject`` names one baton (any live or archived id, stem or path),
    reports it whether or not it is a candidate, and fills ``verdict``. Raises
    ``GateReadRefused`` when the read cannot answer.
    """
    g = _Gate(Path(worktree_root), roadmap_id, targets, subject)
    if g.subject:
        rec = _resolve_blocker(g, _norm_target(g.subject))
        if rec is None:
            raise GateReadRefused(f"subject names no record: {g.subject}")
        info = _info(g, rec)
        state = g.states.get(rec.rel)
        untracked = [rec.rel] if state == "untracked" else []
        held = [rec.rel] if state == "held" else []
        waves, cycles, unschedulable = _waves(g, [info])
        return _emit(g, [info], waves, cycles, unschedulable, untracked, held, [])
    by_state, matched = _candidates(g)
    infos = sorted((_info(g, r) for r in by_state["candidate"]), key=lambda i: i.row["path"])
    waves, cycles, unschedulable = _waves(g, infos)
    unmatched = sorted(t for t in g.targets if t not in matched)
    return _emit(
        g, infos, waves, cycles, unschedulable,
        sorted(r.rel for r in by_state["untracked"]),
        sorted(r.rel for r in by_state["held"]),
        unmatched,
    )
