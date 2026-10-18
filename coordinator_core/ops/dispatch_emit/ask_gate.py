"""dispatch.ask_gate handler: one sizing in, an arm to run or a halt (room, touchpoint, refusal) out.

Reads the sizing; at M+ with no halt it mints the baton record in-process (a write). An unfired
plan on disk with rows still to run rides the verdict as `resume_plan` and `resume_rows`.

Every verdict carries the sizing's `design_route` payload when one is recorded. The gate never
halts on it: a canvas route with no recorded approval arms (plan authored first) and
`ask_stage.stage` refuses it with a `design-review:` reason, so the halt falls after plan and
before stage.
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.warp_fire import fire_command
from coordinator_core.ops.dispatch_emit import design_route
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    HALT_REFUSAL,
    HALT_RESEARCH,
    HALT_ROOM,
    HALT_SPLIT,
    HALT_TOUCHPOINT,
    RESEARCH_HANDBACK_ROUTE,
    RESEARCH_MIXED_REFUSAL,
    RESUME_LEDGER_CARRIED_MET_FIELD,
    RESUME_LEDGER_EM_DISCHARGED_FIELD,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_KIND_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
    RESUME_LEDGER_REMAINING_FIELD,
    RESUMED_RUN_ID_KEY,
    RUN_OUTCOME_FILE,
    GateVerdict,
    read_run_outcome,
    rulable_ids,
)
from coordinator_core.ops.sizing_acceptance import (
    ENGINE_SIZE_RULE,
    apm_picks_xl_exit,
    lacks_human_acceptance,
    sizing_acceptance_skipped,
)
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, manifest_payload, stage
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.dispatch_emit.sizing_fire import (
    ALL_LANDED_NO_RUN,
    ARM_M_PLUS,
    ARM_ROADMAP,
    STATEMENT_ABSENT_PREFIX,
    COMMIT_HALT_KIND,
    RESUME_LEDGER_HALT_BLOCKING_FIELD,
    SizingFireRefused,
    collect_fire_refusals,
    effective_route,
    load_sizing,
    resolve_arm,
    resumable_plan,
)

_ACCEPT_TOUCHPOINTS = frozenset({"accept_sizing", "accept_exit_criterion"})
_ACCEPTED_NULL_PREFIX = "`exit_criterion.accepted`"
_PARAMS = (
    Field("sizing_path", "nonempty_str", required=True),
    Field("writes", "str_list"),
    Field("baton", "nonempty_str"),
    Field("record", "dict"),
    Field("run_id", "nonempty_str"),
    Field("cross_repo_approved", "bool"),
    Field("session_id", "str"),
    Field("route_override_reason", "str"),
)
_RECORD_KINDS = ("exit_criterion", "xl_exit", design_route.RECORD_KIND)
_XL_PICKS = ("roadmap", "accept_multi_session", "split")
_DOC_NEW = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "coordinator-doc-new.py"


def handback_line(sizing: dict) -> str:
    """The one line a shape-routed sizing prints: topic from `name`, else a short slice of `intent`."""
    topic = str(sizing.get("name") or "").strip() or " ".join(str(sizing.get("intent") or "").split())[:60].strip()
    return (
        f"Job unclear: {topic}. Needs a PM conversation (coordinator:shape room); "
        "re-run --ask once the JTBD is stated."
    )


def _halt(kind: str, reason: str, **extra: object) -> GateVerdict:
    return GateVerdict(arm=None, halt={"kind": kind, "reason": reason, **extra})


@functools.cache
def _load_doc_new():
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_for_ask_gate", _DOC_NEW)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _design_scope(sizing: dict, writes: Sequence[str]) -> list[str]:
    """The paths the gate can see: the scouted writes plus each acceptance row's writes."""
    scope = [w for w in writes if isinstance(w, str) and w.strip()]
    for row in sizing.get("acceptance_rows") or []:
        if isinstance(row, dict):
            scope += [w for w in row.get("writes") or [] if isinstance(w, str) and w.strip()]
    return list(dict.fromkeys(scope))


