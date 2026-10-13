"""dispatch.ask_gate handler: one sizing in, an arm to run or a halt (room, touchpoint, refusal) out.

Reads the sizing; at M+ with no halt it mints the baton record in-process (a write). An unfired
plan already on disk rides the verdict as `resume_plan`.
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
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_REFUSAL,
    HALT_ROOM,
    HALT_TOUCHPOINT,
    GateVerdict,
)
from coordinator_core.ops.sizing_acceptance import (
    ENGINE_SIZE_RULE,
    apm_picks_xl_exit,
    lacks_human_acceptance,
    sizing_acceptance_skipped,
)
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.dispatch_emit.sizing_fire import (
    ARM_M_PLUS,
    ARM_ROADMAP,
    STATEMENT_ABSENT_PREFIX,
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
)
_RECORD_KINDS = ("exit_criterion", "xl_exit")
_XL_PICKS = ("roadmap", "accept_multi_session")
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


def gate(
    repo_root: Path, sizing_rel: str, *, writes: Sequence[str] = (), baton: Optional[str] = None
) -> GateVerdict:
    """Arm for the sizing at `sizing_rel`, or the single halt that stops it, naming every cause.

    `baton` (repo-relative) goes to the mint at M+, which owns the baton checks; any other arm mints
    no baton, so it refuses one.
    """
    from coordinator_core import sizing_assemble as sa

    repo_root = Path(repo_root)
    try:
        sizing = load_sizing(repo_root, sizing_rel)
        arm = resolve_arm(sizing)
    except SizingFireRefused as exc:
        return _halt(HALT_REFUSAL, "; ".join(exc.fields))

    if baton and arm != ARM_M_PLUS:
        return _halt(HALT_REFUSAL, f"baton given but arm {arm} mints no baton")

    route = effective_route(sizing)
    if route == "shape":  # DR-450 exemption: only the PM can state the JTBD, so no workflow fronts shape
        return _halt(
            HALT_ROOM,
            f"route 'shape' is room-owned: {sa._ROOM_ENTRY['shape']}",
            route=route,
            handback=handback_line(sizing),
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

    refusals = collect_fire_refusals(
        sizing, sizing_rel=sizing_rel, arm=arm, writes=writes, repo_root=repo_root
    )
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
    if arm != ARM_ROADMAP:
        try:
            resume = resumable_plan(sizing, sizing_rel, repo_root, arm)
        except SizingFireRefused as exc:
            refusals.extend(f for f in exc.fields if f not in refusals)
    if refusals:
        return _halt(HALT_REFUSAL, "; ".join(refusals))

    if arm != ARM_M_PLUS:
        # roadmap mints no baton: roadmap-blitz stubs are the batons
        return GateVerdict(arm=arm, halt=None, acceptance=acceptance, resume_plan=resume)

    doc_new = _load_doc_new()
    try:
        baton = doc_new.mint_baton_from_sizing(sizing_rel, str(repo_root), baton=baton)
    except doc_new.SizingMintRefused as exc:
        return _halt(HALT_REFUSAL, str(exc))
    # The effective route, so a PM-recorded accept_multi_session plans in the
    # single-mode wave instead of re-adjudicating the sizing's raw `pm-decision`.
    return GateVerdict(
        arm=arm,
        halt=None,
        baton={"id": baton["id"], "path": baton["path"], "route": route},
        acceptance=acceptance,
        resume_plan=resume,
    )


def _record_refusal(record: dict) -> Optional[str]:
    """Why `record` is malformed, else None; checks shape only, the writers own the semantics."""
    kind = record.get("kind")
    if kind not in _RECORD_KINDS:
        return f"dispatch.ask_gate params.record.kind must be one of {list(_RECORD_KINDS)}"
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
    `record` (object: kind exit_criterion|xl_exit, apm_ruling, ruling_ref, statement?, xl_exit?).

    With `record`, writes that ruling through its single writer, then gates the sizing as it stands and
    adds `recorded` {ok, error?} to the reply; a refused write still returns the verdict. Replies
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
    recorded = _write_record(root, sizing_rel, record) if record is not None else None
    verdict = gate(root, sizing_rel, writes=writes, baton=params.get("baton"))
    return (replace(verdict, recorded=recorded) if recorded is not None else verdict).to_json()
