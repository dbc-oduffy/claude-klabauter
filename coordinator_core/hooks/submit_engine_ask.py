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

`args` shape: `{ask: str, em_context?, route_preference?, em_rationale?, stop_after?, chatty?}` (`chatty` is a boolean, forwarded to the emit op as params["chatty"]; a non-boolean is refused) | `{sizing: str, stop_after?}` | `{asks: [{ask, source?, sender?, relayed_text?, em_context?, context?, route_preference?, em_rationale?, label?, writes?, stop_after?}], sender?, confirm_split?, stop_after?}` (`stop_after` is `"plan"` only: the run parks after planning for `/mise-prep`; a top-level value beside `asks` applies to every entry naming none; an `args` key outside the accepted set, or an `asks` entry key outside `ASKS_ITEM_KEYS`, is refused naming it and the accepted keys; `em_context` is a string of the EM's own context, carried in the emit op's `ask_brief` under an EM marker, never merged into the ask text, so `source: "pm"` stays true for the verbatim `ask`; an entry's `context` (a string or list of path / prose strings) and `em_context` ride the fan-out items on a multi-item `asks`, and a one-element `asks` carries them as a top-level `context` does; a top-level `context` beside a multi-item `asks` is refused; `route_preference` is goal|roadmap|plan, `em_rationale` at most 2000 chars; both forward to the emit op
or the fan-out, and either beside `{sizing}` is refused) (each ask a
separate sibling run via `dispatch.ask_fanout`; with two or more ready runs the call is rewritten to one
lazy-emit launcher (`fan_launcher.write_launcher`) that emits and starts each, with one ready run it fires
that run directly; the allow reason carries `{parent_id, runs}` and the runs waiting; a one-element array is an `ask`; a
slice `{sizing}` is denied until its `after` predecessors land), optional `repo` (absolute path; else the payload
cwd's repo, or for an ask whose absolute `context` lies in another registered repo, that repo; a run root foreign to the session's repo is refused unless the sizing takes the research route, `cross_repo_approved: true` is set, the venue is remote, or an explicit absolute `repo` names a registered checkout), optional `source: "pm" | "relayed"` (a chosen source is forwarded to the engine verbatim as `ask_source`, and ignored on a `sizing` submission; an unmarked single `ask` defaults to `pm` on the session's first human turn and to `relayed` with one warning in the allow reason on any later turn; only `source: "pm"` makes it the PM's
words, which the engine verifies against the session's recorded turns (refused when contradicted, flagged
when no log resolves); on `asks` it is the default for items naming no `source` of their own; a redirected
root passes `session_root`, the submitting session's repo, so the log is found for that verification), optional EM brief beside either arm
(`brief` inline text, `brief_file` repo path, `context` path or list of paths; carried to the emit
op's `ask_brief` / `ask_brief_file` / `ask_context`, whose refusal is the deny reason), optional `cross_repo_approved: true` (the
PM's local cross-repo assent; implied on a remote venue), optional `supersedes: "<held run id>"` (on `ask` / `sizing`
only: the new run replaces that held slice, which is marked superseded after the emit and never starts; refused
for a landed or running run). A bare string `args` is the ask.
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

import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from coordinator_core.hooks._envelope import deny, no_advisory, payload_of, rewrite_input
from coordinator_core.hooks.sizing_scout_deny import (
    ARGS_SHAPE,
    GUARD_NAME,
    SIZING_SCOUT_TYPES,
    sizing_scout_deny as _scout_deny,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import ASKS_ITEM_KEYS, unknown_keys_refusal
from coordinator_core.ops.sizing_intake_contract import is_path_shaped, with_em_context
from coordinator_core.warp_fire import SUBMIT_WORKFLOW_NAME, is_submit_name

# Every `args` key this hook reads; a key outside it is refused by name. A key read below must be added here.
_ARGS_KEYS: tuple[str, ...] = (
    "ask", "asks", "brief", "brief_file", "chatty", "confirm_split", "context", "cross_repo_approved", "em_context",
    "em_rationale", "repo", "route_preference", "sender", "sizing", "source", "stop_after", "supersedes",
    "writes",
)


_ARGS_SHAPE = ARGS_SHAPE.replace(
    "{ask: \"<words>\"}", "{ask: \"<words>\", route_preference?, em_rationale?}"
).replace("{ask, source?}", "{ask, source?, route_preference?, em_rationale?}")

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


def _is_first_turn(payload: Mapping[str, Any]) -> bool:
    """True when the session transcript holds exactly one human turn; unreadable reads False.

    Reads the transcript file in-process and stops at the second human turn.
    """
    import json

    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return False
    turns = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "user" or rec.get("isMeta"):
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                ):
                    continue
                turns += 1
                if turns > 1:
                    return False
    except OSError:
        return False
    return turns == 1