def gate(
    repo_root: Path,
    sizing_rel: str,
    *,
    writes: Sequence[str] = (),
    baton: Optional[str] = None,
    mint_design_rows: bool = False,
) -> GateVerdict:
    """Arm for the sizing at `sizing_rel`, or the single halt that stops it, naming every cause.

    `baton` (repo-relative) goes to the mint at M+, which owns the baton checks; any other arm mints
    no baton, so it refuses one. A non-halted verdict carries the design-registry brief for the
    sizing's scope; a malformed registry with a non-empty scope halts `registry-unparseable`.
    `mint_design_rows` (a run-bound call) also appends one design row per hit to the register.
    """
    from coordinator_core import design_registry as dr
    from coordinator_core.ops.dispatch_emit import design_conformance as dc

    root = Path(repo_root)
    route_payload = None
    try:
        sizing = load_sizing(root, sizing_rel)
        scope = _design_scope(sizing, writes)
        route_payload, _ = design_route.read(sizing, sizing_rel)
    except SizingFireRefused:
        scope = []
    hits, error = dr.lookup(root, scope) if scope else ([], None)
    registry = dr.registry_path(root)
    try:
        shown = registry.relative_to(root.resolve()).as_posix()
    except ValueError:
        shown = None
    if error:
        return replace(_halt(HALT_REFUSAL, f"{error}: repair {shown or registry}"), design_route=route_payload)
    verdict = _arm_verdict(root, sizing_rel, writes=writes, baton=baton)
    if route_payload is not None and verdict.design_route is None:
        verdict = replace(verdict, design_route=route_payload)
    if verdict.halt is not None or not scope:
        return verdict
    registry_rel = shown if registry.is_file() else None
    brief = dc.brief_text(hits or [], registry_rel)
    if mint_design_rows and hits:
        dc.mint_rows(root, sizing_rel, hits)
    return replace(verdict, design_brief=brief) if brief else verdict


