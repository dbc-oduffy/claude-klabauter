"""Pure contract for the `--ask` gate: sizing load, arm selection, fire refusals, stage-1 plan path, resumable plan.

`ask_gate` and `ask_compose` build against these names. Nothing here
writes, spawns, or reroutes: a sizing whose route disagrees with its arm is refused by field.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

import yaml

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    LEDGER_HALT_KIND,
    RECORD_DIR_ROOT,
    SPINE_SUFFIX,
    UNCLAIMED_ROWS_HALT_KIND,
    LedgerHalt,
    ResumePlan,
    ledger_halt_path,
    read_ledger_halt,
    rulable_ids,
)
from coordinator_core.ops.dispatch_emit.run_credit import credits_for_sizing

COMMIT_HALT_KIND = "commit"
#: `run_id` of an all-landed halt when no halted run is known.
ALL_LANDED_NO_RUN = "none"
#: `resume_ledger` key naming the ids the halt itself blocked on, a subset of `blocking`: only these
#: owe work changed since the halt commit. Rows judged afterwards were not halt-blocking and stay met.
RESUME_LEDGER_HALT_BLOCKING_FIELD = "halt_blocking"
from coordinator_core.ops.sizing_acceptance import lacks_human_acceptance, sizing_acceptance_skipped
from coordinator_core.session import record_homes
from coordinator_core.sizing_route_table import _BASE_ROUTE_BY_TSHIRT

ARM_XS, ARM_S, ARM_M_PLUS, ARM_ROADMAP = "xs", "s", "m_plus", "roadmap"
ARM_ROUTE = {ARM_XS: "dispatch", ARM_S: "spec-dispatch", ARM_M_PLUS: "plan", ARM_ROADMAP: "roadmap"}
_PM_DECISION = "pm-decision"
_ROADMAP_EXIT = "roadmap"

_ARM_BY_ROUTE = {ARM_ROUTE[ARM_XS]: ARM_XS, ARM_ROUTE[ARM_S]: ARM_S}
# A size's arm follows its routed table entry; routes with no dispatch arm (plan, pm-decision, goal-setting) fire m_plus.
_TSHIRT_ARM = {t: _ARM_BY_ROUTE.get(r, ARM_M_PLUS) for t, r in _BASE_ROUTE_BY_TSHIRT.items()}
_FIREABLE_STATUS = ("sized", "routed")
# Literal `_scaffold_sizing` in coordinator-doc-new writes for intent and premise.evidence.
_SCAFFOLD_PLACEHOLDER = "PLACEHOLDER"
STATEMENT_ABSENT_PREFIX = "`exit_criterion.statement` is absent"


class SizingFireRefused(ValueError):
    """A sizing that cannot fire; `fields` carries one message per failing input."""

    def __init__(self, fields: Sequence[str]):
        self.fields = list(fields)
        super().__init__("; ".join(self.fields))


class SizingHandBack(Exception):
    """A shape-routed sizing: a clean hand-back to the PM, not a refusal; `line` is the one printed line."""

    def __init__(self, line: str):
        self.line = line
        super().__init__(line)


def load_sizing(repo_root: Path, sizing_rel: str) -> dict:
    """Read a sizing YAML contained under `<repo_root>/state/sizings/`."""
    root = Path(repo_root)
    p = Path(sizing_rel)
    if not p.is_absolute():
        p = root / p
    p = contained_path(p, [Path(record_homes.home_dir(str(root), "sizings"))])
    if p is None:
        raise SizingFireRefused([f"sizing escapes state/sizings/: {sizing_rel!r}"])
    if not p.is_file():
        raise SizingFireRefused([f"sizing-object not found on disk: {sizing_rel}"])
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SizingFireRefused([f"sizing-object is not valid YAML: {exc}"]) from exc
    if not isinstance(doc, dict):
        raise SizingFireRefused(["sizing-object is not a YAML mapping"])
    return doc


def inherited_interaction_mode(sizing: Mapping, repo_root: Path) -> str | None:
    """The `interaction_mode` a sibling of this sizing's fan-out records, else None.

    Reads the sizing's `em_analysis.ask_parent` marker, the fan-out's parent record (the run's
    tracked copy, else the runtime one), then each sibling sizing in submission order until one
    records a mode. Reads only; a missing or malformed marker, record or sibling yields None.
    """
    import json

    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as fc

    analysis = sizing.get("em_analysis")
    parsed = fc.parse_ask_parent(analysis.get("ask_parent") if isinstance(analysis, Mapping) else None)
    if parsed is None:
        return None
    parent_id, run_id = parsed
    root = Path(repo_root)
    record = None
    for rel in (fc.run_parent_copy_rel(run_id), fc.parent_record_rel(parent_id)):
        try:
            record = fc.ParentRecord.from_json(json.loads((root / rel).read_text(encoding="utf-8")))
            break
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if record is None:
        return None
    for run in record.runs:
        if run.run_id == run_id or not run.slice_sizing:
            continue
        try:
            mode = load_sizing(root, run.slice_sizing).get("interaction_mode")
        except SizingFireRefused:
            continue
        if isinstance(mode, str) and mode.strip():
            return mode.strip()
    return None


def load_sizing_effective(repo_root: Path, sizing_rel: str) -> dict:
    """`load_sizing`, with a missing `interaction_mode` filled in memory.

    Fill order: `inherited_interaction_mode`, then the fleet-resolved mode `sizing-assemble` uses
    when no `--interaction-mode` is passed. The one seam for a fire's readers: the file is never
    backfilled and a recorded mode is never overridden.
    """
    doc = load_sizing(repo_root, sizing_rel)
    if not doc.get("interaction_mode"):
        mode = inherited_interaction_mode(doc, Path(repo_root)) or _fleet_interaction_mode()
        if mode:
            doc = {**doc, "interaction_mode": mode}
    return doc


_fleet_mode_provider: Callable[[], str | None] | None = None


def set_fleet_mode_provider(provider: Callable[[], str | None] | None) -> None:
    """Install the callable that resolves the fleet `interaction_mode`; None removes it.

    The fleet record is read by the installing process (a sanctioned reader), never by this
    module: importing `mode_resolution` here would make the engine's whole import closure a reader.
    """
    global _fleet_mode_provider
    _fleet_mode_provider = provider


def _fleet_interaction_mode() -> str | None:
    """The installed provider's mode; None when no provider is installed or it cannot resolve."""
    if _fleet_mode_provider is None:
        return None
    try:
        mode = _fleet_mode_provider()
    except Exception:
        return None
    return mode.strip() if isinstance(mode, str) and mode.strip() else None