def _call_preference(
    tool_input: Mapping[str, Any], sizing_rel: Optional[str], asks_present: bool
) -> tuple[Any, Any, Optional[str]]:
    """(route_preference, em_rationale, refusal) from the call-level `args` of an `{ask}` submission."""
    args = tool_input.get("args")
    if not isinstance(args, dict):
        return None, None, None
    pref, rationale = args.get("route_preference"), args.get("em_rationale")
    if pref is None and rationale is None:
        return None, None, None
    if sizing_rel is not None:
        return None, None, "route_preference rides an ask; a sizing is already sized."
    if asks_present:
        return None, None, "route_preference and em_rationale ride each asks item, not the call."
    from coordinator_core.ops.dispatch_emit.route_preference import normalize, refusal

    pref, rationale = normalize(pref, rationale)
    return pref, rationale, refusal("route_preference", pref, rationale)


_CHATTY_KEY = "chatty"


def _chatty_refusal(where: str, value: Any) -> Optional[str]:
    """Refusal text for a non-boolean `chatty`; None when absent or a bool."""
    if value is None or isinstance(value, bool):
        return None
    return f"{where}.{_CHATTY_KEY} must be true or false, got {value!r}."


def _stop_after_refusal(where: str, value: Any) -> Optional[str]:
    """Refusal text for a `stop_after` value other than the one accepted; None when absent or `plan`."""
    from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_KEY, STOP_AFTER_PLAN

    if value is None or value == STOP_AFTER_PLAN:
        return None
    return f'{where}.{STOP_AFTER_KEY} must be "{STOP_AFTER_PLAN}" (the only accepted value), got {value!r}.'


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
    em_context = args.get("em_context")
    if em_context is not None and not isinstance(em_context, str):
        return {}, "em_context must be a string."
    if isinstance(em_context, str) and em_context.strip():
        params["ask_brief"] = with_em_context(params.get("ask_brief"), em_context)
    context = args.get("context")
    if context is not None:
        items, bad = _context_items(context, "context")
        if bad:
            return {}, bad
        _add_context(params, items)
    return params, None


def _context_items(context: Any, where: str) -> tuple[list[str], Optional[str]]:
    """(items, refusal): `context` is a string (a path or inline prose) or a list of them."""
    items = [context] if isinstance(context, str) else context
    if not isinstance(items, list) or not all(isinstance(c, str) for c in items):
        return [], f"{where} must be a string (a path or inline prose) or a list of them."
    return items, None


def _add_context(params: dict, items: list[str]) -> None:
    """Path-shaped items join `ask_context`; prose items are appended to `ask_brief`."""
    paths = [c.strip() for c in items if is_path_shaped(c)]
    prose = [c.strip() for c in items if c.strip() and not is_path_shaped(c)]
    if paths:
        params["ask_context"] = [*params.get("ask_context", []), *paths]
    if prose:
        params["ask_brief"] = "\n\n".join(([params["ask_brief"]] if "ask_brief" in params else []) + prose)


def _missing_context(root: Path, paths: list[str]) -> Optional[str]:
    """The first path-shaped context that names no existing file (relative ones against `root`)."""
    for raw in paths:
        p = Path(raw.replace("\\", "/"))
        if not (p if p.is_absolute() else root / p).is_file():
            return raw
    return None


def _repo_root(repo_arg: Optional[str], payload: dict) -> Optional[Path]:
    """Root for the submission: absolute `repo`, else the session anchor's repo, else payload cwd.

    A relative `repo` joins against the anchor; shell cwd is read only when no anchor resolves.
    """
    from coordinator_core.git.repo_root import show_toplevel

    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    if repo_arg and Path(repo_arg).is_absolute():
        top = show_toplevel(repo_arg)
        return Path(top) if top else None

    from coordinator_core.bash_guards._write_bump_applicability import resolve_launch_anchor

    session_id = payload.get("session_id")
    env = payload.get("env") if isinstance(payload.get("env"), dict) else None
    anchor = None
    if isinstance(session_id, str) and session_id:
        if env and env.get("CLAUDE_PROJECT_DIR"):
            anchor = resolve_launch_anchor(session_id, cwd, {**os.environ, **env})
        else:
            anchor = resolve_launch_anchor(session_id, cwd)
    base = anchor or cwd
    start = str(Path(base) / repo_arg) if repo_arg and base else (repo_arg or base)
    top = show_toplevel(start)
    return Path(top) if top else None