def _arm_verdict(
    repo_root: Path, sizing_rel: str, *, writes: Sequence[str] = (), baton: Optional[str] = None
) -> GateVerdict:
    """The arm-or-halt verdict, before the design-registry leg."""
    from coordinator_core import sizing_assemble as sa

    repo_root = Path(repo_root)
    try:
        sizing = load_sizing(repo_root, sizing_rel)
        arm = resolve_arm(sizing)
    except SizingFireRefused as exc:
        return _halt(HALT_REFUSAL, "; ".join(exc.fields))

    if baton and arm != ARM_M_PLUS:
        return _halt(HALT_REFUSAL, f"baton given but arm {arm} mints no baton")

    if sizing.get("xl_exit") == "split":
        return _halt(
            HALT_SPLIT,
            "xl_exit recorded as split: the bundle resolves to sibling runs, not one",
        )
    route = effective_route(sizing)
    if route == "shape":  # DR-450 exemption: only the PM can state the JTBD, so no workflow fronts shape
        return _halt(
            HALT_ROOM,
            f"route 'shape' is room-owned: {sa._ROOM_ENTRY['shape']}",
            route=route,
            handback=handback_line(sizing),
        )
    research = sizing.get("research")
    if isinstance(research, dict) and research:
        if writes:
            return _halt(HALT_REFUSAL, RESEARCH_MIXED_REFUSAL)
        return _halt(
            HALT_RESEARCH,
            f"research sizing {sizing_rel}: the run's `fire` entry is the one call",
            route=RESEARCH_HANDBACK_ROUTE,
            fire=[fire_command(sizing_rel)],
        )
    if arm != ARM_ROADMAP and route in sa._ROOM_ENTRY:
        extra = {}
        if route == "pm-decision" and apm_picks_xl_exit(sizing.get("interaction_mode")):
            # The script's xl-exit stage records the APM's pick and re-gates; pm mode halts here.
            extra["apm_xl_exit"] = True
        return _halt(
            HALT_ROOM,
            f"route {route!r} is room-owned: {sa._ROOM_ENTRY[route]}",
            route=route,
            **extra,
        )

    resume_sink: list = []
    refusals = collect_fire_refusals(
        sizing, sizing_rel=sizing_rel, arm=arm, writes=writes, repo_root=repo_root,
        resume_sink=resume_sink,
    )
    route_payload, route_bad = design_route.read(sizing, sizing_rel)
    if route_bad:
        refusals.append(route_bad)
    accepted_null = lacks_human_acceptance((sizing.get("exit_criterion") or {}).get("accepted"))
    mode = sizing.get("interaction_mode")
    tshirt = sizing["estimate"]["tshirt"]
    needs_acceptance = False
    if mode in sa.TOUCHPOINTS_BY_MODE:
        needs_acceptance = any(
            t["id"] in _ACCEPT_TOUCHPOINTS
            for t in sa.touchpoints(mode, tshirt, sizing.get("route"))
        )
    if any(r.startswith(STATEMENT_ABSENT_PREFIX) for r in refusals):
        # A sizing written exit-criterion-pending: the accept stage authors the statement in-run.
        others = [r for r in refusals if not r.startswith((STATEMENT_ABSENT_PREFIX, _ACCEPTED_NULL_PREFIX))]
        if others:
            return _halt(HALT_REFUSAL, "; ".join(others))
        return _halt(
            HALT_TOUCHPOINT,
            "no exit criterion on record",
            touchpoint=f"coordinator-invoke sizing.accept_exit_criterion with a statement for {sizing_rel}",
        )
    if accepted_null and needs_acceptance:
        line = next((r for r in refusals if r.startswith(_ACCEPTED_NULL_PREFIX)), "")
        return _halt(
            HALT_TOUCHPOINT,
            f"{mode} mode asks for the exit criterion at {tshirt}",
            touchpoint=line.partition("accept it first: ")[2]
            or f"coordinator-invoke sizing.accept_exit_criterion for {sizing_rel}",
        )
    acceptance = None
    if accepted_null:
        refusals = [r for r in refusals if not r.startswith(_ACCEPTED_NULL_PREFIX)]
    if accepted_null and sizing_acceptance_skipped(sizing.get("route"), tshirt):
        # Nobody was asked: the receipt says so, since `accepted` stays null.
        acceptance = {
            "by": ENGINE_SIZE_RULE,
            "route": sizing.get("route"),
            "tshirt": tshirt,
            "mode": mode,
            "ruling": "2026-10-08",
        }
    resume = None
    if resume_sink:
        resume = resume_sink[0]
    elif arm != ARM_ROADMAP:
        try:
            resume = resumable_plan(sizing, sizing_rel, repo_root, arm)
        except SizingFireRefused as exc:
            refusals.extend(f for f in exc.fields if f not in refusals)
    if refusals:
        return _halt(HALT_REFUSAL, "; ".join(refusals))
    try:
        resume_ledger = _resume_ledger(sizing, resume.ledger, repo_root) if resume else None
    except SizingFireRefused as exc:
        return _halt(HALT_REFUSAL, "; ".join(exc.fields))
    resume_plan = resume.plan_rel if resume and resume_ledger is None else None
    resume_rows = list(resume.incomplete) if resume and resume_ledger is None else None

    if arm != ARM_M_PLUS:
        # roadmap mints no baton: roadmap-blitz stubs are the batons
        return GateVerdict(
            arm=arm, halt=None, acceptance=acceptance, resume_plan=resume_plan, resume_rows=resume_rows,
            resume_ledger=resume_ledger, design_route=route_payload,
        )

    doc_new = _load_doc_new()
    try:
        baton = doc_new.mint_baton_from_sizing(sizing_rel, str(repo_root), baton=baton)
    except doc_new.SizingMintRefused as exc:
        return _halt(HALT_REFUSAL, str(exc))
    # The effective route, so a PM-recorded accept_multi_session plans in the
    # single-mode wave instead of re-adjudicating the sizing's raw `pm-decision`.
    minted = {"id": baton["id"], "path": baton["path"], "route": route}
    deliverable_id = str(sizing.get("deliverable_id") or "").strip()
    if deliverable_id:
        minted["deliverableId"] = deliverable_id
    spine, spine_refusal = (None, None) if resume else _spine_plan(repo_root, sizing, sizing_rel)
    if spine_refusal:
        return _halt(HALT_REFUSAL, spine_refusal)
    return GateVerdict(
        arm=arm,
        halt=None,
        baton=minted,
        acceptance=acceptance,
        resume_plan=resume_plan,
        resume_rows=resume_rows,
        spine_plan=spine,
        resume_ledger=resume_ledger,
        design_route=route_payload,
    )