def effective_route(sizing: Mapping) -> object:
    """`route`, except a `pm-decision` whose recorded `xl_exit` resolves it: the plan-resolving
    exit (plan_gate's constant) to `plan`, `fan_out` to plan_gate's FAN_OUT_ROUTE, `roadmap` to
    `roadmap`. An unresolved one stays. A `goal-setting` route with `xl_exit: roadmap` also
    resolves to `roadmap`, as does one carrying a `goal_id` (the goal it landed as): the room
    route is over once the PM has recorded that exit or the goal is ratified."""
    route = sizing.get("route")
    if route == "goal-setting":
        goal_id = sizing.get("goal_id")
        if sizing.get("xl_exit") == _ROADMAP_EXIT or (isinstance(goal_id, str) and goal_id.strip()):
            return _ROADMAP_EXIT
    if route != _PM_DECISION:
        return route
    from coordinator_core.roadmap.plan_gate import (
        _XL_EXIT_FAN_OUT,
        _XL_EXIT_RESOLVING_TO_PLAN,
        FAN_OUT_ROUTE,
    )

    xl_exit = sizing.get("xl_exit")
    if xl_exit == _XL_EXIT_FAN_OUT:
        return FAN_OUT_ROUTE
    if xl_exit == _XL_EXIT_RESOLVING_TO_PLAN:
        return "plan"
    return _ROADMAP_EXIT if xl_exit == _ROADMAP_EXIT else route


def resolve_arm(sizing: Mapping) -> str:
    """The arm keyed by `estimate.tshirt`, ARM_ROADMAP for an XL+ roadmap route; an absent or
    unknown size is a refusal."""
    est = sizing.get("estimate")
    tshirt = est.get("tshirt") if isinstance(est, Mapping) else None
    arm = _TSHIRT_ARM.get(tshirt)
    if arm is None:
        raise SizingFireRefused(
            [f"`estimate.tshirt` is {tshirt!r} — expected one of {sorted(_TSHIRT_ARM)}"]
        )
    # L's table route is `plan`; a recorded `spec-dispatch` is the one route that moves it to arm S.
    if tshirt == "L" and sizing.get("route") == ARM_ROUTE[ARM_S]:
        arm = ARM_S
    if arm == ARM_M_PLUS and tshirt in ("XL", "XXL") and effective_route(sizing) == _ROADMAP_EXIT:
        return ARM_ROADMAP
    return arm


def apply_in_run_resize(
    sizing: Mapping, sizing_rel: str, reported_tshirt: str, repo_root: Path, basis: str
) -> tuple[str, dict, tuple[str, ...]]:
    """Apply a planner- or executor-reported t-shirt that differs from the sizing's.

    Returns `(arm, amended sizing, detents)`. `in_run_widen.resize_verdict` decides: continue
    amends the sizing through `sizing.resize`, re-resolves the arm with `resolve_arm`, and
    returns the detents `sizing_assemble.route` raises for the new size (a resize basis is not
    scout evidence, so `xxl_unprobed` fires on XXL as for any XXL sizing); flag raises
    SizingFireRefused naming a possible engine error or misplaced premise. An unchanged size
    returns the sizing as is. No budget value is touched.
    No production caller yet: the planner/executor t-shirt report is not wired into `resolve_arm`.
    """
    from coordinator_core.ops.dispatch_emit.in_run_widen import resize_verdict

    est = sizing.get("estimate")
    old = est.get("tshirt") if isinstance(est, Mapping) else None
    new = str(reported_tshirt).strip().upper()
    if old == new:
        return resolve_arm(sizing), dict(sizing), ()
    if resize_verdict(str(old), new) == "flag":
        raise SizingFireRefused(
            [
                f"reported size {new} against sizing {old}: a jump this large is a possible engine "
                "error or misplaced premise; flag to the EM, resize with `sizing.resize` once confirmed"
            ]
        )
    from coordinator_core.ops.sizing_resize import _handler as _resize_op
    from coordinator_core.sizing_assemble import route as _assemble_route

    res = _resize_op({"sizing": sizing_rel, "tshirt": new, "basis": basis}, repo_root=Path(repo_root))
    if res.get("exit_code") != 0:
        raise SizingFireRefused([f"in-run resize to {new} failed: {res.get('error')}"])
    amended = load_sizing(Path(repo_root), sizing_rel)
    detents = tuple(_assemble_route(estimate={"tshirt": new}, scout_evidence=[])["detents"])
    return resolve_arm(amended), amended, detents