def _anchor_disagreement(payload: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    """`(anchor, launch)` paths when the session's anchor repo and launch repo differ, else None."""
    from coordinator_core.bash_guards._write_bump_applicability import launch_anchor_disagreement

    session_id = payload.get("session_id")
    if not (isinstance(session_id, str) and session_id):
        return None
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    env = payload.get("env") if isinstance(payload.get("env"), dict) else None
    if env and env.get("CLAUDE_PROJECT_DIR"):
        env = {**os.environ, **env}
    return launch_anchor_disagreement(session_id, cwd, env)


def _cross_repo_refusal(run_root: Path, own: Optional[Path], research_route: bool, approved: bool) -> Optional[str]:
    """Deny text when the run root is another checkout and no research sizing, approval or remote venue admits it; else None."""
    from coordinator_core.ops.dispatch_emit.cross_repo_ask_refusal import cross_repo_run_refusal

    return cross_repo_run_refusal(run_root, own, research_route=research_route, approved=approved)


def _names_registered_sibling(repo_arg: Optional[str], root: Path) -> bool:
    """True when an explicit absolute `repo` resolved to a checkout in the machine's repo registry."""
    if not (repo_arg and Path(repo_arg).is_absolute()):
        return False
    from coordinator_core.bash_guards._write_bump_applicability import target_is_registered_repo

    return target_is_registered_repo(str(root))


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
        return None, f"asks must be a non-empty list of {{{', '.join(ASKS_ITEM_KEYS)}}}."
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_SOURCES
    from coordinator_core.ops.dispatch_emit.route_preference import normalize as normalize_route
    from coordinator_core.ops.dispatch_emit.route_preference import refusal as route_refusal

    asks = list(asks)
    if len(asks) > 1 and args.get("context") is not None:
        return None, "context rides each asks item (asks[i].context), not the call."
    if args.get("sender") is not None and not isinstance(args["sender"], str):
        return None, "sender must be a string."
    for i, item in enumerate(asks):
        if not isinstance(item, dict) or not isinstance(item.get("ask"), str) or not item["ask"].strip():
            return None, f"asks[{i}] carries no ask text."
        unknown = unknown_keys_refusal(f"asks[{i}]", item, ASKS_ITEM_KEYS + (_CHATTY_KEY,))
        if unknown:
            return None, unknown
        if item.get(_CHATTY_KEY) is not None:
            chatty_why = _chatty_refusal(f"asks[{i}]", item[_CHATTY_KEY])
            if chatty_why:
                return None, chatty_why
            if len(asks) > 1:
                return None, "chatty is not carried by a multi-item asks fan-out; submit one ask per chatty run."
        if item.get("context") is not None:
            _, bad = _context_items(item["context"], f"asks[{i}].context")
            if bad:
                return None, bad
        item_source = item.get("source")
        if item_source is not None and item_source not in ASK_SOURCES:
            return None, f"asks[{i}].source must be one of: {', '.join(ASK_SOURCES)}."
        sender = item.get("sender")
        if sender is not None and not isinstance(sender, str):
            return None, f"asks[{i}].sender must be a string."
        relayed_text = item.get("relayed_text")
        if relayed_text is not None and not isinstance(relayed_text, str):
            return None, f"asks[{i}].relayed_text must be a string."
        if item.get("route_preference") is not None or item.get("em_rationale") is not None:
            pref, why = normalize_route(item.get("route_preference"), item.get("em_rationale"))
            route_why = route_refusal(f"asks[{i}]", pref, why)
            if route_why:
                return None, route_why
            item = {k: v for k, v in item.items() if k not in ("route_preference", "em_rationale")}
            item.update({k: v for k, v in (("route_preference", pref), ("em_rationale", why)) if v is not None})
            asks[i] = item
        if item.get("em_context") is not None and not isinstance(item["em_context"], str):
            return None, f"asks[{i}].em_context must be a string."
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            return None, f"asks[{i}].label must be a string."
        writes = item.get("writes")
        if writes is not None and not (isinstance(writes, list) and all(isinstance(w, str) for w in writes)):
            return None, f"asks[{i}].writes must be a list of repo-relative paths."
        stop_why = _stop_after_refusal(f"asks[{i}]", item.get("stop_after"))
        if stop_why:
            return None, stop_why
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
    if status == "running":
        from coordinator_core.ops.dispatch_emit.sibling_hold import reconcile_running

        if reconcile_running(root, run_id):
            return "landed"
    return status if isinstance(status, str) else None


def _rows_committed(root: Path, run_id: str) -> bool:
    """True when a halted run's status file carries the all-rows-committed mark."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import status_rel
    from coordinator_core.ops.dispatch_emit.run_close import ROWS_COMMITTED_KEY

    try:
        data = json.loads((root / status_rel(run_id)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get(ROWS_COMMITTED_KEY) is True


def _read_parent(root: Path, parent_id: str):
    import json

    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import ParentRecord, parent_record_rel

    try:
        return ParentRecord.from_json(json.loads((root / parent_record_rel(parent_id)).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _slice_deny(root: Path, marker: tuple[str, str]) -> Optional[str]:
    """Why a slice sizing may not start now: an unlanded `after` predecessor."""
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import (
        STATUS_HALTED, STATUS_HELD, STATUS_LANDED,
    )

    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import read_superseded_by
    from coordinator_core.ops.dispatch_emit.sibling_hold import predecessor_closed

    parent_id, run_id = marker
    by = read_superseded_by(root, run_id)
    if by:
        return f"Run {run_id} is superseded by {by}; it does not start."
    parent = _read_parent(root, parent_id)
    mine = next((r for r in (parent.runs if parent else ()) if r.run_id == run_id), None)
    if mine is None:
        return f"Run {run_id} is not in parent {parent_id}'s record. Submit the ask as a fresh {{ask}}."
    if mine.status == STATUS_HELD:
        return (
            f"Run {run_id} is held: {mine.hold_reason}. "
            f'Resubmit as a fresh {{ask, supersedes: "{run_id}"}} once that blocker lands.'
        )
    for pred in mine.after:
        state = _status_of(root, pred)
        if state == STATUS_LANDED:
            continue
        if state == STATUS_HALTED:
            if _rows_committed(root, pred) or predecessor_closed(root, pred):
                continue
            return (
                f"Predecessor {pred} halted, so {run_id} cannot start. "
                f"If its work landed, record it: dispatch.record_halted_run_landed "
                f"{{run_id: {pred}, sha}}; else resubmit the slice as a "
                f'fresh {{ask, supersedes: "{run_id}"}}.'
            )
        return f"Predecessor {pred} has not landed ({state or 'not started'}); its close names this run's fire call."
    return None


def _supersedes(tool_input: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(held run id, refusal) from `args.supersedes`; valid on `{ask}` / `{sizing}` only."""
    args = tool_input.get("args")
    if not isinstance(args, dict) or "supersedes" not in args:
        return None, None
    value = args["supersedes"]
    if "asks" in args:
        return None, "supersedes rides an {ask} or {sizing} submission, not asks."
    if not isinstance(value, str) or not value.strip():
        return None, "supersedes must be the held run id (a string)."
    return value.strip(), None


def _revise_hint(run_id: str) -> str:
    """The call shape for a mid-run PM revision of `run_id`."""
    return (
        "Mid-run PM revision: "
        f"coordinator-invoke workflow.revise '{{\"run_id\": \"{run_id}\", \"verbatim\": \"<requirement>\"}}'"
        " (optional: from, all_of_pm_ask; full contract: --describe)."
    )


def _fan_tail(root: Path, reply: Mapping[str, Any], started: Sequence[str], launcher: bool = False) -> str:
    """The allow reason's tail for an asks submission: the frozen reply, the runs started, the runs waiting."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import FanoutReply

    parent = _read_parent(root, str(reply["parent_id"]))
    runs = parent.runs if parent else ()
    frozen = FanoutReply(parent_id=str(reply["parent_id"]), runs=runs).to_json()
    parts = [f"Reply: {json.dumps(frozen)}.", f"Started: {', '.join(started)}."]
    waiting = [f"{r.run_id} ({r.status})" for r in runs if r.run_id not in started and r.status != "ready"]
    if waiting:
        parts.append(
            f"Waiting ({', '.join(waiting)}): a run waiting on a predecessor is named by the close of the run that frees it."
        )
    if launcher:
        parts.append(
            "The launcher's completion is your one wake; it carries each run's result, and an emit "
            "refused at a child's start is named there with its reason."
        )
    else:
        parts.append("Each run's completion is its own wake.")
    return " ".join(parts)


def _default_launch(root: Path, parent_id: str, children: Sequence[Any], session_id: Optional[str]) -> Path:
    from coordinator_core.ops.dispatch_emit.fan_launcher import write_launcher

    return write_launcher(root, parent_id, children, session_id)


def _default_fanout(params: dict, repo_root: Path) -> dict:
    from coordinator_core.ops.dispatch_emit.ask_fanout import fanout

    return fanout(params, repo_root)


def _declared_writes(root: Path, tool_input: Mapping[str, Any], sizing_doc: Mapping[str, Any]) -> list[str]:
    """`args.writes`, else the cited sizing's plan-spine writes; empty when neither is declared."""
    args = tool_input.get("args")
    writes = args.get("writes") if isinstance(args, dict) else None
    if isinstance(writes, list) and writes and all(isinstance(w, str) and w.strip() for w in writes):
        return [w.strip() for w in writes]
    plan_rel = sizing_doc.get("plan")
    if not isinstance(plan_rel, str) or not plan_rel.strip():
        return []
    from coordinator_core.ops.dispatch_emit.cross_plan_write_overlap import _declared_write_paths

    plans = (root / "docs" / "plans").resolve()
    plan = (root / plan_rel.strip()).resolve()
    if plans not in plan.parents or not plan.is_file():
        return []
    return sorted(_declared_write_paths(plan))


def _live_write_overlap_deny(root: Path, tool_input: Mapping[str, Any], sizing_doc: Mapping[str, Any]) -> Optional[str]:
    """Deny text when a single ask's declared writes overlap a live run's staged footprint; else None."""
    writes = _declared_writes(root, tool_input, sizing_doc)
    if not writes:
        return None
    from coordinator_core.ops.dispatch_emit import live_run_writes

    try:
        hits = live_run_writes.overlaps(writes, live_run_writes.live_footprints(root, exclude_run_id=None))
    except OSError:
        return None
    if not hits:
        return None
    run_id, paths = next(iter(sorted(hits.items())))
    shown = ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")
    return f"Run {run_id} is live and writes {shown}, which this ask also writes. Resubmit when {run_id} lands."


_RESEARCH_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+?)(?:\.md)?$")
_RESEARCH_STOPWORDS = frozenset(
    "the and for with that this from into what how why does did are was were can should would could "
    "about when which where who whom your our its their not any all has have had".split()
)
_FUZZY_MIN_SHARED = 3
_FUZZY_MIN_COVERAGE = 0.6