def _resume_ledger(sizing: dict, halt, repo_root: Path) -> Optional[dict]:
    """The verdict's `resume_ledger` for a ledger-halt resume: run, plan, blocking ids, the
    sizing's rulable register ids and the plan's em-discharged rows as `{row_id: text}`; None when
    the resume carries no halt. `blocking` is the halt's blocking ids followed by every other rulable
    row (neither met nor waived), so rows recorded or left unjudged after the halt are judged too."""
    if halt is None:
        return None
    from coordinator_core.ops.dispatch_emit.requirements_ledger import read_notes, ruled_out_ids, run_dir
    from coordinator_core.ops.plan_tasks_mutate import read_em_discharges

    rows = (sizing.get("requirement_register") or {}).get("rows") or []
    out = set(ruled_out_ids([r for r in rows if isinstance(r, dict)]))
    rulable = [r for r in rulable_ids(rows) if r not in out]
    discharged = {
        str(row_id): entry["text"]
        for row_id, entry in read_em_discharges(Path(repo_root) / halt.plan_rel).items()
        if isinstance(entry, dict) and isinstance(entry.get("text"), str)
    }
    carried = [c for c in halt.carried_met if c not in out]
    blocking = [
        *(b for b in halt.blocking if b not in out),
        *(r for r in rulable if r not in halt.blocking and r not in carried),
    ]
    all_landed = halt.halted == ALL_LANDED_HALT_KIND
    verdict = {
        "run_id": halt.run_id,
        "plan": halt.plan_rel,
        "blocking": blocking,
        RESUME_LEDGER_CARRIED_MET_FIELD: carried,
        RESUME_LEDGER_HALT_BLOCKING_FIELD: [b for b in halt.blocking if b not in out],
        RESUME_LEDGER_EM_DISCHARGED_FIELD: discharged,
        "rulable_ids": rulable,
        RESUME_LEDGER_HALT_SHA_FIELD: halt.halt_sha,
        RESUME_LEDGER_REMAINING_FIELD: {k: v for k, v in halt.remaining.items() if k in blocking},
        RESUME_LEDGER_NOTES_FIELD: (
            [] if all_landed and halt.run_id == ALL_LANDED_NO_RUN else read_notes(run_dir(Path(repo_root), halt.run_id))
        ),
    }
    if all_landed:
        verdict[RESUME_LEDGER_KIND_FIELD] = ALL_LANDED_HALT_KIND
    if halt.halted == COMMIT_HALT_KIND:
        outcome = read_run_outcome(Path(repo_root), halt.run_id)
        if outcome is None:
            raise SizingFireRefused(
                [f"commit halt of run {halt.run_id} has no readable {RUN_OUTCOME_FILE}; its review verdict is unrecorded"]
            )
        verdict["inline_review"] = {**outcome["inline_review"], RESUMED_RUN_ID_KEY: halt.run_id}
    return verdict


