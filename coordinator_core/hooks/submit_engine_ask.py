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

`args` shape: `{ask: str}` | `{sizing: str}` | `{asks: [{ask, source?, label?, writes?}], confirm_split?}` (each ask a
separate sibling run via `dispatch.ask_fanout`; the call fires the first ready run and the allow reason
carries `{parent_id, runs}` plus the other ready runs' fire calls; a one-element array is an `ask`; a
slice `{sizing}` is denied until its `after` predecessors land and while its parent is at budget), optional `repo` (absolute path; else the payload
cwd's repo, or for an ask whose absolute `context` lies in another registered repo, that repo), optional `source: "pm" | "relayed"` (absent is `pm`; on `asks` it is the default for items naming
no `source` of their own; a relayed ask is forwarded to the engine as `ask_source: "relayed"`), optional EM brief beside either arm
(`brief` inline text, `brief_file` repo path, `context` path or list of paths; carried to the emit
op's `ask_brief` / `ask_brief_file` / `ask_context`, whose refusal is the deny reason), optional `cross_repo_approved: true` (the
PM's local cross-repo assent; implied on a remote venue). A bare string `args` is the ask.
A sizing carrying a non-empty `research` block is emitted on the research route (`from_sizing`,
with `context` paths as the route's `context`; `brief`/`brief_file` are refused there); any other
sizing takes the plan chain (`ask` + `sizing_path`); only the plan chain echoes inherited revisions.
Refusals deny with the engine's own reason; a submission for a sizing a live run is already
working denies naming that run (`coordinator_core.warp_live`).

Also here, `sizing_scout_deny`: the main loop's `Agent(sizing-scout)` is the hand route into the
sizing agent the submission replaces, so it is always denied: naming the live run when this
session has one, else the Workflow ask submission that carries sizing through to bookkeeping.
Called from `hooks.block_unenumerated_agent_type.check` (the engine leg DoE's PreToolUse(Agent)
fan-in already runs). A Workflow's own `agent()` spawns raise no
PreToolUse(Agent), so a workflow that dispatches the sizing scout (plan-blitz) is untouched.

Negative spec: never launches a process — the harness runs the rewritten call. Never edits
the emitted script. Never denies a Workflow call that does not name the stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from coordinator_core.hooks._envelope import deny, no_advisory, payload_of, rewrite_input
from coordinator_core.hooks.sizing_scout_deny import (
    ARGS_SHAPE,
    GUARD_NAME,
    SIZING_SCOUT_TYPES,
    sizing_scout_deny as _scout_deny,
)
from coordinator_core.ipc import register_op
from coordinator_core.warp_fire import SUBMIT_WORKFLOW_NAME, fire_command, is_submit_name


_ARGS_SHAPE = ARGS_SHAPE

EmitFn = Callable[[dict, Path], dict]
FanoutFn = Callable[[dict, Path], dict]  # the contract's FANOUT_OP signature (ask_fanout_contract.FanoutFn)


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


def _brief_params(tool_input: Mapping[str, Any]) -> tuple[dict, Optional[str]]:
    """(emit params, refusal) for the EM's optional `brief` / `brief_file` / `context` args."""
    args = tool_input.get("args")
    if not isinstance(args, dict):
        return {}, None
    params: dict = {}
    for key, param in (("brief", "ask_brief"), ("brief_file", "ask_brief_file")):
        value = args.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return {}, f"{key} must be a string."
        if value.strip():
            params[param] = value
    context = args.get("context")
    if context is not None:
        items = [context] if isinstance(context, str) else context
        if not isinstance(items, list) or not all(isinstance(c, str) for c in items):
            return {}, "context must be a string (a path or inline prose) or a list of them."
        paths = [c.strip() for c in items if _is_path_shaped(c)]
        prose = [c.strip() for c in items if c.strip() and not _is_path_shaped(c)]
        if paths:
            params["ask_context"] = paths
        if prose:
            params["ask_brief"] = "\n\n".join(([params["ask_brief"]] if "ask_brief" in params else []) + prose)
    return params, None


def _is_path_shaped(item: str) -> bool:
    """A single whitespace-free token carrying a separator or an extension; anything else is prose."""
    text = item.strip()
    return bool(text) and not any(ch.isspace() for ch in text) and ("/" in text or "\\" in text or "." in text)


def _missing_context(root: Path, paths: list[str]) -> Optional[str]:
    """The first path-shaped context that names no existing file (relative ones against `root`)."""
    for raw in paths:
        p = Path(raw.replace("\\", "/"))
        if not (p if p.is_absolute() else root / p).is_file():
            return raw
    return None


def _repo_root(repo_arg: Optional[str], cwd: object) -> Optional[Path]:
    from coordinator_core.git.repo_root import show_toplevel

    base = cwd if isinstance(cwd, str) and cwd else None
    start = repo_arg
    if start and base and not Path(start).is_absolute():
        start = str(Path(base) / start)
    top = show_toplevel(start or base)
    return Path(top) if top else None


def _context_home(root: Path, paths: list[str]) -> Path:
    """The registered repo holding the first absolute context path that lies outside `root`; else `root`."""
    resolved_root = root.resolve()
    for raw in paths:
        p = Path(raw.replace("\\", "/"))
        if not p.is_absolute():
            continue
        try:
            p.resolve().relative_to(resolved_root)
            continue
        except ValueError:
            pass
        from coordinator_core.bash_guards._write_bump_applicability import target_is_registered_repo
        from coordinator_core.git.repo_root import show_toplevel

        top = show_toplevel(str(p.parent))
        if top and target_is_registered_repo(top):
            return Path(top)
        return root
    return root


def _default_emit(params: dict, repo_root: Path) -> dict:
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    return _dispatch_emit(params, repo_root)


def _load_sizing_doc(root: Path, sizing_rel: str) -> dict:
    """The sizing mapping; an unreadable sizing reads `{}` and is left to the engine's own refusal."""
    from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, load_sizing

    try:
        return load_sizing(root, sizing_rel)
    except SizingFireRefused:
        return {}


def _has_research_block(sizing: Mapping[str, Any]) -> bool:
    research = sizing.get("research")
    return isinstance(research, dict) and bool(research)


def _asks(tool_input: Mapping[str, Any]) -> tuple[Optional[list], Optional[str]]:
    """(asks, refusal): `asks` is None when the call carries no `asks` key."""
    args = tool_input.get("args")
    if not isinstance(args, dict) or "asks" not in args:
        return None, None
    asks = args["asks"]
    if not isinstance(asks, list) or not asks:
        return None, "asks must be a non-empty list of {ask, source?, label?, writes?}."
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_SOURCES

    for i, item in enumerate(asks):
        if not isinstance(item, dict) or not isinstance(item.get("ask"), str) or not item["ask"].strip():
            return None, f"asks[{i}] carries no ask text."
        item_source = item.get("source")
        if item_source is not None and item_source not in ASK_SOURCES:
            return None, f"asks[{i}].source must be one of: {', '.join(ASK_SOURCES)}."
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            return None, f"asks[{i}].label must be a string."
        writes = item.get("writes")
        if writes is not None and not (isinstance(writes, list) and all(isinstance(w, str) for w in writes)):
            return None, f"asks[{i}].writes must be a list of repo-relative paths."
    confirm = args.get("confirm_split")
    if confirm is not None and not isinstance(confirm, bool):
        return None, "confirm_split must be true or false."
    return asks, None


def _status_of(root: Path, run_id: str) -> Optional[str]:
    """A run's `sibling-status.json` status; None when the run has not bound or the file is unreadable."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import status_rel

    try:
        data = json.loads((root / status_rel(run_id)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    status = data.get("status") if isinstance(data, dict) else None
    return status if isinstance(status, str) else None


def _read_parent(root: Path, parent_id: str):
    import json

    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import ParentRecord, parent_record_rel

    try:
        return ParentRecord.from_json(json.loads((root / parent_record_rel(parent_id)).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _slice_deny(root: Path, marker: tuple[str, str]) -> Optional[str]:
    """Why a slice sizing may not start now: an unlanded `after` predecessor, or a full budget."""
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import (
        STATUS_AFTER, STATUS_HALTED, STATUS_LANDED, STATUS_QUEUED, STATUS_RUNNING, concurrent_run_budget,
    )

    parent_id, run_id = marker
    parent = _read_parent(root, parent_id)
    mine = next((r for r in (parent.runs if parent else ()) if r.run_id == run_id), None)
    if mine is None:
        return f"Run {run_id} is not in parent {parent_id}'s record. Submit the ask as a fresh {{ask}}."
    for pred in mine.after:
        state = _status_of(root, pred)
        if state == STATUS_LANDED:
            continue
        if state == STATUS_HALTED:
            return f"Predecessor {pred} halted, so {run_id} cannot start. Resubmit the slice as a fresh {{ask}}."
        return f"Predecessor {pred} has not landed ({state or 'not started'}); its close names this run's fire call."
    if mine.status in (STATUS_QUEUED, STATUS_AFTER):
        running = [r.run_id for r in parent.runs if r.run_id != run_id and _status_of(root, r.run_id) == STATUS_RUNNING]
        if len(running) >= concurrent_run_budget():
            return f"Parent {parent_id} is at its run budget; running: {', '.join(running)}. A close names this run's fire call."
    return None


def _fan_tail(root: Path, reply: Mapping[str, Any], fired_run: str) -> str:
    """The allow reason's tail for an asks submission: the frozen reply and the other ready runs' fire calls."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import FanoutReply

    parent = _read_parent(root, str(reply["parent_id"]))
    runs = parent.runs if parent else ()
    frozen = FanoutReply(parent_id=str(reply["parent_id"]), runs=runs).to_json()
    others = [
        fire_command(r.slice_sizing) for r in runs if r.run_id != fired_run and r.status == "ready" and r.slice_sizing
    ]
    parts = [f"Reply: {json.dumps(frozen)}."]
    if others:
        parts.append("Fire the listed runs now, one call each, this turn: " + " ".join(others))
    held = [r.run_id for r in runs if r.status in ("after", "queued")]
    if held:
        parts.append(f"Held runs ({', '.join(held)}) are named by the close of the run that frees them.")
    parts.append("Each run's completion is its own wake.")
    return " ".join(parts)


def _default_fanout(params: dict, repo_root: Path) -> dict:
    from coordinator_core.ops.dispatch_emit.ask_fanout import fanout

    return fanout(params, repo_root)


def _refuse(text: str) -> dict:
    return deny("PreToolUse", f"{text}\nShape: Workflow({{name: \"{SUBMIT_WORKFLOW_NAME}\", {_ARGS_SHAPE}}}).")


def evaluate(
    payload: Mapping[str, Any], *, emit: Optional[EmitFn] = None, fanout: Optional[FanoutFn] = None
) -> dict:
    """The PreToolUse envelope for a Workflow call: the fire rewrite for a submission, else nothing."""
    if not is_submission(payload):
        return no_advisory()
    if payload.get("agent_id"):
        return deny("PreToolUse", "Only the EM submits an ask to the engine; return it in your report.")

    ask, sizing_rel, repo_arg, source = _request(payload["tool_input"])
    asks, asks_refusal = _asks(payload["tool_input"])
    if asks_refusal:
        return _refuse(asks_refusal)
    if sum(x is not None for x in (ask, sizing_rel, asks)) != 1:
        return _refuse("Submit exactly one of ask, sizing, asks.")
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_SOURCE_RELAYED, ASK_SOURCES

    if source is not None:
        if source not in ASK_SOURCES:
            return _refuse(f"source {source!r} is not one of: {', '.join(ASK_SOURCES)}.")
        if sizing_rel is not None:
            return _refuse("source marks an ask's text; a sizing submission carries none.")
    if asks is not None:
        asks = [
            {**item, "source": item.get("source", source)} if item.get("source", source) is not None else dict(item)
            for item in asks
        ]
        if len(asks) == 1:
            ask, source, asks = asks[0]["ask"], asks[0].get("source"), None

    brief, brief_refusal = _brief_params(payload["tool_input"])
    if brief_refusal:
        return _refuse(brief_refusal)
    if asks is not None:
        if "ask_brief" in brief or "ask_brief_file" in brief:
            return _refuse("A brief rides one run, not a set of sibling runs. Put each run's directives in its ask text.")

    root = _repo_root(repo_arg, payload.get("cwd"))
    if root is None:
        where = repo_arg or payload.get("cwd") or "the session cwd"
        return _refuse(f"{where} is in no git repo. Add `repo: \"<abs path>\"` to args.")

    if (ask is not None or asks is not None) and repo_arg is None and brief.get("ask_context"):
        root = _context_home(root, brief["ask_context"])
    missing = _missing_context(root, brief.get("ask_context", []))
    if missing:
        return _refuse(f"context {missing}: no such file. Pass inline prose as brief or context text, or fix the path.")

    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None

    from coordinator_core import warp_live

    fan_reply: Optional[dict] = None
    reserved_run_id: Optional[str] = None
    if asks is not None:
        fan_params: dict = {"asks": asks, "session_id": session_id or ""}
        if payload["tool_input"]["args"].get("confirm_split") is True:
            fan_params["confirm_split"] = True
        try:
            fan_reply = (fanout or _default_fanout)(fan_params, root)
        except Exception as exc:  # the engine's refusal text is the whole message
            return deny("PreToolUse", str(exc))
        parent = _read_parent(root, str(fan_reply.get("parent_id"))) if isinstance(fan_reply, dict) else None
        first = next((r for r in (parent.runs if parent else ()) if r.status == "ready"), None)
        if first is None:
            return deny("PreToolUse", "The fan-out produced no ready run to fire.")
        sizing_rel, reserved_run_id = first.slice_sizing, first.run_id

    sizing_doc = _load_sizing_doc(root, sizing_rel) if sizing_rel else {}
    if sizing_rel and asks is None:
        from coordinator_core.ops.dispatch_emit.ask_fanout_contract import parse_ask_parent

        marker = parse_ask_parent((sizing_doc.get("em_analysis") or {}).get("ask_parent"))
        if marker is not None:
            why = _slice_deny(root, marker)
            if why:
                return deny("PreToolUse", why)
            reserved_run_id = marker[1]

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
    if sizing_rel and _has_research_block(sizing_doc):
        if "ask_brief" in brief or "ask_brief_file" in brief:
            return _refuse(
                "A research sizing takes no brief or inline-prose context: the research route has no brief slot. "
                "Pass context file paths (context: [paths]) instead."
            )
        params["from_sizing"] = sizing_rel
        if brief.get("ask_context"):
            params["context"] = brief["ask_context"]
    else:
        if sizing_rel:
            params.update({"ask": True, "sizing_path": sizing_rel})
            if reserved_run_id:
                params["reserved_run_id"] = reserved_run_id
            # An XS sizing carries no footprint; a resubmission (applying review findings) names it.
            args = payload["tool_input"].get("args")
            writes = args.get("writes") if isinstance(args, dict) else None
            if isinstance(writes, list) and writes and all(isinstance(w, str) and w.strip() for w in writes):
                params["writes"] = [w.strip() for w in writes]
        else:
            params["ask"] = ask
            if source == ASK_SOURCE_RELAYED:
                params["ask_source"] = ASK_SOURCE_RELAYED
        params.update(brief)
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
    inherited = result.get("revisions_inherited")
    carries = (
        " Carries revisions: " + "; ".join(f"{r['id']} ({r['first_line']})" for r in inherited) + "."
        if inherited
        else ""
    )
    if fan_reply is not None:
        tail = _fan_tail(root, fan_reply, str(reserved_run_id))
    else:
        tail = (
            "It runs to its commit in the background; its completion notification is your one wake. "
            "End the turn: no polling, dispatch or verification meanwhile."
        )
    envelope = rewrite_input(
        "PreToolUse",
        new_input,
        f"Submitted to the engine as run {run_id}.{carries} {tail}",
    )
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = f"Engine submission: emitted {Path(script).name} this call."
    return envelope


def sizing_scout_deny(payload: Mapping[str, Any]) -> Optional[dict]:
    """The leaf's deny, resolving the payload's own subagent type (the Agent leg passes its own)."""
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") != "Agent" or not isinstance(tool_input, dict):
        return None
    from coordinator_core.hooks.block_unenumerated_agent_type import resolve_subagent_type

    return _scout_deny(payload, resolve_subagent_type(tool_input))


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