def _slug_terms(slug: str) -> frozenset[str]:
    return frozenset(t for t in slug.split("-") if len(t) >= 3 and t not in _RESEARCH_STOPWORDS)


def _existing_research(root: Path, ask: Optional[str], sizing_rel: Optional[str]) -> list[str]:
    """Repo-relative `docs/research/` entries (archived `<date>-<topic>/` dirs or flat `<date>-<name>.md` files) for this submission's topic.

    One directory listing under `docs/research`, matched by name: the topic slug exactly, or at least
    three shared slug terms covering most of the smaller side (a differently worded ask). Never reads
    content, never spawns.
    """
    from coordinator_core.ops.dispatch_emit.research_emit import _TOPIC_FALLBACK, topic_slug

    slug = topic_slug(ask or "", sizing_rel or "")
    if slug == _TOPIC_FALLBACK:
        return []
    want = _slug_terms(slug)
    found = []
    try:
        for p in (root / "docs" / "research").iterdir():
            m = _RESEARCH_NAME_RE.match(p.name)
            if not m or not (p.is_dir() or p.suffix == ".md"):
                continue
            name = m.group(1)
            have = _slug_terms(name)
            shared = len(want & have)
            if name == slug or (
                shared >= _FUZZY_MIN_SHARED and shared / min(len(want), len(have)) >= _FUZZY_MIN_COVERAGE
            ):
                found.append(f"docs/research/{p.name}{'/' if p.is_dir() else ''}")
    except OSError:
        return []
    return sorted(found)


