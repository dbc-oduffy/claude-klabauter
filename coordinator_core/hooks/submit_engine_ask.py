"""coordinator_core.hooks.submit_engine_ask — PreToolUse(Workflow): the EM's submission of an
engineering ask IS the running Workflow.

The EM's one act on an ask is `Workflow({name: "coordinator:engine-ask", args: {ask: "<PM's words,
verbatim>"}})` (or `args: {sizing: "<state/sizings/x.yaml>"}` for a routed sizing). The harness
resolves `name` to the plugin's `workflows/engine-ask.js` stub before any PreToolUse hook runs,
so the stub must exist; this hook then emits the warp run through `dispatch.emit` (the ask route
`emit-dispatch-workflow --ask/--sizing` takes: run token, receipt, `ask.md` with the verbatim words
written by the engine) and answers allow + `updatedInput` = `{scriptPath: <emitted>, args:
<fire args>}`. `updatedInput` replaces the whole tool input, so the call the EM made launches
the emitted run, in the background, under `claude -p` as interactively; its completion
notification is the EM's one wake. The stub only ever runs when this hook did not: it returns a
halt that names the gap, never a run.

`args` shape: `{ask: str}` | `{sizing: str}`, optional `repo` (absolute path; else the payload
cwd's repo), optional `source: "pm" | "relayed"` (ask only; absent is `pm`; a relayed ask is
forwarded to the engine as `ask_source: "relayed"`), optional `cross_repo_approved: true` (the
PM's local cross-repo assent; implied on a remote venue). A bare string `args` is the ask.
Refusals deny with the engine's own reason; a submission for a sizing a live run is already
working denies naming that run (`coordinator_core.warp_live`).

Also here, `sizing_scout_deny`: the main loop's `Agent(sizing-scout)` is the hand route into the
sizing agent the submission replaces, so it is denied while this session has a live run, naming
that run. Called from `hooks.block_unenumerated_agent_type.check` (the engine leg DoE's
PreToolUse(Agent) fan-in already runs). A Workflow's own `agent()` spawns raise no
PreToolUse(Agent), so a workflow that dispatches the sizing scout (plan-blitz) is untouched.

Negative spec: never launches a process — the harness runs the rewritten call. Never edits
the emitted script. Never denies a Workflow call that does not name the stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from coordinator_core.hooks._envelope import deny, no_advisory, payload_of, rewrite_input
from coordinator_core.ipc import register_op
from coordinator_core.warp_fire import SUBMIT_WORKFLOW_NAME, is_submit_name

GUARD_NAME = "submit-engine-ask"
SIZING_SCOUT_TYPES = frozenset({"coordinator:sizing-scout", "sizing-scout"})

_ARGS_SHAPE = 'args: {ask: "<the PM\'s words, verbatim>"} or {sizing: "<state/sizings/x.yaml>"}'

EmitFn = Callable[[dict, Path], dict]


def is_submission(payload: Mapping[str, Any]) -> bool:
    if payload.get("tool_name") != "Workflow":
        return False
    tool_input = payload.get("tool_input")
    return isinstance(tool_input, dict) and is_submit_name(tool_input.get("name"))


def _request(
    tool_input: Mapping[str, Any],
) -> tuple[Optional[str], Optional[str], Optional[str], Any]:
    """(ask, sizing_rel, repo, source) from the call's `args`; `source` is None when absent."""
    args = tool_input.get("args")
    if isinstance(args, str):
        return (args if args.strip() else None), None, None, None
    if not isinstance(args, dict):
        return None, None, None, None
    ask = args.get("ask")
    sizing = args.get("sizing")
    repo = args.get("repo")
    return (
        ask if isinstance(ask, str) and ask.strip() else None,
        sizing.strip() if isinstance(sizing, str) and sizing.strip() else None,
        repo.strip() if isinstance(repo, str) and repo.strip() else None,
        args.get("source"),
    )


def _repo_root(repo_arg: Optional[str], cwd: object) -> Optional[Path]:
    from coordinator_core.git.repo_root import show_toplevel

    base = cwd if isinstance(cwd, str) and cwd else None
    start = repo_arg
    if start and base and not Path(start).is_absolute():
        start = str(Path(base) / start)
    top = show_toplevel(start or base)
    return Path(top) if top else None


def _default_emit(params: dict, repo_root: Path) -> dict:
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    return _dispatch_emit(params, repo_root)


def _refuse(text: str) -> dict:
    return deny("PreToolUse", f"{text}\nSubmit shape: Workflow({{name: \"{SUBMIT_WORKFLOW_NAME}\", {_ARGS_SHAPE}}}).")