def _spine_plan(repo_root: Path, sizing: dict, sizing_rel: str) -> tuple[Optional[str], Optional[str]]:
    """`(repo-relative spine path, None)` minted from a concrete sizing, `(None, None)` when the
    sizing is not concrete, or `(None, refusal)` when the spine cannot be minted.

    A file already at the path is never overwritten: when its rows' writes or checks differ from
    the sizing's, the refusal names the differing rows.
    """
    from coordinator_core.ops.dispatch_emit.inventory_mint import InventoryMintError
    from coordinator_core.ops.dispatch_emit.sizing_m_mint import (
        concrete_acceptance,
        mint_m_spine,
        spine_mismatch,
    )

    rows = concrete_acceptance(sizing)
    if rows is None:
        return None, None
    try:
        text, rel = mint_m_spine(sizing, sizing_rel=sizing_rel, rows=rows, repo_root=repo_root)
    except (InventoryMintError, ValueError) as exc:
        return None, f"spine-direct mint refused: {exc}"
    target = repo_root / rel
    if target.exists():
        bad = spine_mismatch(target.read_text(encoding="utf-8"), text)
        if bad:
            return None, (
                f"{rel.as_posix()} already exists and its rows differ from the sizing's acceptance_rows "
                f"({', '.join(bad)}); delete it to re-mint, or edit the rows to match"
            )
        return rel.as_posix(), None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return rel.as_posix(), None


def _record_refusal(record: dict) -> Optional[str]:
    """Why `record` is malformed, else None; checks shape only, the writers own the semantics."""
    kind = record.get("kind")
    if kind not in _RECORD_KINDS:
        return f"dispatch.ask_gate params.record.kind must be one of {list(_RECORD_KINDS)}"
    if kind == design_route.RECORD_KIND:
        if "apm_ruling" in record:
            return "dispatch.ask_gate params.record.apm_ruling is refused for design_approval; the PM approves a canvas"
        for name in ("board_url", "pm_ruling", "ruling_ref"):
            if not isinstance(record.get(name), str) or not record[name].strip():
                return f"dispatch.ask_gate params.record.{name} must be a non-empty string"
        return None
    for name in ("apm_ruling", "ruling_ref"):
        if not isinstance(record.get(name), str) or not record[name].strip():
            return f"dispatch.ask_gate params.record.{name} must be a non-empty string"
    statement = record.get("statement")
    if statement is not None and (not isinstance(statement, str) or not statement.strip()):
        return "dispatch.ask_gate params.record.statement must be a non-empty string"
    if kind == "xl_exit" and record.get("xl_exit") not in _XL_PICKS:
        return f"dispatch.ask_gate params.record.xl_exit must be one of {list(_XL_PICKS)}"
    return None


def _write_record(repo_root: Path, sizing_rel: str, record: dict) -> dict:
    """Write the ruling through its kind's single writer; `{"ok": bool, "error"?: str}`."""
    # Writer modules import lazily: they sit outside dispatch_emit and are needed only under `record`.
    if record["kind"] == design_route.RECORD_KIND:
        from coordinator_core.ops.dispatch_emit.design_approval import record as write_approval

        reply = write_approval(repo_root, sizing_rel, board_url=record["board_url"])
        if reply.get("exit_code") == 0:
            return {"ok": True, "pm_ruling": record["pm_ruling"], "ruling_ref": record["ruling_ref"]}
        return {"ok": False, "error": str(reply.get("error") or "record refused")}
    base = {"sizing": sizing_rel, "apm_ruling": record["apm_ruling"], "ruling_ref": record["ruling_ref"]}
    if record["kind"] == "exit_criterion":
        from coordinator_core.ops.sizing_accept_exit_criterion import _handler as writer

        if record.get("statement"):
            base["statement"] = record["statement"]
    else:
        from coordinator_core.ops.sizing_record_xl_exit import _handler as writer

        base["xl_exit"] = record["xl_exit"]
    reply = writer(base, repo_root)
    if reply.get("exit_code") == 0:
        return {"ok": True}
    return {"ok": False, "error": str(reply.get("error") or reply.get("message") or "record refused")}