def _refuse(text: str) -> dict:
    return deny("PreToolUse", f"{text}\nInstead: `Workflow({{name: \"{SUBMIT_WORKFLOW_NAME}\", {_ARGS_SHAPE}}})`.")


def _child_params(
    root: Path, tool_input: Mapping[str, Any], session_id: Optional[str], run: Any, call_stop: Any, approved: bool
) -> dict:
    """The `dispatch.emit` params the direct path sends for one fan-out slice; the launcher's emit leg sends these."""
    # `dispatch.emit` is repo-unkeyed over the CLI: without target_root a relative sizing_path
    # resolves against the engine's own root, not the submitting repo.
    params: dict = {"session_id": session_id or "", "target_root": str(root)}
    if _has_research_block(_load_sizing_doc(root, run.slice_sizing)):
        params["from_sizing"] = run.slice_sizing
    else:
        params.update({"ask": True, "sizing_path": run.slice_sizing, "reserved_run_id": run.run_id})
        args = tool_input.get("args")
        writes = args.get("writes") if isinstance(args, dict) else None
        if isinstance(writes, list) and writes and all(isinstance(w, str) and w.strip() for w in writes):
            params["writes"] = [w.strip() for w in writes]
        if call_stop is not None:
            params["stop_after"] = call_stop
        if isinstance(args, dict) and args.get(_CHATTY_KEY) is True:
            params[_CHATTY_KEY] = True
    if approved:
        params["cross_repo_approved"] = True
    return params