def evaluate(payload: Mapping[str, Any], *, emit: Optional[EmitFn] = None) -> dict:
    """The PreToolUse envelope for a Workflow call: the fire rewrite for a submission, else nothing."""
    if not is_submission(payload):
        return no_advisory()
    if payload.get("agent_id"):
        return deny("PreToolUse", "Only the EM submits an ask to the engine; return it in your report.")

    ask, sizing_rel, repo_arg, source = _request(payload["tool_input"])
    if (ask is None) == (sizing_rel is None):
        return _refuse("The submission carries neither an ask nor a sizing, or both.")
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_SOURCE_RELAYED, ASK_SOURCES

    if source is not None:
        if source not in ASK_SOURCES:
            return _refuse(f"source {source!r} is not one of: {', '.join(ASK_SOURCES)}.")
        if sizing_rel is not None:
            return _refuse("source marks an ask's text; a sizing submission carries none.")

    root = _repo_root(repo_arg, payload.get("cwd"))
    if root is None:
        where = repo_arg or payload.get("cwd") or "the session cwd"
        return _refuse(f"{where} is in no git repo. Add `repo: \"<absolute repo path>\"` to args.")

    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None

    from coordinator_core import warp_live

    if sizing_rel:
        from coordinator_core.ops.dispatch_emit import run_token

        stale = warp_live.unlaunched_run_for_sizing(root, sizing_rel, session_id)
        if stale:
            run_token.consume(root, stale)
        live = warp_live.live_run_for_sizing(
            root, sizing_rel, session_id=session_id, transcript_path=transcript
        )
        if live:
            return deny(
                "PreToolUse",
                f"Run {live} is already working {sizing_rel}; its completion is your one wake. "
                "End the turn.",
            )

    params: dict = {"session_id": session_id or ""}
    if sizing_rel:
        params.update({"ask": True, "sizing_path": sizing_rel})
    else:
        params["ask"] = ask
        if source == ASK_SOURCE_RELAYED:
            params["ask_source"] = ASK_SOURCE_RELAYED
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import is_remote_venue

    args = payload["tool_input"].get("args")
    if is_remote_venue() or (isinstance(args, dict) and args.get("cross_repo_approved") is True):
        params["cross_repo_approved"] = True

    try:
        result = (emit or _default_emit)(params, root)
    except Exception as exc:  # the engine's refusal text is the whole message
        return _refuse(f"The engine refused the submission: {exc}")

    script = result.get("path") if isinstance(result, dict) else None
    if not script or result.get("ok") is False:
        detail = result.get("findings") if isinstance(result, dict) else result
        stale = result.get("run_id") if isinstance(result, dict) else None
        if stale:
            from coordinator_core.ops.dispatch_emit import run_token

            run_token.consume(root, str(stale))
        return _refuse(f"The engine emitted no run: {detail}")

    run_id = result.get("run_id") or Path(script).name
    call_id = payload.get("tool_use_id")
    try:
        warp_live.record_submission(
            root,
            str(run_id),
            session_id=session_id,
            transcript_path=transcript,
            script_path=str(script),
            sizing_rel=sizing_rel,
            tool_use_id=call_id if isinstance(call_id, str) else None,
        )
    except OSError:
        pass  # liveness then reads the run token alone

    new_input: dict = {"scriptPath": str(script)}
    if result.get("fire_args"):
        new_input["args"] = result["fire_args"]
    envelope = rewrite_input(
        "PreToolUse",
        new_input,
        f"Submitted to the engine as run {run_id}. It runs to its commit in the background; its "
        "completion notification is your one wake. End the turn: no polling, dispatch or "
        "verification meanwhile.",
    )
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = f"Engine submission: emitted {Path(script).name} this call."
    return envelope


def sizing_scout_deny(payload: Mapping[str, Any]) -> Optional[dict]:
    """Deny the main loop's hand dispatch of the sizing scout while this session has a live run.

    Order is load-bearing: tool, agent_id and resolved type are checked before any file is read, so
    no other Agent dispatch pays a launch-record directory listing.
    """
    if payload.get("tool_name") != "Agent" or payload.get("agent_id"):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    from coordinator_core.hooks.block_unenumerated_agent_type import resolve_subagent_type

    if resolve_subagent_type(tool_input) not in SIZING_SCOUT_TYPES:
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    root = _repo_root(None, payload.get("cwd"))
    if root is None:
        return None
    from coordinator_core import warp_live

    transcript = payload.get("transcript_path")
    live = warp_live.live_run_for_session(
        root, session_id, transcript_path=transcript if isinstance(transcript, str) else None
    )
    if not live:
        return None
    from coordinator_core.machine_profile import apply_guard_level

    return apply_guard_level(
        GUARD_NAME,
        deny("PreToolUse", f"Run {live} is already sizing; its completion is your one wake. End the turn."),
    )


@register_op("hooks.submit_engine_ask")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Workflow) door onto `evaluate`; a deny resolves at the guard-level policy point."""
    payload = payload_of(params)
    from coordinator_core.machine_profile import apply_guard_level

    return apply_guard_level(GUARD_NAME, evaluate(payload)) or no_advisory()


__all__ = [
    "GUARD_NAME",
    "SIZING_SCOUT_TYPES",
    "evaluate",
    "is_submission",
    "sizing_scout_deny",
]