@register_op("dispatch.ask_gate")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.ask_gate": params `sizing_path` (str, required), `writes` (list[str]), `baton` (str),
    `record` (object: kind exit_criterion|xl_exit, apm_ruling, ruling_ref, statement?, xl_exit?; or kind
    design_approval, board_url, pm_ruling, ruling_ref: the canvas board link and the PM's verbatim approval,
    echoed in `recorded` and not stored),
    `run_id` (str: the warp run asking), `cross_repo_approved` (bool) and `session_id` (str): the stage params
    for a ledger-resume verdict, which with `run_id` gains `manifest` (the `dispatch.ask_stage` reply on its
    `resume_ledger.plan`, or its `{error}`); no other verdict carries the key.

    `route_override_reason` (str): why the sizing routes against the run's `route-preference.json`; with a
    `run_id` whose record holds a preference it is written to `em_analysis`, and a divergence with no reason
    on the call or the sizing replies a refusal halt.

    With `run_id`, the run is bound to the sizing first (`_bind_run`), whatever the verdict. With
    `record`, writes that ruling through its single writer, then gates the sizing as it stands and
    adds `recorded` {ok, error?, pm_ruling?, ruling_ref?} to the reply; a refused write still returns the verdict. Replies
    `GateVerdict.to_json()`; a malformed request replies `{"error": str}` and writes nothing.
    """
    if repo_root is None:
        return {"error": "dispatch.ask_gate requires repo_root"}
    refusal = validate_params("dispatch.ask_gate", params, _PARAMS)
    if refusal is not None:
        return refusal
    record = params.get("record")
    if record is not None:
        refusal = _record_refusal(record)
        if refusal is not None:
            return {"error": refusal}
    sizing_rel = params["sizing_path"]
    writes = params.get("writes") or []
    root = main_worktree_root(Path(repo_root))
    if params.get("run_id"):
        refused = _bind_run(
            root, params["run_id"], sizing_rel, route_override_reason=params.get("route_override_reason")
        )
        if refused is not None:
            return _halt(HALT_REFUSAL, refused).to_json()
    recorded = _write_record(root, sizing_rel, record) if record is not None else None
    verdict = gate(
        root, sizing_rel, writes=writes, baton=params.get("baton"),
        mint_design_rows=bool(params.get("run_id")),
    )
    if params.get("run_id"):
        _record_resume_snapshot(root, params["run_id"], verdict)
    out = (replace(verdict, recorded=recorded) if recorded is not None else verdict).to_json()
    if params.get("run_id") and verdict.halt is None and verdict.resume_ledger is not None:
        try:
            out["manifest"] = manifest_payload(root, stage(
                root,
                run_id=params["run_id"],
                plan_rel=verdict.resume_ledger["plan"],
                writes=writes,
                cross_repo_approved=bool(params.get("cross_repo_approved")),
                session_id=params.get("session_id") or None,
                commit_sizing_rel=sizing_rel,
                ledger_resume=True,
            ))
        except (AskStageError, SizingFireRefused) as exc:
            out["manifest"] = {"error": str(exc)}
    return out


def _record_resume_snapshot(root: Path, run_id: str, verdict: GateVerdict) -> None:
    """Write the run's resume snapshot (the plan's row ids minus `resume_rows`) for a row resume,
    else clear it, so a snapshot from an earlier gate call never outlives its verdict. Every spine
    entry counts, owed and gated rows included. One plan read, no git spawn."""
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.dispatch_emit import resume_snapshot
    from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo

    if verdict.halt is None and verdict.resume_plan and verdict.resume_rows:
        plan = root / verdict.resume_plan
        try:
            result = load_rows_memo(plan.read_text(encoding="utf-8"))
        except OSError:
            result = None
        if result is not None and result.status is LocateStatus.LOCATED:
            ids = {r["id"] for r in result.rows if isinstance(r, dict) and isinstance(r.get("id"), str)}
            resume_snapshot.write(root, run_id, verdict.resume_plan, ids - set(verdict.resume_rows))
            return
    resume_snapshot.clear(root, run_id)


def _bind_run(
    root: Path, run_id: str, sizing_rel: str, route_override_reason: Optional[str] = None
) -> Optional[str]:
    """Bind the run to the sizing it is gating: the sizing's requirement_register takes the PM's
    words, the raw ask (a relayed ask under its own origin) and any queued revision
    (`requirements_ledger.bind`), and the run's token record names the sizing, so
    `run_token.run_owns_sizing` claims it and nothing else. The refusal text when the
    requirements cannot be recorded, else None."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger, run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_TEXT_FILE, RELAYED_ASK_FILE

    def _text(path: Path) -> Optional[str]:
        return path.read_text(encoding="utf-8") if path.is_file() else None

    try:
        run_dir = requirements_ledger.run_dir(root, run_id)
        requirements_ledger.bind(
            root, run_id, sizing_rel,
            ask_text=_text(run_dir / ASK_TEXT_FILE), relayed_text=_text(run_dir / RELAYED_ASK_FILE),
        )
    except (requirements_ledger.LedgerRefused, OSError) as exc:
        return f"requirements: {exc}"
    refused = _bind_route_preference(root, run_id, sizing_rel, route_override_reason)
    if refused is not None:
        return refused
    run_token.bind_sizing(root, run_id, sizing_rel)
    return None