def _launch_all(
    root: Path,
    payload: Mapping[str, Any],
    fan_reply: Mapping[str, Any],
    ready: Sequence[Any],
    call_stop: Any,
    launch: Optional[Callable[..., Path]],
    approved: bool,
    sizing_root: Optional[Path] = None,
) -> dict:
    """The rewrite to one launcher script that emits and starts every ready run of an asks submission.

    `sizing_root` is the root the fan-out wrote the child sizings under; each child emit resolves its sizing there."""
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit.fan_launcher import LaunchChild

    tool_input = payload["tool_input"]
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None
    children = [
        LaunchChild(
            run_id=r.run_id,
            sizing_rel=r.slice_sizing,
            emit_params=_child_params(sizing_root or root, tool_input, session_id, r, call_stop, approved),
        )
        for r in ready
    ]
    parent_id = str(fan_reply["parent_id"])
    try:
        script = (launch or _default_launch)(root, parent_id, children, session_id)
    except Exception as exc:  # the launcher's refusal text is the whole message
        return _refuse(f"The engine wrote no launcher: {exc}")
    call_id = payload.get("tool_use_id")
    for r in ready:
        try:
            warp_live.record_submission(
                root,
                r.run_id,
                session_id=session_id,
                transcript_path=transcript,
                script_path=str(script),
                sizing_rel=r.slice_sizing,
                tool_use_id=call_id if isinstance(call_id, str) else None,
            )
        except OSError:
            pass  # liveness then reads the run token alone
    started = [r.run_id for r in ready]
    tail = _fan_tail(root, fan_reply, started, launcher=True)
    envelope = rewrite_input(
        "PreToolUse",
        {"scriptPath": str(script)},
        f"Submitted to the engine as runs {', '.join(started)} under one launcher. {tail} {_revise_hint(started[0])}",
    )
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = f"Engine submission: wrote {Path(script).name} this call."
    return envelope