def s_plan_path(sizing_rel: str) -> str:
    """Stage-1 plan location, `docs/plans/<sizing-stem>.md`; a repeated leading `YYYY-MM-DD-` collapses to one."""
    stem = PurePosixPath(str(sizing_rel).replace(chr(92), "/")).stem
    stem = re.sub(r"^((?:\d{4}-\d{2}-\d{2}-))(?:\1)+", r"\1", stem)
    return f"docs/plans/{stem}.md"


def resumable_plan(
    sizing: Mapping, sizing_rel: str, repo_root: Path, arm: str | None = None
) -> ResumePlan | None:
    """The existing plan for this sizing and its rows still to run, else None.

    The plan is the sizing's `plan:` edge, else at arm S `s_plan_path`. Incomplete rows are those
    neither closed by disposition (`coded`, `delivered_elsewhere`, ...) nor landed by a checkpoint
    commit of this plan or a `ledger-check` halt commit whose `Halt-Plan:` names it; row reports
    on disk are stale history and do not refuse. A plan with rows, every one landed, resumes at its ledger: a ledger-halt pointer (`ledger-check` or `commit`
    halt) naming this plan and sizing with no row coded is carried in `ResumePlan.ledger`; with nothing to rule, an
    `ALL_LANDED_HALT_KIND` halt with empty `blocking` routes the run to verify the exit criterion.
    Failing that, a rulable register row or a not_met recorded outcome resumes it as a synthesised
    `ALL_LANDED_HALT_KIND` ledger resume (`_all_landed_halt`); a spine with no `coded` row also pays the
    one `resume_landed_state` git spawn. A plan with
    no readable spine or no rows is not resumable (None). An em-performed row (declared `performer: em`,
    or derived by `em_only_act`) that the plan's evidence sidecar carries under `em_discharged` counts
    landed, as in `read_spine` (`em_discharge_eligible`). A row the EM credited through
    `workflow.revise credit_chunks` on any run of this sizing (`run_credit.credits_for_sizing`) also counts
    landed: one date-floored scandir plus a `credit.jsonl` read per candidate dir, only while a row is
    still open. One file read, plus one more (the evidence sidecar) only when an open em-performed row exists; one `git log` only when a
    row is open. With rows left open, a ledger resume
    also needs the newest ledger-check halt's committed record to have staged every one of them
    (`_ledger_halt_for`), pointer or no pointer; a row it did not stage keeps `ResumePlan.rows`,
    except an inbound-memo-close row, which the halted manifest never stages and so is exempt.
    `ResumePlan.not_run_reasons` names the evidence for each spine row the gate does not stage, from
    sets already computed here (no new read or spawn).
    """
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.dispatch_emit.spine_read import (
        NON_DISPATCHABLE_DISPOSITIONS,
        load_rows_memo,
        with_canonical_disposition,
    )

    edge = sizing.get("plan")
    root = Path(repo_root)
    plan_rel = edge if isinstance(edge, str) and edge.strip() else (s_plan_path(sizing_rel) if arm == ARM_S else None)
    if plan_rel is None and arm == ARM_XS:
        plan_rel = _xs_halt_spine(root, sizing_rel)
    if plan_rel is None:
        return None
    plan = contained_path(root / plan_rel, [root / "docs" / "plans", root / RECORD_DIR_ROOT])
    if plan is None or not plan.is_file():
        return None
    plan_text = plan.read_text(encoding="utf-8")
    result = load_rows_memo(plan_text)
    if result.status is not LocateStatus.LOCATED:
        return None
    rows = [with_canonical_disposition(r) for r in result.rows]
    ids = [r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)]
    if not ids:
        return None
    reasons: dict[str, str] = {}
    open_ids = []
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            if r.get("disposition") not in NON_DISPATCHABLE_DISPOSITIONS:
                open_ids.append(r["id"])
            else:
                reasons[r["id"]] = f"disposition {r.get('disposition')}"
    if open_ids:
        kept_open = _drop_em_discharged(plan, plan_text, rows, open_ids)
        reasons.update({i: "em-discharged" for i in open_ids if i not in kept_open})
        open_ids = kept_open
    open_pre = list(open_ids)
    credited: Mapping = {}
    if open_ids:
        credited = credits_for_sizing(root, sizing_rel)
        reasons.update({i: "credited by workflow.revise" for i in open_ids if i in credited})
        open_ids = [i for i in open_ids if i not in credited]
    state = None
    landed_open: tuple = ()
    pending = list(open_ids)
    record: object = _UNREAD
    named: list[str] = []
    if open_ids or not _terminal_coded(rows) or _sha_coded(rows):
        from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

        state = resume_landed_state(root, plan_rel)
        record, named = _blocking_named_rows(root, plan_rel, sizing_rel, plan_text, rows, open_pre, state)
        landed_open = tuple((i, state.row_shas[i]) for i in open_ids if i in state.rows and state.row_shas.get(i))
        reasons.update(
            {i: f"landed at {state.row_shas.get(i) or 'checkpoint'}" for i in open_ids if i in state.rows}
        )
        open_ids = [i for i in open_ids if i not in state.rows]
        pending = list(open_ids)
        if open_ids:
            from coordinator_core.ops.dispatch_emit.plan_row_coding import rows_landed_outside_executor

            outside = rows_landed_outside_executor(
                root, plan_rel, [r for r in rows if r.get("id") in open_ids and r.get("id") not in named]
            )
            landed_open += tuple((i, outside[i]) for i in open_ids if i in outside)
            reasons.update({i: f"landed outside the executor at {outside[i]}" for i in open_ids if i in outside})
            open_ids = [i for i in open_ids if i not in outside]
    ledger_reasons = tuple(sorted((i, r) for i, r in reasons.items() if i not in pending))
    halt = _ledger_halt_for(root, plan_rel, sizing_rel, rows, open_ids, state, record)
    if halt is not None:
        held = _held_rows(named, pending, credited, state)
        return ResumePlan(
            plan_rel, (), ledger=replace(halt, unlanded=tuple(pending)), held=held, not_run_reasons=ledger_reasons
        )
    if not open_ids:
        halt = _all_landed_halt(root, plan_rel, sizing, sizing_rel, state)
        if halt is not None:
            held = _held_rows(named, pending, credited, state)
            return ResumePlan(
                plan_rel, (), ledger=replace(halt, unlanded=tuple(pending)), held=held,
                not_run_reasons=ledger_reasons,
            )
        pointer = _own_pointer(root, plan_rel, sizing_rel)
        run = pointer.run_id if pointer is not None else (state.ledger_run if state is not None else None)
        verify = LedgerHalt(run or ALL_LANDED_NO_RUN, plan_rel, sizing_rel, (), halted=ALL_LANDED_HALT_KIND)
        return ResumePlan(plan_rel, (), ledger=verify, held=_held_rows(named, pending, credited, state), not_run_reasons=ledger_reasons)
    unclaimed_halt = _unclaimed_rows_halt(root, plan_rel, plan_text, sizing, sizing_rel, pending)
    if unclaimed_halt is not None:
        return ResumePlan(
            plan_rel, (), ledger=unclaimed_halt, held=_held_rows(named, pending, credited, state),
            not_run_reasons=ledger_reasons,
        )
    kept, kept_run = _kept_rows(root, sizing_rel, open_ids)
    return ResumePlan(
        plan_rel, tuple(open_ids), landed_open=landed_open, kept=kept, kept_run=kept_run,
        held=_held_rows(named, open_ids, credited, state),
        not_run_reasons=tuple(sorted(reasons.items())),
    )