def _bind_route_preference(
    root: Path, run_id: str, sizing_rel: str, route_override_reason: Optional[str]
) -> Optional[str]:
    """Record the run's EM route preference on the sizing's `em_analysis`; the refusal text when the
    sizing routes against it with no stated reason, else None. Writes nothing when the sizing already
    holds the values, and nothing for a run with no `route-preference.json`."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger, route_preference as rp

    record = requirements_ledger.run_dir(root, run_id) / rp.ROUTE_PREFERENCE_FILE
    if not record.is_file():
        return None
    try:
        pref = rp.from_json(record.read_text(encoding="utf-8"))
    except OSError:
        return None
    if pref.is_empty():
        return None
    reason = route_override_reason.strip() if isinstance(route_override_reason, str) else ""

    import yaml

    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    def mutate(old: str) -> str:
        doc = yaml.safe_load(old) or {}
        em = doc.get("em_analysis")
        em = dict(em) if isinstance(em, dict) else {}
        route = effective_route(doc)
        held = em.get(rp.ROUTE_OVERRIDE_REASON_KEY)
        held = held.strip() if isinstance(held, str) else ""
        want = {**em, **rp.to_em_analysis(pref)}
        if isinstance(route, str) and route and rp.diverges(pref, route):
            if not (reason or held):
                raise MutateAbort(
                    f"route_override_reason: the sizing routes {route} against route_preference "
                    f"{pref.route_preference}; state why in route_override_reason."
                )
            if reason:
                want[rp.ROUTE_OVERRIDE_REASON_KEY] = reason
        if want == em:
            return old
        doc["em_analysis"] = want
        crlf = "\r\n" in old
        text = yaml.safe_dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True, width=100000)
        text = requirements_ledger._validated(text)
        return text.replace("\n", "\r\n") if crlf else text

    try:
        locked_rmw(requirements_ledger._sizing_path(root, sizing_rel), mutate, repo_root=Path(root))
    except MutateAbort as exc:
        return str(exc.args[0]) if exc.args else "route preference write refused"
    except (LockTimeout, requirements_ledger.LedgerRefused, OSError) as exc:
        return f"route_preference: {exc}"
    return None