def evaluate(
    payload: Mapping[str, Any],
    *,
    emit: Optional[EmitFn] = None,
    fanout: Optional[FanoutFn] = None,
    launch: Optional[Callable[..., Path]] = None,
) -> dict:
    """The PreToolUse envelope for a Workflow call: the fire rewrite for a submission, else nothing."""
    if not is_submission(payload):
        return no_advisory()
    if payload.get("agent_id"):
        return deny("PreToolUse", "Only the EM submits an ask to the engine; return it in your report.")

    call_args = payload["tool_input"].get("args")
    if isinstance(call_args, dict):
        unknown_arg = unknown_keys_refusal("args", call_args, _ARGS_KEYS)
        if unknown_arg:
            return _refuse(unknown_arg)
    ask, sizing_rel, repo_arg, source = _request(payload["tool_input"])
    sizing_submitted = sizing_rel is not None
    asks, asks_refusal = _asks(payload["tool_input"])
    if asks_refusal:
        return _refuse(asks_refusal)
    if sum(x is not None for x in (ask, sizing_rel, asks)) != 1:
        return _refuse("Submit exactly one of ask, sizing, asks.")
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_SOURCES

    route_pref, em_rationale, route_refusal = _call_preference(payload["tool_input"], sizing_rel, asks is not None)
    if route_refusal:
        return _refuse(route_refusal)
    call_stop = call_args.get("stop_after") if isinstance(call_args, dict) else None
    stop_why = _stop_after_refusal("args", call_stop)
    if stop_why:
        return _refuse(stop_why)
    call_chatty = call_args.get(_CHATTY_KEY) if isinstance(call_args, dict) else None
    chatty_why = _chatty_refusal("args", call_chatty)
    if chatty_why:
        return _refuse(chatty_why)
    if call_chatty is True and asks is not None and len(asks) > 1:
        return _refuse("chatty is not carried by a multi-item asks fan-out; submit one ask per chatty run.")
    if source is not None:
        if source not in ASK_SOURCES:
            return _refuse(f"source {source!r} is not one of: {', '.join(ASK_SOURCES)}.")
        if sizing_rel is not None:
            source = None
    item_em_context: Optional[str] = None
    item_context: list[str] = []
    if asks is not None:
        asks = [
            {**item, "source": item.get("source", source)} if item.get("source", source) is not None else dict(item)
            for item in asks
        ]
        if call_stop is not None:
            asks = [{**item, "stop_after": item.get("stop_after", call_stop)} for item in asks]
        if len(asks) == 1 and not (
            asks[0].get("sender") or asks[0].get("relayed_text") or payload["tool_input"]["args"].get("sender")
        ):
            ask, source = asks[0]["ask"], asks[0].get("source")
            route_pref, em_rationale = asks[0].get("route_preference"), asks[0].get("em_rationale")
            call_stop = asks[0].get("stop_after")
            if asks[0].get(_CHATTY_KEY) is not None:
                call_chatty = asks[0][_CHATTY_KEY]
            item_em_context = asks[0].get("em_context")
            item_context, _ = _context_items(asks[0].get("context") or [], "context")
            asks = None

    defaulted_pm = False
    defaulted_relayed = False
    if ask is not None and source is None:
        if _is_first_turn(payload):
            source, defaulted_pm = "pm", True
        else:
            source, defaulted_relayed = "relayed", True

    supersedes, supersedes_refusal = _supersedes(payload["tool_input"])
    if supersedes_refusal:
        return _refuse(supersedes_refusal)
    brief, brief_refusal = _brief_params(payload["tool_input"])
    if brief_refusal:
        return _refuse(brief_refusal)
    if isinstance(item_em_context, str) and item_em_context.strip():
        brief["ask_brief"] = with_em_context(brief.get("ask_brief"), item_em_context)
    _add_context(brief, item_context)
    if asks is not None:
        if "ask_brief" in brief or "ask_brief_file" in brief:
            return _refuse("A brief rides one run, not a set of sibling runs. Put each run's directives in its ask text.")

    disagree = None if (repo_arg and Path(repo_arg).is_absolute()) else _anchor_disagreement(payload)
    if disagree:
        return _refuse(
            f"Session anchor {disagree[0]} and launch repo {disagree[1]} are different repos; "
            "the run would resolve to the anchor. Pass an absolute `repo` to choose one."
        )

    root = _repo_root(repo_arg, payload)
    if root is None:
        where = repo_arg or payload.get("cwd") or "the session cwd"
        return _refuse(f"{where} is in no git repo. Add `repo: \"<abs path>\"` to args.")

    target_root = root
    if (ask is not None or asks is not None) and repo_arg is None and brief.get("ask_context"):
        root = _context_home(root, brief["ask_context"])
    session_root: Optional[str] = None
    own: Optional[Path] = None
    if (ask is not None or asks is not None) and (repo_arg is not None or root != target_root):
        own = _repo_root(None, payload) if repo_arg and Path(repo_arg).is_absolute() else target_root
        if own is not None and own.resolve() != root.resolve():
            session_root = str(own)
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import is_remote_venue

    approved = (
        is_remote_venue()
        or (isinstance(call_args, dict) and call_args.get("cross_repo_approved") is True)
        or _names_registered_sibling(repo_arg, root)
    )
    if own is not None:
        cross = _cross_repo_refusal(root, own, False, approved)
        if cross:
            return _refuse(cross)
    missing = _missing_context(root, brief.get("ask_context", []))
    if missing:
        return _refuse(f"context {missing}: no such file. Pass inline prose as brief or context text, or fix the path.")
    for i, item in enumerate(asks or ()):
        entry_paths = [c.strip() for c in _context_items(item.get("context") or [], "context")[0] if is_path_shaped(c)]
        missing = _missing_context(root, entry_paths)
        if missing:
            return _refuse(f"asks[{i}].context {missing}: no such file. Pass inline prose as context text, or fix the path.")

    if supersedes:
        from coordinator_core.ops.dispatch_emit.sibling_hold import supersede_refusal

        refusal = supersede_refusal(root, supersedes, "pending-supersessor")
        if refusal:
            return _refuse(refusal)

    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None

    from coordinator_core import warp_live

    fan_reply: Optional[dict] = None
    reserved_run_id: Optional[str] = None
    if asks is not None:
        fan_asks = [{k: v for k, v in item.items() if k != _CHATTY_KEY} for item in asks]
        fan_params: dict = {"asks": fan_asks, "session_id": session_id or ""}
        if session_root:
            fan_params["session_root"] = session_root
        if isinstance(payload["tool_input"]["args"].get("sender"), str):
            fan_params["sender"] = payload["tool_input"]["args"]["sender"]
        if payload["tool_input"]["args"].get("confirm_split") is True:
            fan_params["confirm_split"] = True
        try:
            fan_reply = (fanout or _default_fanout)(fan_params, root)
        except Exception as exc:  # the engine's refusal text is the whole message
            return deny("PreToolUse", str(exc))
        if not isinstance(fan_reply, dict):
            return deny("PreToolUse", f"The fan-out returned no reply: {fan_reply!r}.")
        if fan_reply.get("error") or fan_reply.get("exit_code") or fan_reply.get("applied") is False:
            return deny("PreToolUse", str(fan_reply.get("error") or "The fan-out was refused."))
        writer_root = Path(fan_reply["writer_root"]) if isinstance(fan_reply.get("writer_root"), str) else root
        parent = _read_parent(writer_root, str(fan_reply.get("parent_id")))
        ready = [r for r in (parent.runs if parent else ()) if r.status == "ready"]
        if not ready:
            return deny("PreToolUse", "The fan-out produced no ready run to fire.")
        unwritten = next((r.slice_sizing for r in ready if not (writer_root / r.slice_sizing).is_file()), None)
        if unwritten:
            return deny("PreToolUse", f"The fan-out reported {unwritten} written under {writer_root}, but it is not on disk there.")
        if len(ready) >= 2:
            return _launch_all(root, payload, fan_reply, ready, call_stop, launch, approved, writer_root)
        first = ready[0]
        # Trap: rebinding moves every later `root` use (cross-repo, live-run, overlap, emit target_root), not only sizing.
        root = writer_root
        sizing_rel, reserved_run_id = first.slice_sizing, first.run_id

    sizing_doc = _load_sizing_doc(root, sizing_rel) if sizing_rel else {}
    if sizing_submitted and repo_arg and Path(repo_arg).is_absolute():
        cross = _cross_repo_refusal(root, _repo_root(None, payload), _has_research_block(sizing_doc), approved)
        if cross:
            return _refuse(cross)
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

    if asks is None and reserved_run_id is None:
        clash = _live_write_overlap_deny(root, payload["tool_input"], sizing_doc)
        if clash:
            return deny("PreToolUse", clash)

    retire_note = ""
    if sizing_rel and reserved_run_id is None:
        from coordinator_core.ops.dispatch_emit.ask_gate import retire_token_spent_halt

        try:
            retire_token_spent_halt(root, sizing_rel)
        except (OSError, ValueError) as exc:  # non-blocking: a failed retire leaves the halted run resumable
            retire_note = f" Retiring the spent halt failed: {type(exc).__name__}: {exc}."

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
            if source is not None:
                params["ask_source"] = source
            if session_root:
                params["session_root"] = session_root
            if route_pref is not None:
                params["route_preference"] = route_pref
            if em_rationale is not None:
                params["em_rationale"] = em_rationale
        params.update(brief)
        if call_stop is not None:
            params["stop_after"] = call_stop
        if call_chatty is True:
            params[_CHATTY_KEY] = True
    if approved:
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

    marked = ""
    if supersedes:
        from coordinator_core.ops.dispatch_emit.sibling_hold import SupersedeRefused, mark_superseded

        try:
            mark_superseded(root, supersedes, str(run_id))
            marked = f" Marked {supersedes} superseded by {run_id}."
        except SupersedeRefused as exc:
            marked = f" Not marked superseded: {exc}"

    new_input: dict = {"scriptPath": str(script)}
    if result.get("fire_args"):
        new_input["args"] = result["fire_args"]
    inherited = result.get("revisions_inherited")
    carries = (
        " Carries revisions: "
        + "; ".join(
            f"{r['id']} (EM note: {r['first_line']})" if r.get("kind") == "note" else f"{r['id']} ({r['first_line']})"
            for r in inherited
        )
        + "."
        if inherited
        else ""
    )
    if fan_reply is not None:
        tail = _fan_tail(root, fan_reply, [str(reserved_run_id)])
    else:
        tail = (
            "It runs to its commit in the background; its completion notification is your one wake. "
            "End the turn: no polling, dispatch or verification meanwhile."
        )
    from coordinator_core.ops.dispatch_emit.ask_contract import OUTPUT_PATH_REDIRECT_KEY

    redirect = result.get(OUTPUT_PATH_REDIRECT_KEY)
    lead = (
        f"Halted run {redirect.get('halted_run')} belonged to {redirect.get('prior_session')} "
        f"({redirect.get('reason')}); emitted fresh at {redirect.get('to')}. "
        if isinstance(redirect, dict)
        else ""
    )
    derived = (
        " Unmarked on the session's first turn: defaulted to source pm (the engine verifies it)."
        if defaulted_pm
        else ""
    )
    if defaulted_relayed:
        derived += (
            " Warning: unmarked ask ran as source relayed (AI-derived). "
            "To mark the PM's verbatim: put it in `ask` with source unset or `source: \"pm\"`, "
            "and the elaboration in `context: [...]`."
        )
    if isinstance(result, dict) and result.get("pm_verification") == "unverifiable":
        derived += " source pm is unverified: no session turn log resolved."
    if asks is None and not (sizing_rel and _has_research_block(sizing_doc)):
        found = _existing_research(root, ask, sizing_rel)
        if found:
            derived += (
                f" Research artifacts for this ask already exist: {', '.join(found)}. "
                "Read them before the build reruns that work."
            )
    envelope = rewrite_input(
        "PreToolUse",
        new_input,
        f"{lead}Submitted to the engine as run {run_id}.{marked}{carries}{derived}{retire_note} {tail} {_revise_hint(str(run_id))}",
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

    try:
        envelope = evaluate(payload)
    except Exception as exc:
        envelope = deny("PreToolUse", f"The submit hook failed: {type(exc).__name__}: {exc}. Check for an existing run record before re-submitting.")
    return apply_guard_level(GUARD_NAME, envelope) or no_advisory()


__all__ = [
    "GUARD_NAME",
    "SIZING_SCOUT_TYPES",
    "evaluate",
    "is_submission",
    "sizing_scout_deny",
]