#: `_ledger_halt_for`'s `record` when the caller has not read the halt record.
_UNREAD = object()


def _blocking_named_rows(
    root: Path, plan_rel: str, sizing_rel: str, plan_text: str, rows: Sequence, open_ids: Sequence[str], state
) -> tuple[object, list[str]]:
    """`(record, named)`: the newest ledger-check halt record of this plan and sizing (read once; `_UNREAD`
    when none is read) and the open non-EM rows, in `open_ids` order, that a blocking item's `remaining`
    lines name by id and that serve that item."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_halt_record
    from coordinator_core.ops.dispatch_emit.em_only_act import row_is_em_performed
    from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import row_ids_named, rows_serving
    from coordinator_core.roadmap.prep_gate import plan_frontmatter

    if state.ledger_run is None or not open_ids:
        return _UNREAD, []
    record = read_halt_record(root, state.ledger_run, at_sha=state.ledger_sha)
    if (
        record is None
        or record.sizing_rel != sizing_rel
        or (record.plan_rel is not None and record.plan_rel != plan_rel)
    ):
        return record, []
    by_id = {r["id"]: r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)}
    candidates = [i for i in open_ids if i in by_id and not row_is_em_performed(by_id[i])]
    raw = (plan_frontmatter(plan_text).get("register_claims") or {}).get("rows")
    claims = [str(c) for c in raw] if isinstance(raw, list) else []
    named: set[str] = set()
    for item, entry in record.remaining.items():
        if record.blocking is not None and item not in record.blocking:
            continue
        hit = row_ids_named([str(s) for s in entry.get("remaining") or []], candidates)
        serving = {r["id"] for r in rows_serving([by_id[i] for i in hit], claims, [item])}
        named |= serving
    return record, [i for i in candidates if i in named]


def _held_rows(named: Sequence[str], handed: Sequence[str], credited: Mapping, state) -> tuple[tuple[str, str], ...]:
    """`(row id, reason)` for each named row the gate did not hand the stage: `credited` or `landed:<sha>`."""
    out: list[tuple[str, str]] = []
    for i in named:
        if i in handed:
            continue
        if i in credited:
            out.append((i, "credited"))
        elif state is not None and i in state.rows and state.row_shas.get(i):
            out.append((i, f"landed:{state.row_shas[i]}"))
    return tuple(out)


def _unclaimed_rows_halt(
    root: Path, plan_rel: str, plan_text: str, sizing: Mapping, sizing_rel: str, pending: Sequence[str]
) -> LedgerHalt | None:
    """The synthesised `UNCLAIMED_ROWS_HALT_KIND` halt for a plan with rows still open whose
    `register_claims` omit register rows (not ruled out), else None. It routes the refire through the
    ledger re-plan leg, which adds rows serving them. Reads nothing: `plan_text` and `sizing` are in
    hand. The halt is never persisted."""
    from coordinator_core.ops import requirement_register as rr
    from coordinator_core.ops.dispatch_emit.halted_run import halted_ask_run_for
    from coordinator_core.ops.dispatch_emit.requirements_ledger import ruled_out_ids
    from coordinator_core.roadmap.prep_gate import plan_frontmatter

    register = sizing.get("requirement_register")
    rows = register.get("rows") if isinstance(register, Mapping) else None
    if not isinstance(rows, list):
        return None
    claims = plan_frontmatter(plan_text).get("register_claims")
    if not isinstance(claims, Mapping) or claims.get("sizing") != sizing_rel or not isinstance(claims.get("rows"), list):
        return None
    dict_rows = [r for r in rows if isinstance(r, dict)]
    out = ruled_out_ids(dict_rows)
    if not rr.unclaimed_ids(dict_rows, claims["rows"], exclude=out):
        return None
    rulable = [r for r in rulable_ids(rows) if r not in out]
    run = halted_ask_run_for(root, sizing_rel)
    return LedgerHalt(
        run or ALL_LANDED_NO_RUN, plan_rel, sizing_rel, tuple(rulable),
        halted=UNCLAIMED_ROWS_HALT_KIND, unlanded=tuple(pending),
    )


def _kept_rows(root: Path, sizing_rel: str, open_ids: Sequence[str]) -> tuple[tuple[str, ...], str | None]:
    """The still-open ids the newest halted ask run of this sizing kept, and that run's id.

    A row is kept while `kept_work.still_kept` holds against the run's live notes. Reads the run
    record dir only; no git spawn.
    """
    from coordinator_core.ops.dispatch_emit import kept_work, run_notes
    from coordinator_core.ops.dispatch_emit.halted_run import halted_ask_run_for
    from coordinator_core.ops.dispatch_emit.requirements_ledger import NOTES_FILE, LedgerRefused, run_dir

    run_id = halted_ask_run_for(root, sizing_rel)
    if run_id is None:
        return (), None
    try:
        record = run_dir(root, run_id)
    except LedgerRefused:
        return (), None
    chunks = kept_work.read(record)["chunks"]
    candidates = [i for i in open_ids if i in chunks]
    if not candidates:
        return (), None
    notes = [e["note"] for e in run_notes.live(run_notes.entries(record / NOTES_FILE, run_id))]
    kept = tuple(i for i in candidates if kept_work.still_kept(root, chunks[i], notes, i, record_dir=record))
    return (kept, run_id) if kept else ((), None)


def _halt_carries_executed_rows(run_dir: Path, halted: Mapping) -> bool:
    """True for a `ledger-check` marker, or a still-`halted` non-`refusal` marker whose run staged
    rows (its `manifest.json` lists some): such a run executed rows its refire must resume, where a
    refusal or plan-gate halt staged none."""
    if halted.get("halted") == "ledger-check":
        return True
    if halted.get("halted") == "refusal" or halted.get("status") != "halted":
        return False
    try:
        rows = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")).get("rows")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(rows, list) and bool(rows)


def _xs_halt_spine(root: Path, sizing_rel: str) -> str | None:
    """The record spine named by the newest halt marker of a run bound to `sizing_rel` that is a
    `ledger-check` halt or carries executed rows (`_halt_carries_executed_rows`), else None. Reads
    `halted.json` (and `manifest.json` for non-ledger-check markers) only from `ask-`/`warp-` run
    dirs stamped on or after the sizing's date; no git spawn. The spine must sit directly in a `RECORD_DIR_ROOT/<run>` dir."""
    import os

    from coordinator_core.ops.dispatch_emit.halted_run import _DATE_PREFIX, _STAMP

    sizing = PurePosixPath(str(sizing_rel).replace("\\", "/")).as_posix()
    stem = PurePosixPath(sizing).stem
    dated = _DATE_PREFIX.match(stem)
    if dated is None:
        return None
    floor = "".join(dated.groups())
    prefix = f"warp-{stem}-"
    base = root / RECORD_DIR_ROOT
    candidates: list[tuple[str, str]] = []
    try:
        with os.scandir(base) as it:
            for entry in it:
                name = entry.name
                if name.startswith("ask-"):
                    stamp = name[4:]
                    if not (_STAMP.fullmatch(stamp) and stamp[:8] >= floor):
                        continue
                elif name.startswith(prefix):
                    stamp = name[len(prefix):]
                    if not _STAMP.fullmatch(stamp):
                        continue
                else:
                    continue
                candidates.append((stamp, name))
    except OSError:
        return None
    for _, name in sorted(candidates, reverse=True):
        try:
            halted = json.loads((base / name / "halted.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(halted, dict) or not _halt_carries_executed_rows(base / name, halted):
            continue
        rel = halted.get("sizing_rel")
        if not isinstance(rel, str) or PurePosixPath(rel.replace("\\", "/")).as_posix() != sizing:
            continue
        plan = halted.get("plan")
        if not isinstance(plan, str):
            return None
        parts = PurePosixPath(plan.replace("\\", "/")).parts
        if (
            len(parts) == len(PurePosixPath(RECORD_DIR_ROOT).parts) + 2
            and "/".join(parts[:-2]) == RECORD_DIR_ROOT
            and parts[-2] == name
            and parts[-1].endswith(SPINE_SUFFIX)
        ):
            return "/".join(parts)
        return None
    return None


_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _coded_rows(rows: Sequence) -> list:
    return [r for r in rows if isinstance(r, dict) and r.get("disposition") == "coded"]


def _sha_coded(rows: Sequence) -> bool:
    """True when a `coded` row's `disposition_ref` is a full sha: it may be a gate stamp."""
    return any(_FULL_SHA.fullmatch(str(r.get("disposition_ref") or "")) for r in _coded_rows(rows))


def _terminal_coded(rows: Sequence, state=None) -> bool:
    """True when a row is `coded` other than by the gate's resume stamp: a `coded` row whose
    `disposition_ref` is in `state.landing_shas` for it (either half of a split checkpoint, or a
    ledger-check commit) was stamped by the gate, so the terminal commit has not run. With no
    `state`, any `coded` row counts.
    Trap: the terminal commit also stamps checkpoint-landed rows with their checkpoint sha, so a run
    whose every row landed by checkpoint reads here as not yet terminal-committed."""
    shas = state.landing_shas if state is not None else {}
    return any(
        not (r.get("disposition_ref") and r.get("disposition_ref") in shas.get(r.get("id"), ()))
        for r in _coded_rows(rows)
    )


def _drop_em_discharged(plan: Path, plan_text: str, rows: Sequence, open_ids: list) -> list:
    """`open_ids` less every row `read_spine` counts satisfied by an em-discharge entry. The evidence
    sidecar is read once, and only when an open row is eligible for discharge."""
    from coordinator_core.ops.dispatch_emit.spine_read import drop_em_discharged

    return drop_em_discharged(plan, plan_text, rows, open_ids)


def _all_landed_halt(
    root: Path, plan_rel: str, sizing: Mapping, sizing_rel: str, state=None
) -> LedgerHalt | None:
    """The synthesised `ALL_LANDED_HALT_KIND` halt for a plan with no open row, else None (nothing
    left to judge). It resumes when the sizing's register holds a rulable row, or the newest recorded
    outcome of this plan's halted run judged an item not_met. `state` is the `resume_landed_state`
    answer already spent; absent, it is read here once and only when a rulable row exists. The halt is
    never persisted."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_halt_record, read_run_outcome

    register = sizing.get("requirement_register")
    rows = register.get("rows") if isinstance(register, Mapping) else None
    rulable = rulable_ids(rows) if isinstance(rows, list) else ()
    pointer = _own_pointer(root, plan_rel, sizing_rel)
    run = pointer.run_id if pointer is not None else (state.ledger_run if state is not None else None)
    if not rulable and not _outcome_not_met(read_run_outcome(root, run) if run else None):
        return None
    if state is None and run is None:
        from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

        state = resume_landed_state(root, plan_rel)
        run = state.ledger_run
    at_sha = state.ledger_sha if state is not None and run == state.ledger_run else None
    record = read_halt_record(root, run, at_sha=at_sha) if run else None
    remaining = {k: v for k, v in record.remaining.items() if k in rulable} if record is not None else {}
    return LedgerHalt(
        run or ALL_LANDED_NO_RUN, plan_rel, sizing_rel, tuple(rulable), remaining=remaining, halted=ALL_LANDED_HALT_KIND
    )


def _outcome_not_met(outcome: Mapping | None) -> bool:
    """True when the recorded run outcome's `ledger_verdict` holds an item judged `not_met`. Termination relies on the next run rewriting its outcome; no test pins that."""
    verdict = outcome.get("ledger_verdict") if outcome else None
    items = verdict.get("items") if isinstance(verdict, Mapping) else None
    return isinstance(items, list) and any(isinstance(i, Mapping) and i.get("status") == "not_met" for i in items)


def _own_pointer(root: Path, plan_rel: str, sizing_rel: str) -> LedgerHalt | None:
    """The halt pointer naming this plan and sizing, from the repo root else the git common dir."""
    pointer = read_ledger_halt(ledger_halt_path(root, plan_rel))
    if pointer is None:
        # The halt writer is handed the git common dir as its root, so a pointer may sit under it.
        from coordinator_core.git.git_dir import resolve_git_common_dir

        pointer = read_ledger_halt(ledger_halt_path(resolve_git_common_dir(root), plan_rel))
    if pointer is not None and (pointer.plan_rel != plan_rel or pointer.sizing_rel != sizing_rel):
        return None
    return pointer


def _ledger_halt_for(
    root: Path, plan_rel: str, sizing_rel: str, rows: Sequence, open_ids: Sequence[str], state=None,
    record: object = _UNREAD,
) -> LedgerHalt | None:
    """The ledger halt a refire resumes at, else None. None when a row is `coded` (the terminal
    commit landed). A commit-halt pointer resumes only with no open id, so an open row takes the
    ordinary row resume. Otherwise the halt is the newest `ledger-check` halt commit naming the plan
    (`state.ledger_run`) whose committed record names this sizing (and this plan when it records
    one) and staged every id in `open_ids` bar ruled ids and inbound-memo-close rows; its blocking ids come from the pointer when the pointer
    names that run, else the record. With no open id the pointer alone also suffices. `state` is the
    `resume_landed_state` answer already spent; absent, it is read here once, so no path spends a
    second `git log`."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_halt_record

    if state is None and _sha_coded(rows):
        from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

        state = resume_landed_state(root, plan_rel)
    if _terminal_coded(rows, state):
        return None
    pointer = _own_pointer(root, plan_rel, sizing_rel)
    if pointer is not None and pointer.halted == COMMIT_HALT_KIND and open_ids:
        return None
    if state is None:
        from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

        state = resume_landed_state(root, plan_rel)
    if pointer is not None and not open_ids:
        halt_sha = state.run_halts.get(pointer.run_id, (frozenset(), None))[1]
        return _carry(root, plan_rel, replace(pointer, halt_sha=halt_sha), state)
    if state.ledger_run is None:
        # A halt commit without a `Halt-Plan:` trailer is invisible to `resume_landed_state`; the
        # pointer's run then resumes when that run's halt commit landed every open row.
        if pointer is not None:
            landed, sha = state.run_halts.get(pointer.run_id, (frozenset(), None))
            if set(open_ids) - set(pointer.ruled) - _memo_close_ids(rows, open_ids) <= landed:
                return _carry(root, plan_rel, _with_ruled_blocking(pointer, halt_sha=sha), state)
        return None
    if record is _UNREAD:
        record = read_halt_record(root, state.ledger_run, at_sha=state.ledger_sha)
    same_run = pointer is not None and pointer.run_id == state.ledger_run
    ruled = set(pointer.ruled) if same_run else set()
    if (
        record is None
        or record.sizing_rel != sizing_rel
        or (record.plan_rel is not None and record.plan_rel != plan_rel)
        or not set(open_ids) - ruled - _memo_close_ids(rows, open_ids) <= record.staged
    ):
        return None
    if same_run:
        blocking = _with_ruled_blocking(pointer).blocking
    else:
        blocking = record.blocking or ()
    halt = LedgerHalt(
        state.ledger_run, plan_rel, sizing_rel, tuple(blocking),
        halt_sha=state.ledger_sha, remaining=record.remaining, unlanded=tuple(open_ids),
    )
    if state.ledger_sha:
        halt = replace(halt, landed_since_halt=state.landed_since_ledger)
    return _carry(root, plan_rel, halt, state)


def _memo_close_ids(rows: Sequence, open_ids: Sequence[str]) -> set:
    """Open ids whose row is an inbound-memo-close row (`memo_rows.inbound_close_path`). The
    close-out memo-lifecycle leg owns them and a halted manifest never stages them, so the
    staged-subset test exempts them. Pure: no I/O."""
    from coordinator_core.ops.dispatch_emit.memo_rows import inbound_close_path

    want = set(open_ids)
    return {r["id"] for r in rows if isinstance(r, dict) and r.get("id") in want and inbound_close_path(r) is not None}


def _with_ruled_blocking(pointer: LedgerHalt, **changes) -> LedgerHalt:
    """`pointer` with its ruled ids appended to `blocking`, so the verify leg judges them."""
    extra = tuple(r for r in pointer.ruled if r not in pointer.blocking)
    return replace(pointer, blocking=(*pointer.blocking, *extra), **changes)


def since_halt_shas_of(state, halt: LedgerHalt) -> tuple[str, ...]:
    """Plan checkpoint shas newer than the halt's measuring base (`since_sha`, else `halt_sha`).
    The base is a halt commit, so it is positioned in `state.walk_ordinals`, never in
    `checkpoint_shas`. A base outside the walk gives `()`: empty means no credit."""
    base = halt.since_sha or halt.halt_sha
    ordinals = getattr(state, "walk_ordinals", None) or {}
    pos = ordinals.get(base) if base else None
    if pos is None:
        return ()
    return tuple(s for s in getattr(state, "checkpoint_shas", ()) if ordinals.get(s, pos) < pos)


def _carry(root: Path, plan_rel: str, halt: LedgerHalt, state) -> LedgerHalt:
    """`_carry_chain` with `since_halt_shas` derived once `since_sha` is final on every return."""
    out = _carry_chain(root, plan_rel, halt, state)
    return replace(out, since_halt_shas=since_halt_shas_of(state, out))


def _carry_chain(
    root: Path, plan_rel: str, halt: LedgerHalt, state
) -> LedgerHalt:
    """`halt` with ids an earlier halt of this plan and sizing judged met dropped from `blocking`
    (named in `carried_met`) and `remaining` merged across the halts walked. Reads one record per
    halt back to the first that landed a chunk; no git spawn. A commit-halt pointer, a halt that
    itself landed a chunk, or an unreadable earlier record leaves `halt` as it is. This function only
    loads records; `carry_forward` owns the stop-at-landing rule, so the early breaks here are a read bound.
    Every return sets `since_sha` to the first halt of the oldest run walked (`halt_sha` stays the
    newest), so the since-halt gate measures from the chain's start, not the halt a fire just wrote."""
    from coordinator_core.ops.dispatch_emit.ask_contract import carry_forward, read_halt_record
    from coordinator_core.ops.dispatch_emit.halt_baseline import first_halt_sha, walk_chain

    halt = replace(halt, since_sha=first_halt_sha(state, halt.run_id))
    if halt.halted != LEDGER_HALT_KIND:
        return halt
    newest = read_halt_record(root, halt.run_id, at_sha=halt.halt_sha)
    if newest is None:
        return halt
    head_lands = bool(state.run_halts.get(halt.run_id, (frozenset(), None))[0])
    earlier, oldest = walk_chain(root, plan_rel, halt.run_id, state, halt.sizing_rel)
    blocking, remaining, carried = carry_forward((replace(newest, blocking=halt.blocking), head_lands), earlier)
    return replace(
        halt, blocking=blocking, remaining={**halt.remaining, **remaining}, carried_met=carried,
        since_sha=first_halt_sha(state, oldest),
    )


def _acceptance_skipped(sizing: Mapping) -> bool:
    """The engine size rule, on the recorded route (never `effective_route`) and size."""
    est = sizing.get("estimate")
    return sizing_acceptance_skipped(sizing.get("route"), est.get("tshirt") if isinstance(est, Mapping) else None)


def collect_fire_refusals(
    sizing: Mapping,
    *,
    sizing_rel: str,
    arm: str,
    writes: Sequence[str],
    repo_root: Path | None = None,
    resume_sink: list | None = None,
) -> list[str]:
    """Every failing fire input, one message each, never stopping at the first; [] when fireable.

    `repo_root` enables the arm-s check that the stage-1 plan path is still free.
    `resume_sink`, when given, receives the arm-s `resumable_plan` answer (a `ResumePlan`, or
    None; nothing when it refused) so the caller does not repeat that read's `git log`.
    """
    out: list[str] = []
    ec = sizing.get("exit_criterion")
    ec = ec if isinstance(ec, Mapping) else {}
    if not ec.get("statement"):
        out.append(f"{STATEMENT_ABSENT_PREFIX} — nothing to hand off as the exit criterion")
    if lacks_human_acceptance(ec.get("accepted")) and not _acceptance_skipped(sizing):
        out.append(
            "`exit_criterion.accepted` is null — accept it first: "
            f"sizing-accept-exit-criterion --sizing {sizing_rel} --pm-quote \"<PM's words>\" "
            "(or --apm-ruling \"<ruling>\" --ruling-ref <path> in pm/ceo mode)"
        )
    if not sizing.get("interaction_mode"):
        out.append("`interaction_mode` is absent")
    intent = sizing.get("intent")
    if isinstance(intent, str) and intent.startswith(_SCAFFOLD_PLACEHOLDER):
        out.append("`intent` is still the scaffold placeholder — write the PM's ask, verbatim")
    premise = sizing.get("premise")
    premise = premise if isinstance(premise, Mapping) else {}
    evidence = premise.get("evidence")
    if isinstance(evidence, str) and evidence.startswith(_SCAFFOLD_PLACEHOLDER):
        out.append("`premise.evidence` is still the scaffold placeholder — cite what you actually looked at")
    if premise.get("provenance") == "unrecorded":
        out.append("`premise.provenance` is 'unrecorded' — record how the premise was verified")
    status = sizing.get("status")
    if status not in _FIREABLE_STATUS:
        out.append(f"`status` is {status!r}, not one of {list(_FIREABLE_STATUS)}")
    expected = ARM_ROUTE[arm]
    route = effective_route(sizing)
    if route != expected:
        out.append(f"`route` is {route!r}, but arm {arm!r} fires only route {expected!r}")
    if arm == ARM_XS and not list(writes):
        out.append(
            "XS needs `writes` (the ask's file footprint): a sizing carries no footprint; "
            "name it as args.writes beside args.sizing"
        )
    if arm == ARM_S and repo_root is not None:
        plan_rel = s_plan_path(sizing_rel)
        try:
            resumable = resumable_plan(sizing, sizing_rel, Path(repo_root), arm)
            if resumable is None and (Path(repo_root) / plan_rel).exists():
                resumable = ResumePlan(plan_rel, ())
            if resume_sink is not None:
                resume_sink.append(resumable)
        except SizingFireRefused as exc:
            out.extend(exc.fields)
    return out
