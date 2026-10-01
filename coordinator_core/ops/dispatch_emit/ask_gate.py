"""dispatch.ask_gate handler: one sizing in, an arm to run or a halt (room, touchpoint, refusal) out.

Reads the sizing; at M+ with no halt it mints the baton record in-process (a write).
"""

from __future__ import annotations

import functools
import importlib.util
import sys
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
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.dispatch_emit.sizing_fire import (
    ARM_M_PLUS,
    SizingFireRefused,
    collect_fire_refusals,
    load_sizing,
    resolve_arm,
)

_ACCEPT_TOUCHPOINTS = frozenset({"accept_sizing", "accept_exit_criterion"})
_ACCEPTED_NULL_PREFIX = "`exit_criterion.accepted`"
_PARAMS = (Field("sizing_path", "nonempty_str", required=True), Field("writes", "str_list"))
_DOC_NEW = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "coordinator-doc-new.py"


def _halt(kind: str, reason: str, **extra: str) -> GateVerdict:
    return GateVerdict(arm=None, halt={"kind": kind, "reason": reason, **extra})


@functools.cache
def _load_doc_new():
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_for_ask_gate", _DOC_NEW)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def gate(repo_root: Path, sizing_rel: str, *, writes: Sequence[str] = ()) -> GateVerdict:
    """Arm for the sizing at `sizing_rel`, or the single halt that stops it, naming every cause."""
    from coordinator_core import sizing_assemble as sa

    repo_root = Path(repo_root)
    try:
        sizing = load_sizing(repo_root, sizing_rel)
        arm = resolve_arm(sizing)
    except SizingFireRefused as exc:
        return _halt(HALT_REFUSAL, "; ".join(exc.fields))

    route = sizing.get("route")
    if route in sa._ROOM_ENTRY:
        return _halt(
            HALT_ROOM,
            f"route {route!r} is room-owned: {sa._ROOM_ENTRY[route]}",
            route=route,
        )

    refusals = collect_fire_refusals(
        sizing, sizing_rel=sizing_rel, arm=arm, writes=writes, repo_root=repo_root
    )
    accepted_null = (sizing.get("exit_criterion") or {}).get("accepted") is None
    mode = sizing.get("interaction_mode")
    tshirt = sizing["estimate"]["tshirt"]
    needs_acceptance = False
    if mode in sa.TOUCHPOINTS_BY_MODE:
        needs_acceptance = any(
            t["id"] in _ACCEPT_TOUCHPOINTS for t in sa.touchpoints(mode, tshirt)
        )
    if accepted_null and needs_acceptance:
        line = next((r for r in refusals if r.startswith(_ACCEPTED_NULL_PREFIX)), "")
        return _halt(
            HALT_TOUCHPOINT,
            f"{mode} mode asks for the exit criterion at {tshirt}",
            touchpoint=line.partition("accept it first: ")[2]
            or f"coordinator-invoke sizing.accept_exit_criterion for {sizing_rel}",
        )
    if accepted_null:
        refusals = [r for r in refusals if not r.startswith(_ACCEPTED_NULL_PREFIX)]
    if refusals:
        return _halt(HALT_REFUSAL, "; ".join(refusals))

    if arm != ARM_M_PLUS:
        return GateVerdict(arm=arm, halt=None)

    doc_new = _load_doc_new()
    try:
        baton = doc_new.mint_baton_from_sizing(sizing_rel, str(repo_root))
    except doc_new.SizingMintRefused as exc:
        return _halt(HALT_REFUSAL, str(exc))
    return GateVerdict(arm=arm, halt=None, baton={"id": baton["id"], "path": baton["path"]})


@register_op("dispatch.ask_gate")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.ask_gate": params `sizing_path` (str, required), `writes` (list[str]).

    Replies `GateVerdict.to_json()`; a malformed request replies `{"error": str}`.
    """
    if repo_root is None:
        return {"error": "dispatch.ask_gate requires repo_root"}
    refusal = validate_params("dispatch.ask_gate", params, _PARAMS)
    if refusal is not None:
        return refusal
    sizing_rel = params["sizing_path"]
    writes = params.get("writes") or []
    return gate(main_worktree_root(Path(repo_root)), sizing_rel, writes=writes).to_json()
