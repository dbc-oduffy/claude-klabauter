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

`args` shape: `{ask: str, em_context?, route_preference?, em_rationale?, em_size?, stop_after?, chatty?}` (`em_size` is the EM's own size, XS|S|M|L|XL|XXL, forwarded to the emit op as params["em_size"]; the gate halts only when it is M or below against an engine XL/XXL; a value outside the set, or one beside a multi-item `asks`, is refused) (`chatty` is a boolean, forwarded to the emit op as params["chatty"]; a non-boolean is refused) | `{sizing: str, stop_after?}` | `{asks: [{ask, source?, sender?, relayed_text?, em_context?, context?, route_preference?, em_rationale?, label?, repo?, writes?, after?, stop_after?}], sender?, confirm_split?, stop_after?}` (`stop_after` is `"plan"` only: the run parks after planning for `/mise-prep`; a top-level value beside `asks` applies to every entry naming none; an entry's `repo` (else the top-level `repo`) is the checkout its run starts in and writes to: entries are partitioned by resolved repo, each repo is cross-repo-checked and fanned out once, and the runs start under one launcher per repo; an `after` edge or a `join` across repos is refused; an `args` key outside the accepted set, or an `asks` entry key outside `ASKS_ITEM_KEYS`, is refused naming it and the accepted keys; `em_context` is a string of the EM's own context, carried in the emit op's `ask_brief` under an EM marker, never merged into the ask text, so `source: "pm"` stays true for the verbatim `ask`; an entry's `context` (a string or list of path / prose strings) and `em_context` ride the fan-out items on a multi-item `asks`, and a one-element `asks` carries them as a top-level `context` does; a top-level `context` beside a multi-item `asks` is refused; `route_preference` is goal|roadmap|plan, `em_rationale` at most 2000 chars; both forward to the emit op
or the fan-out, and either beside `{sizing}` is refused) (each ask a
separate sibling run via `dispatch.ask_fanout`; with two or more ready runs the call is rewritten to one
launcher (`fan_launcher.write_launcher`, given each run's pre-emitted reply) that starts each, with one ready run it fires
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

A refire of a halted sizing reads the halt-time record (`resume.json`, `halt_resume_record`),
checks its fingerprint (file bytes and the halt marker; no git) and emits from the stored
script. A missing, stale or stalled record spawns `halt_resume_build` detached and starts the run
through a one-child launcher; the hook never composes. The overlap check reads the live-run index
(`live_run_index`), never the warp dirs. Every such read is deadline-bound (`_bounded_call`): a
stalled disk degrades to the background path and never blocks the run start.

Negative spec: never launches a process except the detached recompose — the harness runs the
rewritten call. Never edits the emitted script. Never denies a Workflow call that does not name
the stub.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import os
import re
import threading
import uuid
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
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import (
    ASKS_ITEM_KEYS,
    resolve_after,
    unknown_keys_refusal,
)
from coordinator_core.ops.dispatch_emit.extant_work import normalized_ask_hash
from coordinator_core.ops.sizing_intake_contract import (
    EM_CONTEXT_DIGEST_PARAM,
    em_context_digest,
    is_path_shaped,
    with_em_context,
)
from coordinator_core.warp_fire import SUBMIT_WORKFLOW_NAME, is_submit_name

# Every `args` key this hook reads; a key outside it is refused by name. A key read below must be added here.
_ARGS_KEYS: tuple[str, ...] = (
    "ask", "asks", "brief", "brief_file", "chatty", "confirm_split", "context", "cross_repo_approved", "em_context",
    "em_rationale", "em_size", "join", "repo", "resume_run_id", "route_preference", "sender", "sizing", "source",
    "stop_after", "supersedes", "writes",
)
_INPUT_KEYS: tuple[str, ...] = ("run_id", "repo")
_HOOK_ITEM_KEYS: tuple[str, ...] = tuple(dict.fromkeys((*ASKS_ITEM_KEYS, "repo", "chatty")))


def _em_size_param(tool_input: Mapping[str, Any], multi_ask: bool) -> tuple[Optional[str], Optional[str]]:
    """(normalised em_size or None, refusal) for the EM's optional `em_size` arg."""
    args = tool_input.get("args")
    value = args.get("em_size") if isinstance(args, dict) else None
    if value is None:
        return None, None
    from coordinator_core.ops.dispatch_emit.ask_contract import VALID_SIZES

    size = value.strip().upper() if isinstance(value, str) else None
    if size not in VALID_SIZES:
        return None, f"em_size must be one of {', '.join(VALID_SIZES)}, got {value!r}."
    if multi_ask:
        return None, "em_size rides one run, not a set of sibling runs. Submit each ask on its own."
    return size, None


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


def _resume_run_id(tool_input: Mapping[str, Any]) -> Optional[str]:
    """The halted run id a refire's `args.resume_run_id` names, else None."""
    args = tool_input.get("args")
    value = args.get("resume_run_id") if isinstance(args, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_peer_delivery(content: Any) -> bool:
    """True when a user record's text opens with the `<cross-session-message` peer wrapper."""
    if isinstance(content, list):
        content = next(
            (b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"), None
        )
    return isinstance(content, str) and content.lstrip().startswith("<cross-session-message")


def _is_first_turn(payload: Mapping[str, Any]) -> bool:
    """True when the session transcript holds exactly one human turn; unreadable reads False.

    A peer-delivered `<cross-session-message` record is not a human turn.

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
                if _is_peer_delivery(content):
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
    context = args.get("context")
    if context is not None:
        items, bad = _context_items(context, "context")
        if bad:
            return {}, bad
        _add_context(params, items)
    if isinstance(em_context, str) and em_context.strip():
        # Trap: the EM block stays last in `ask_brief`, so the text after its marker is exactly what the digest names.
        params["ask_brief"] = with_em_context(params.get("ask_brief"), em_context)
        params[EM_CONTEXT_DIGEST_PARAM] = em_context_digest(em_context)
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


_RESOLVED_MEMO_STATUSES = frozenset({"actioned", "closed"})
_STATUS_LINE = re.compile(r"^status:[ \t]*[\"']?([A-Za-z_-]+)", re.MULTILINE)
_COMMIT_LINE = re.compile(r"^(?:actioned_commit|commit|sha):[ \t]*[\"']?([0-9a-fA-F]{7,40})\b", re.MULTILINE)
_NOTE_LINE = re.compile(r"^actioned_note:(.*)$", re.MULTILINE)
_NOTE_SHA = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")


def _memo_commit(front: str) -> str:
    """The commit that actioned a memo: its commit key, else a sha inside `actioned_note`; "no commit recorded" otherwise."""
    found = _COMMIT_LINE.search(front)
    if found:
        return f"commit {found.group(1)}"
    note = _NOTE_LINE.search(front)
    sha = _NOTE_SHA.search(note.group(1)) if note else None
    return f"commit {sha.group(0)}" if sha else "no commit recorded"


def _resolved_memos(root: Path, paths: list[str]) -> list[str]:
    """`<memo> is <status> (<commit>)` per context under `state/cross-repo/{inbox,archive}` whose frontmatter status is resolved.

    Feeds an advisory only: a re-sent actioned memo is duplicate work, never a deny."""
    notes: list[str] = []
    for raw in paths:
        p = Path(raw.replace("\\", "/"))
        parts = p.parts
        if not any(a == "cross-repo" and b in ("inbox", "archive") for a, b in zip(parts, parts[1:])):
            continue
        try:
            with open(p if p.is_absolute() else root / p, encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        if not head.startswith("---"):
            continue
        front = head[3:].split("\n---", 1)[0]
        found = _STATUS_LINE.search(front)
        if found and found.group(1).lower() in _RESOLVED_MEMO_STATUSES:
            notes.append(f"{p.name} is {found.group(1).lower()} ({_memo_commit(front)})")
    return notes


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
    if repo_arg and not Path(start).exists():
        return None
    top = show_toplevel(start)
    return Path(top) if top else None


_REGISTRY_REPO_PREFIXES = ("repos.", "engine.working_repos.")


def _registry_repo(repo: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(repo, refusal): a `repos.<key>` / `engine.working_repos.<key>` value becomes its registered absolute path
    through the single-key registry resolver; an unknown key is refused naming it; any other value passes through."""
    if not (isinstance(repo, str) and repo.strip().startswith(_REGISTRY_REPO_PREFIXES)):
        return repo, None
    from coordinator_core import machine_resolver

    key = repo.strip()
    path = machine_resolver.registry_get(key)
    if not path:
        return repo, f"`repo` {key!r} names no registry key on this machine. Pass an absolute `repo` path."
    return path, None


def _sizing_home(sizing: str) -> tuple[str, Optional[str], Optional[str]]:
    """(sizing, repo, refusal) for an absolute sizing path with no `repo`: the repo holding the file is the run root.

    The sizing is made repo-relative so run tokens and halt lookups key on the same path a relative submission uses."""
    from coordinator_core.git.repo_root import show_toplevel

    top = show_toplevel(str(Path(sizing).parent))
    if not top:
        return sizing, None, f"Sizing {sizing} lies in no git repo, so no run root can be derived from it."
    try:
        return Path(sizing).resolve().relative_to(Path(top).resolve()).as_posix(), top, None
    except ValueError:
        return sizing, top, None


def _name_root_tried(text: str, root: Path, sizing_rel: Optional[str]) -> str:
    """`text` plus the run root tried when the engine could not find the submitted sizing there."""
    if sizing_rel and "not found on disk" in text:
        return f"Run root tried: {root}. {text} Pass the sizing's absolute path, or `repo`."
    return text


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


_OVER_CAP = "-byte cap"


@contextlib.contextmanager
def _hoisting(on: bool):
    """Set `prompt_fragments.HOIST_ENABLED` to `on` for the block, restored on exit."""
    from coordinator_core.ops.dispatch_emit import prompt_fragments

    prior = prompt_fragments.HOIST_ENABLED
    prompt_fragments.HOIST_ENABLED = on
    try:
        yield
    finally:
        prompt_fragments.HOIST_ENABLED = prior


def _default_emit(params: dict, repo_root: Path) -> dict:
    """`dispatch.emit` with fragment hoisting off: hoisting only shrinks the script and costs ~200ms per
    run of process time. A script then over the Workflow byte cap is re-emitted hoisted."""
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    try:
        with _hoisting(False):
            result = _dispatch_emit(params, repo_root)
        over = isinstance(result, dict) and _OVER_CAP in str(_no_run_detail(result)) and not result.get("path")
    except Exception as exc:
        if _OVER_CAP not in str(exc):
            raise
        over = True
    if not over:
        return result
    with _hoisting(True):
        return _dispatch_emit(params, repo_root)


#: Total stall one hook call may absorb across every read it makes; a read past it returns
#: `STALLED` without waiting. Outside `evaluate` each direct call is bounded by it alone.
_READ_DEADLINE_MS = 300.0
_INFLIGHT: set = set()
_INFLIGHT_LOCK = threading.Lock()
_BUDGET: Optional[list] = None  # [ms still unspent] while `evaluate` runs; an orphaned worker may outlive the reset to None


class _SizingStalled(Exception):
    """The sizing read outlived the stall budget: its ask_parent marker and research block are unknown."""


def _in_scope(fn: Callable[[], Any]) -> Callable[[], Any]:
    """`fn` bound to the calling thread's context, so a worker thread joins the open `sizing_memo` scope."""
    ctx = contextvars.copy_context()
    return lambda: ctx.run(fn)


def _wait_ms(deadline_ms: Optional[float]) -> float:
    """The wait a read may take now: its own cap, cut to the unspent hook budget."""
    cap = _READ_DEADLINE_MS if deadline_ms is None else deadline_ms
    return cap if _BUDGET is None else min(cap, _BUDGET[0])


def _charge(started: float) -> None:
    if _BUDGET is not None:
        import time

        _BUDGET[0] -= (time.monotonic() - started) * 1000.0


#: The caller's child budget (DoE `_hook_boot.py` `_CHILD_BUDGET_S`); the caller kills the hook at it
#: and denies with no run id, so the hook answers on its own at `_EMIT_BUDGET_SHARE` of it. The
#: effective budget is the lesser of the caller's and the door's `_DOOR_HOOK_READ_DEADLINE_S`.
_CALLER_BUDGET_S = 12.0
#: Twin of `DOOR_HOOK_SUBMIT_READ_DEADLINE_MS` in warm/door/door.c and door_posix.c (the door's longer
#: bound for this op alone; every other hook read is held to `DOOR_HOOK_READ_DEADLINE_MS`, 500ms):
#: past it the door gives up and relays no hook answer. Pinned equal by test_submit_engine_ask_door_deadline.py.
_DOOR_HOOK_READ_DEADLINE_S = 8.0
_EMIT_BUDGET_SHARE = 0.8
_FANOUT_TAIL_GRACE_S = 0.25


class _EmitOverran(Exception):
    """The emit outlived the caller's budget; `message` is the deny text."""

    def __init__(self, message: str, minted: bool = False, stalled: bool = False, run_ids: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.minted = minted
        self.stalled = stalled
        self.run_ids = tuple(run_ids)


def _caller_budget_s() -> float:
    try:
        value = float(os.environ.get("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", ""))
    except ValueError:
        value = _CALLER_BUDGET_S
    if not value > 0:
        value = _CALLER_BUDGET_S
    return min(value, _DOOR_HOOK_READ_DEADLINE_S)


#: Fraction of the caller budget by which `evaluate` answers whatever it is doing; the emit join
#: ends earlier (`_EMIT_BUDGET_SHARE`), and the minted-run scan at the answer gets `_SCAN_GRACE_S`.
_ANSWER_SHARE = 0.85
_SCAN_GRACE_S = 0.8
_EMIT_FLOOR_S = 0.25
_CALL: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar("submit_call", default=None)  # {"t0", "wall0", "roots", "minted", "gate", "gates", "answered", "session"}


def _owned(call: Optional[dict], root: Path, ids: Sequence[str], session: Optional[str] = None) -> list:
    """The `ids` this call may name as its own: those it reported minting, plus those whose run-token record
    names its session (`session`, else the call's). Trap: every session on the box mints under the same `state/warp`, so a run that
    appeared in the window is a peer's until its record says otherwise; naming one starts it here."""
    reported = set(call["minted"]) if call else set()
    session = session or (call.get("session") if call else None)
    rest =[i for i in ids if i not in reported]
    owners: dict = {}
    if rest and session:
        from coordinator_core.ops.dispatch_emit.run_token import live_records

        owners = {r["run_id"]: r.get("session_id") for r in live_records(root)}
    return sorted({i for i in ids if i in reported or (session and owners.get(i) == session)})


def _emit_join_s() -> float:
    """The emit join: the emit share of the caller budget measured from the call's start, so work
    before the emit shortens it (never below `_EMIT_FLOOR_S`)."""
    import time

    full = _caller_budget_s() * _EMIT_BUDGET_SHARE
    call = _CALL.get()
    if call is None:
        return full
    return max(_EMIT_FLOOR_S, min(full, call["t0"] + full - time.monotonic()))


def _note_root(root: Path) -> None:
    call = _CALL.get()
    if call is not None and root not in call["roots"]:
        call["roots"].append(root)


def _note_resume(run_id: str) -> None:
    """Records the halted run this call resolved to refire: a spool of the same ask would drain into a
    second launch of that run id, so an overrun answers a refusal instead (`_answer_overran`)."""
    call = _CALL.get()
    if call is not None:
        call["resume"] = run_id


def _note_minted(run_id: Any) -> None:
    call = _CALL.get()
    if call is not None and isinstance(run_id, str) and run_id not in call["minted"]:
        call["minted"].append(run_id)


def _minted_since_start(call: dict) -> Optional[list]:
    """Runs this call minted: those it reported plus any run dir under its roots created since it began.
    None when the directory read stalled past `_SCAN_GRACE_S`."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

    found: list = list(call["minted"])

    def scan() -> None:
        for root in list(call["roots"]):
            fresh: list = []
            try:
                with os.scandir(root / RECORD_DIR_ROOT) as it:
                    for e in it:
                        stage = os.path.join(e.path, RUN_STAGE_FILE)
                        try:
                            if e.is_dir() and os.stat(stage).st_mtime >= call["wall0"] - 1.0 and e.name not in found:
                                fresh.append(e.name)
                        except OSError:
                            continue
            except OSError:
                continue
            found.extend(i for i in _owned(call, root, fresh) if i not in found)

    thread = threading.Thread(target=scan, daemon=True)
    thread.start()
    thread.join(_SCAN_GRACE_S)
    return None if thread.is_alive() else sorted(found)


_MINTED_OP = "dispatch.minted_run"
_ENVELOPE_GRACE_S = 0.35


def _log_fail_open(cause: str, call: Optional[dict] = None) -> None:
    """Appends one line to `<settings>/state/door-fail-open.log`, the same shape the door twins write
    (`door_log_fail_open`). Never raises and never changes the verdict."""
    import time
    from datetime import datetime, timezone

    try:
        from coordinator_core._settings_home import settings_home
        from coordinator_core.atomic_append import append_line

        state = settings_home() / "state"
        state.mkdir(parents=True, exist_ok=True)
        started = call.get("t0") if call else None
        elapsed = int((time.monotonic() - started) * 1000) if isinstance(started, float) else 0
        line = (
            f"ts={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} deadline_ms={int(_caller_budget_s() * _ANSWER_SHARE * 1000)} "
            f"elapsed_ms={elapsed} op=hooks.submit_engine_ask door_pid=- engine_pid={os.getpid()} engine_start=- "
            f"cause={cause[:160]}\n"
        )
        append_line(state / "door-fail-open.log", line.encode("utf-8"))
    except Exception:
        return


def _answer_overran(call: dict) -> dict:
    """The answer for a call still running at the answer deadline. A run it minted rides an allow that
    starts it (`_minted_start_envelope`); a deny only when no run was minted or the disk is stalled."""
    import time

    _log_fail_open("submit hook outran its answer deadline", call)
    gate_state = _cancel_call_gates(call)
    minted = _minted_since_start(call)
    deadline = time.monotonic() + _SCAN_GRACE_S
    roots = call["roots"]
    if minted:
        return _minted_start_envelope(roots[0] if roots else None, minted, deadline, call)
    if minted is None:
        return _refuse(
            "The submission outran the hook budget and the disk is stalled, so whether a run was minted is unknown. "
            f"List recent runs under state/warp before re-submitting; start or abandon any at stage minted with {_MINTED_OP}."
        )
    if gate_state == "committed":
        ids = _token_run_ids_since(call, deadline) if roots else []
        if ids:
            return _minted_start_envelope(roots[0], ids, deadline, call)
        return _refuse(f"The submission outran the hook budget as its run was minted. Do not re-submit. {_minted_remedy([])}")
    if gate_state == "writing":
        return _refuse(_STALLED_RECORD_WRITE)
    resuming = call.get("resume")
    if resuming:
        return _refuse(
            f"The refire of run {resuming} outran the hook budget; the engine is reachable and "
            "nothing was spooled. Re-submit once: it resumes the same run."
        )
    spooled = _spool_envelope(call.get("payload"), roots[0] if roots else None, "reach-budget-no-run")
    if spooled is not None:
        return spooled
    return _refuse("The submission outran the hook budget before any run was minted; nothing was taken. Re-submit once.")


_STALLED_RECORD_WRITE = (
    "The submission was refused: the disk is stalled, so the run record could not be written within the hook "
    "budget. Nothing was minted and no run will appear later; re-submitting is safe once the disk recovers."
)


def _cancel_call_gates(call: dict) -> str:
    """Marks the call answered and cancels every emit gate it opened: "committed" when any had published its
    token record, else "writing" when any was mid-write, else "idle". After it no emit of this call starts or publishes."""
    call["answered"] = True
    states = [g.cancel() for g in (call.get("gate"), *call.get("gates", ())) if g is not None]
    for state in ("committed", "writing"):
        if state in states:
            return state
    return "idle"


def _token_run_ids_since(call: dict, deadline: float) -> list:
    """Run ids whose token record this call published (minted after it began) under its roots; [] on a stalled read."""
    from coordinator_core.ops.dispatch_emit.run_token import live_records

    def read() -> list:
        ids: list = []
        for root in list(call["roots"]):
            for record in live_records(root):
                try:
                    if (
                        float(record.get("minted_at") or 0) >= call["wall0"] - 1.0
                        and record["run_id"] not in ids
                        and (record["run_id"] in call["minted"] or (call.get("session") and record.get("session_id") == call["session"]))
                    ):
                        ids.append(record["run_id"])
                except (TypeError, ValueError):
                    continue
        return ids

    return _quick(read, deadline) or []


def _quick(fn: Callable[[], Any], deadline: float) -> Any:
    """`fn()` on a daemon thread joined until the monotonic `deadline`; None when it outlives that or raises."""
    import time

    box: list = []

    def work() -> None:
        try:
            box.append(fn())
        except Exception:
            pass

    thread = threading.Thread(target=_in_scope(work), daemon=True)
    thread.start()
    thread.join(max(deadline - time.monotonic(), 0.05))
    return box[0] if box else None


def _write_leg(label: str, fn: Callable[[], Any], gated: bool = False) -> tuple[str, Any]:
    """A record write on a daemon thread joined to the remaining emit window: ("done", value),
    ("raised", exception) or ("detached", None) for a thread still running at the window's end.
    Detached is outcome-unknown (the hook process exiting kills the thread); `gated` cancels a
    `run_token.mint` publish so it cannot land after the answer. The step is named on the call."""
    from coordinator_core.ops.dispatch_emit import run_token

    gate = run_token.WriteGate() if gated else None
    box: list = []

    def work() -> None:
        if gate is not None:
            run_token.WRITE_GATE.set(gate)
        try:
            box.append(("done", fn()))
        except Exception as exc:
            box.append(("raised", exc))

    thread = threading.Thread(target=_in_scope(work), daemon=True)
    thread.start()
    thread.join(_emit_join_s())
    if box:
        return box[0]
    if gate is not None:
        gate.cancel()
    call = _CALL.get()
    if call is not None and label not in call.setdefault("detached", []):
        call["detached"].append(label)
    return "detached", None


def _write_or_detach(label: str, fn: Callable[[], Any]) -> bool:
    """True when the write detached; a raise inside it propagates as it did unbounded."""
    state, value = _write_leg(label, fn)
    if state == "raised":
        raise value
    return state == "detached"


def _detached_note(steps: Sequence[str]) -> str:
    return (
        f" Detached: {', '.join(steps)} did not finish within the hook budget, so whether it landed is unknown "
        "and the run's commit is handed back."
    )


def _allow_rewrite(new_input: dict, context: str, reason: str) -> dict:
    envelope = rewrite_input("PreToolUse", new_input, context)
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = reason
    return envelope


def _minted_remedy(run_ids: Sequence[str]) -> str:
    which = ", ".join(run_ids) if run_ids else "the run under state/warp left at stage minted"
    return f"Start or abandon {which} with {_MINTED_OP} {{run_id, action: start|abandon}}."


def _direct_start(root: Path, run_id: str, deadline: float) -> Optional[dict]:
    """The rewrite onto a script that starts a minted run's own script with the op's fire_args through the
    stage loop; None when the run script is not on disk yet."""
    def start() -> Any:
        from coordinator_core.ops.dispatch_emit import minted_run

        from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

        if not minted_run.is_minted_only(root / RECORD_DIR_ROOT / run_id):
            return None
        reply = minted_run.start_args(root, run_id)
        if not (isinstance(reply, dict) and reply.get("ok") and reply.get("script_path")):
            return None
        return write_direct_start_script(root, run_id, reply["script_path"], reply.get("fire_args") or None)

    script = _quick(start, deadline)
    if script is None:
        return None
    return _allow_rewrite(
        {"scriptPath": str(script)},
        f"Submitted to the engine as run {run_id}: its emit outran the hook budget after minting it, so the run starts "
        f"from the script already on disk. It runs to its commit in the background; its completion notification is your "
        f"one wake. Do not re-submit. Do not poll or verify this run meanwhile; unrelated work proceeds. {_revise_hint(run_id)}",
        f"Engine submission: run {run_id} started after an emit overrun.",
    )


def _note_entries(call: Optional[dict], asks: Sequence[Mapping[str, Any]], ready: Sequence[Any]) -> None:
    """Records on `call` which asks entry each ready fan-out run serves, so an overrun can name the entries
    no run was minted for. Matched by the fan-out's own label rule; nothing is recorded for a label that matches no entry or more than one."""
    if call is None:
        return
    labels = [(item.get("label") or "").strip() or f"slice-{k + 1}" for k, item in enumerate(asks)]
    entries = []
    for r in ready:
        if labels.count(r.label) == 1:
            index = labels.index(r.label)
            entries.append({"index": index, "label": r.label, "run_id": r.run_id, "item": asks[index]})
    call["entries"] = entries


def _unminted_tail(call: Optional[dict], minted: Sequence[str]) -> str:
    """The reply tail naming each asks entry of a partially minted fan-out with its re-submit args; "" unless
    a strict, non-empty subset of the submission's ready entries was minted. Re-submit args are JSON. Entries
    held on a predecessor are not named: the launcher starts them when the predecessor closes."""
    import json

    entries = (call or {}).get("entries") or []
    have = set(minted)
    missing = [e for e in entries if e["run_id"] not in have]
    if not missing or len(missing) == len(entries):
        return ""
    lines = []
    for e in missing:
        lines.append(f"asks[{e['index']}] ({e['label']}) was NOT minted; re-submit args: {json.dumps({'asks': [e['item']]})}")
    return (
        f" The submission minted {len(entries) - len(missing)} of {len(entries)} entries; "
        "re-submit only the unminted ones, never the minted. " + " ".join(lines)
    )


def _append_reason(envelope: dict, tail: str) -> dict:
    hso = envelope.get("hookSpecificOutput")
    if tail and isinstance(hso, dict):
        hso["permissionDecisionReason"] = (hso.get("permissionDecisionReason") or "") + tail
    return envelope


def _minted_start_envelope(
    root: Optional[Path], run_ids: Sequence[str], deadline: float, call: Optional[dict] = None
) -> dict:
    """The allow for runs the submission minted before the hook answered, never a deny.

    A lone run whose script is on disk is started directly; any other rides a deferred script that starts
    each through `dispatch.minted_run`, whose failure names the op's start and abandon. The deny is the
    last resort (no root, no run id, or a script that cannot be written) and names the op. When only some
    entries of a multi-ask were minted, every reply also names the unminted entries and their re-submit args."""
    ids = sorted(run_ids)
    tail = _unminted_tail(call if call is not None else _CALL.get(), ids)
    if root is None or not ids:
        return _refuse(f"The submission outran the hook budget after minting a run. {_minted_remedy(ids)}{tail}")
    import time

    if len(ids) == 1:
        direct = _direct_start(root, ids[0], min(deadline, time.monotonic() + _ENVELOPE_GRACE_S / 2))
        if direct is not None:
            return _append_reason(direct, tail)
    script = _quick(lambda: _write_minted_start_script(root, ids), max(deadline, time.monotonic() + 0.05))
    if script is None:
        return _refuse(f"The submission outran the hook budget after minting run {', '.join(ids)}. {_minted_remedy(ids)}{tail}")
    return _append_reason(_allow_rewrite(
        {"scriptPath": str(script)},
        f"Submitted to the engine as run {', '.join(ids)}: its emit outran the hook budget after minting it, so a script "
        f"starts it in the background through {_MINTED_OP}. Its completion notification is your one wake. "
        "Do not re-submit. Do not poll or verify this run meanwhile; unrelated work proceeds.",
        f"Engine submission: minted run {', '.join(ids)}, deferred start {script.name}.",
    ), tail)


def _runner_less_run(root: Path, sizing_rel: str, live: Optional[str]) -> Optional[str]:
    """The run for `sizing_rel` that was minted, joined no launch past `warp_live.MINTED_RUNNER_GRACE_SECONDS`
    and so has no runner: `live` itself when it proves none, else a token-held run of that sizing. Resumable
    through `_MINTED_OP`, never "already working"."""
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit import run_token

    if live:
        return live if warp_live.runner_unproven(root, live) else None
    rel = run_token.normalize_sizing_rel(root, sizing_rel)
    if not rel:
        return None
    for record in run_token.live_records(root):
        named = record.get("sizing_rel")
        if isinstance(named, str) and run_token.normalize_sizing_rel(root, named) == rel:
            if warp_live.runner_unproven(root, record["run_id"]):
                return record["run_id"]
    return None


def _run_dirs(root: Path) -> set:
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

    try:
        with os.scandir(root / RECORD_DIR_ROOT) as it:
            return {e.name for e in it if e.is_dir() and os.path.exists(os.path.join(e.path, RUN_STAGE_FILE))}
    except OSError:
        return set()


def _emit_gate(call: Optional[dict]) -> Any:
    """The write gate for one emit: the call's own for its first emit, a registered extra for each later one,
    so `_answer_overran` cancels them all. `_EmitOverran` (nothing minted) when the call has already answered."""
    from coordinator_core.ops.dispatch_emit import run_token

    refusal = "The submission outran the hook budget before any run was minted; nothing was taken. Re-submit once."
    if call is None:
        return run_token.WriteGate()
    if call.get("answered"):
        raise _EmitOverran(refusal)
    if call.get("gate") is not None and not call.get("gate_used"):
        call["gate_used"] = True
        gate = call["gate"]
    else:
        gate = run_token.WriteGate()
        call.setdefault("gates", []).append(gate)
    if call.get("answered"):  # the answer landed between the check and the registration
        gate.cancel()
        raise _EmitOverran(refusal)
    return gate


def _session_tokens_since(root: Path, session: Optional[str], since: float) -> list:
    """Run ids whose token record `session` minted at or after `since`; [] with no session or on a stalled read."""
    import time

    from coordinator_core.ops.dispatch_emit.run_token import live_records

    if not session:
        return []

    def read() -> list:
        return sorted(
            r["run_id"] for r in live_records(root)
            if r.get("session_id") == session and float(r.get("minted_at") or 0) >= since - 1.0
        )

    return _quick(read, time.monotonic() + _SCAN_GRACE_S) or []


def _emit_within_budget(
    emit: EmitFn, params: dict, root: Path, budget_s: Optional[float] = None, scan: bool = True
) -> dict:
    """`emit(params, root)`, or `_EmitOverran` naming any run it minted once the emit budget is spent.

    The emit keeps running on a daemon thread; the caller kills the process at its own budget, so
    the answer must land first or the caller denies with no run id and the EM re-submits.
    `scan=False` skips the `state/warp` directory walk: the emit writes only a run-token record, which
    the write gate alone reports."""
    from coordinator_core.ops.dispatch_emit import run_token

    import time

    call = _CALL.get()
    gate = _emit_gate(call)
    before = _run_dirs(root) if scan else set()
    wall0 = time.time()
    box: dict = {}

    def work() -> None:
        from coordinator_core.session.core import session_identity_override

        try:
            sid = params.get("session_id")
            with session_identity_override(sid if isinstance(sid, str) else None):
                box["result"] = emit(params, root)
        except BaseException as exc:  # re-raised on the caller's thread
            box["error"] = exc

    def gated() -> None:
        run_token.WRITE_GATE.set(gate)
        work()

    thread = threading.Thread(target=_in_scope(gated), daemon=True)
    thread.start()
    thread.join(_emit_join_s() if budget_s is None else budget_s)
    if thread.is_alive():
        minted = _owned(call, root, sorted(_run_dirs(root) - before), params.get("session_id") or None) if scan else []
        if minted:
            names = ", ".join(minted)
            raise _EmitOverran(
                f"The submission outran the hook budget after minting run {names}. Do not re-submit: "
                f"the run exists; reconcile it by id (a re-submit duplicates it).",
                minted=True,
                run_ids=minted,
            )
        # The cancel keeps the abandoned thread from publishing its record after this answer.
        cancelled_in = gate.cancel()
        if cancelled_in == "writing":
            raise _EmitOverran(_STALLED_RECORD_WRITE, stalled=True)
        if cancelled_in == "committed":  # the record published between the scan above and the cancel
            session = params.get("session_id") or None
            late = _owned(call, root, sorted(_run_dirs(root) - before), session) if scan else []
            if scan and not late:  # the emit writes its token before its run dir: the token alone names the run
                late = _session_tokens_since(root, session or (call or {}).get("session"), wall0)
            if late:
                raise _EmitOverran(
                    f"The submission outran the hook budget after minting run {', '.join(late)}. Do not re-submit: "
                    f"the run exists; reconcile it by id (a re-submit duplicates it).",
                    minted=True,
                    run_ids=late,
                )
            raise _EmitOverran(
                "The submission outran the hook budget as its run was minted. Do not re-submit: "
                "the run exists; reconcile it by listing runs (a re-submit duplicates it).",
                minted=True,
                run_ids=late,
            )
        raise _EmitOverran("The submission outran the hook budget before any run was minted; nothing was taken. Re-submit once.")
    if "error" in box:
        raise box["error"]
    return box["result"]


from coordinator_core.ops.dispatch_emit.deferred_emit_script import (  # noqa: E402,F401
    _CHAIN_FOLLOW_JS,
    _DEFERRED_JS,
    _DEFERRED_START_JS,
    _EMIT_RELAY_JS,
    _STAGE_LOOP_JS,
    DEFERRED_BRIEF_INLINE_MAX,
    _chain_follow_values,
    _spill_long_brief,
    _stage_loop_values,
    write_deferred_script,
)


_SPOOLED_JS = r"""export const meta = { name: 'engine-ask-spooled', description: 'Marks an ask held in the local ask spool' }
return { status: 'spooled', spool_id: __SPOOL_ID__ };
"""

_UNAVAILABLE_CODE = -32004


def _unavailable(exc: BaseException) -> Optional[str]:
    """The spool reason when `exc` is a held mint (HandoverPending) or a -32004 dispatch timeout, else None."""
    from coordinator_core.ops.dispatch_emit.requirements_ledger import HandoverPending

    if isinstance(exc, HandoverPending):
        return "handover-pending"
    code = getattr(exc, "code", None)
    if code is None and exc.args and isinstance(exc.args[0], dict):
        code = exc.args[0].get("code")
    return "dispatch-timeout" if code == _UNAVAILABLE_CODE else None


_WITHDRAW_HINT = (
    "To withdraw it: coordinator-invoke ask_spool.withdraw "
    "'{{\"spool_id\":\"{spool_id}\",\"reason\":\"...\"}}'."
)


def _spool_envelope(payload: Any, root: Optional[Path], reason: str) -> Optional[dict]:
    """Allow envelope for an ask written to the ask spool ("spooled as <id>"), or None when it cannot be
    spooled (no payload or root, or an unwritable disk), so the caller keeps its own answer. No engine call."""
    import json

    from coordinator_core.ask_spool import record

    if not isinstance(payload, Mapping):
        return None
    if root is None:
        root = _repo_root(None, dict(payload))
        if root is None:
            return None
    session = payload.get("session_id")
    tool_use_id = payload.get("tool_use_id")
    try:
        raw = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        spool_id, _created = record.spool_ask(
            str(root),
            raw,
            session_id=session if isinstance(session, str) else "",
            tool_use_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None,
            reason=reason,
        )
    except (OSError, ValueError):
        return None
    script = root / "scratch" / "warp" / f"spooled-{spool_id}.workflow.mjs"
    try:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(_SPOOLED_JS.replace("__SPOOL_ID__", json.dumps(spool_id)), encoding="utf-8")
    except OSError:
        return _refuse(
            f"The engine is unavailable ({reason}); the ask is spooled as {spool_id} and mints once the "
            "engine is healthy. Do not re-submit. " + _WITHDRAW_HINT.format(spool_id=spool_id)
        )
    return _allow_rewrite(
        {"scriptPath": str(script)},
        f"The engine is unavailable ({reason}); the ask is spooled as {spool_id}. It mints exactly one run "
        "once the engine is healthy and this session is notified. Do not re-submit. Do not poll or verify this run meanwhile; unrelated work proceeds. "
        + _WITHDRAW_HINT.format(spool_id=spool_id),
        f"Engine submission: spooled as {spool_id}.",
    )


def _write_deferred_script(root: Path, params: dict) -> Path:
    """The deferred script for `params`; the hook's call sites resolve this name so a test can replace it."""
    return write_deferred_script(root, params)


_MINTED_START_JS = r"""export const meta = { name: 'engine-ask-minted-start', description: 'Starts runs a submission minted before its hook answered' }
const REPO_ROOT = __REPO_ROOT__;
const RUN_IDS = __RUN_IDS__;
const INVOKE = __INVOKE__;
const OP = __OP__;
const START_SCHEMA = {
  type: 'object',
  required: ['ok', 'script_path'],
  properties: { ok: { type: 'boolean' }, script_path: { type: 'string' }, refusal: { type: 'string' } },
};
""" + _STAGE_LOOP_JS + r"""const results = [];
for (const rid of RUN_IDS) {
  const params = JSON.stringify({ run_id: rid, action: 'start' });
  const reply = await agent(
    __AUTHORITY__
    + ' Run `' + INVOKE + ' --repo "' + REPO_ROOT + '" ' + OP + " '" + params + "'` once, with the command word exactly as written, and "
    + 'return ok, script_path and refusal from its JSON reply verbatim. When the command is denied or fails, '
    + "return ok false, script_path as an empty string and the command's error text verbatim in refusal. "
    + 'If you do not run the command for any reason, put that reason verbatim in refusal the same way.',
    { label: 'minted-start', phase: 'start', agentType: 'coordinator:op-relay', model: 'haiku', effort: 'low', schema: START_SCHEMA },
  );
  const path = (reply && reply.ok === true && typeof reply.script_path === 'string') ? _normPath(reply.script_path) : '';
  // Trap: the relay can mis-copy; the script path must be this run's own script.
  if (path === '' || path.split('/').pop() !== rid + RUN_SCRIPT_SUFFIX) {
    return {
      status: 'failed', run_id: rid,
      error: 'minted run ' + rid + ' did not start (' + ((reply && reply.refusal) || 'the op returned no run script') + '); start or abandon it with ' + OP + ' {"run_id":"' + rid + '","action":"start|abandon"}',
    };
  }
  const started = await _startLoop(path);
  if (!started.ok) return { status: 'failed', run_id: rid, error: started.error };
  results.push(started.result);
}
return results.length === 1 ? results[0] : { started: results };
"""

# Starts one minted run's script through the stage loop, so a stage_pending return starts the staged twin.
_DIRECT_START_JS = r"""export const meta = { name: 'engine-ask-direct-start', description: 'Starts a minted run through the stage loop' }
const REPO_ROOT = __REPO_ROOT__;
const RUN_ID = __RUN_ID__;
const RUN_PATH = __RUN_PATH__;
""" + _EMIT_RELAY_JS + _STAGE_LOOP_JS + r"""const ASK_RUN_ID = RUN_ID;
const ASK_SCRIPT_PATH = _normPath(RUN_PATH);
const first = await _startLoop(ASK_SCRIPT_PATH);
""" + _CHAIN_FOLLOW_JS


def _write_minted_start_script(root: Path, run_ids: Sequence[str]) -> Path:
    """A script under `scratch/warp/` that starts each minted run through `dispatch.minted_run`."""
    import json

    from coordinator_core.ops.dispatch_emit.fan_launcher import _INVOKE, EMIT_RELAY_AUTHORITY

    values = {
        "__REPO_ROOT__": json.dumps(Path(root).as_posix()),
        "__RUN_IDS__": json.dumps(list(run_ids)),
        "__INVOKE__": json.dumps(_INVOKE),
        "__OP__": json.dumps(_MINTED_OP),
        "__AUTHORITY__": json.dumps(EMIT_RELAY_AUTHORITY),
        **_stage_loop_values(),
    }
    script = root / "scratch" / "warp" / f"defer-{uuid.uuid4().hex[:12]}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(re.sub("|".join(values), lambda m: values[m.group(0)], _MINTED_START_JS), encoding="utf-8")
    return script


def write_direct_start_script(root: Path, run_id: str, script_path: str, fire_args: Optional[dict]) -> Path:
    """A script under `scratch/warp/` that starts one minted run's script with `fire_args` through the stage loop."""
    import json

    values = {
        "__REPO_ROOT__": json.dumps(Path(root).as_posix()),
        "__RUN_ID__": json.dumps(run_id),
        "__RUN_PATH__": json.dumps(str(script_path).replace("\\", "/")),
        **_stage_loop_values(fire_args),
        **_chain_follow_values(),
    }
    script = root / "scratch" / "warp" / f"defer-{uuid.uuid4().hex[:12]}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(re.sub("|".join(values), lambda m: values[m.group(0)], _DIRECT_START_JS), encoding="utf-8")
    return script


def _premintable(params: dict, sizing_rel: Optional[str], asks: Optional[list], supersedes: Optional[str]) -> bool:
    """A plain raw ask: no sizing, fan-out or supersession, whose emit has nothing to reserve or resume."""
    return (
        isinstance(params.get("ask"), str)
        and sizing_rel is None
        and asks is None
        and not supersedes
        and "reserved_run_id" not in params
        and "ask_brief_file" not in params  # the emit reads the file; a missing one is refused before the run
    )


def _premint_marker(params: dict, root: Path, run_id: Optional[str] = None) -> dict:
    """Mint the run id (`run_id` when the caller chose it first) and publish its run-token marker (the one
    record write before the answer). A marker whose deferred script never runs is reaped by the token TTL."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    run_id = run_id or run_token.new_ask_run_id()
    raw = params.get("ask")
    run_token.mint(
        root, run_id, f"{RUN_DIR_ROOT}/{run_id}.workflow.mjs",
        session_id=params.get("session_id") or None, marker=True,
        ask_sha256=normalized_ask_hash(raw) if isinstance(raw, str) and raw.strip() else None,
    )
    return {"run_id": run_id}


def _premint_deferred_envelope(root: Path, params: dict, run_id: str) -> dict:
    """The allow answer for a raw ask whose run id is minted: the emit (prompts, script, receipt) and the
    run both start from a written script, so no bulk read or write precedes the answer."""
    deferred = {**params, "preminted_run_id": run_id}
    try:
        script = _write_deferred_script(root, deferred)
    except OSError:
        try:
            from coordinator_core.ops.dispatch_emit import run_token

            run_token.discard(root, run_id)
        except OSError:
            pass
        return _refuse("The submission could not be written to disk; no run was started. Re-submit once.")
    envelope = rewrite_input(
        "PreToolUse",
        {"scriptPath": str(script)},
        f"Submitted to the engine as run {run_id}: its emit runs inside the background run, which then starts. "
        "Its completion notification is your one wake. Do not re-submit. Do not poll or "
        f"verify this run meanwhile; unrelated work proceeds. {_revise_hint(run_id)}",
    )
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = f"Engine submission: run {run_id}, deferred emit {script.name}."
    return envelope


def _deferred_envelope(root: Path, params: dict, over: _EmitOverran) -> dict:
    """The taken submission for an emit that outran the hook budget with no run minted; a refusal
    naming `over` when the script cannot be written."""
    taken = _taken_as_deferred(root, params)
    return taken if taken is not None else _refuse(over.message)


def _taken_as_deferred(root: Path, params: dict) -> Optional[dict]:
    """The allow envelope taking `params` as a deferred emit, or None when its script cannot be written.

    The hook process ends when it answers, so the emit cannot finish on its daemon thread; a
    written script re-runs `dispatch.emit` through an op-relay agent and starts what it emits."""
    try:
        script = _write_deferred_script(root, params)
    except OSError:
        return None
    defer_id = script.name.split(".")[0]
    envelope = rewrite_input(
        "PreToolUse",
        {"scriptPath": str(script)},
        f"The emit outran the hook budget before minting a run, so the submission is taken as deferred emit {defer_id}: "
        "its script emits the run and starts it in the background; the run id arrives with its completion notification, "
        "your one wake. Do not re-submit. Do not poll or verify this run meanwhile; unrelated work proceeds.",
    )
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = f"Engine submission: deferred emit {script.name}."
    return envelope


def _budgeted_read(read: Callable[[float], Any]) -> Any:
    """`read(deadline_ms)` for a reader that bounds itself; `STALLED` at once when the budget is spent."""
    import time

    from coordinator_core.ops.dispatch_emit.halt_resume_record import STALLED

    wait = _wait_ms(None)
    if wait <= 0:
        return STALLED
    started = time.monotonic()
    try:
        return read(wait)
    finally:
        _charge(started)


def _bounded_call(key: Any, fn: Callable[[], Any], deadline_ms: Optional[float] = None) -> Any:
    """`fn()` run on a daemon thread joined for the remaining hook budget (at most `deadline_ms`);
    `halt_resume_record.STALLED` when it outlives that (the thread is left to finish), and `fn`'s own
    exception re-raised. A spent budget returns `STALLED` without starting `fn`.
    At most one call per `key` is in flight: a second while one is outstanding returns `STALLED` at once."""
    import time

    from coordinator_core.ops.dispatch_emit.halt_resume_record import STALLED

    wait = _wait_ms(deadline_ms)
    if wait <= 0:
        return STALLED
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return STALLED
        _INFLIGHT.add(key)
    box: list = []
    done = threading.Event()

    def work() -> None:
        try:
            box.append((True, fn()))
        except BaseException as exc:
            box.append((False, exc))
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(key)
            done.set()

    try:
        threading.Thread(target=_in_scope(work), daemon=True).start()
    except BaseException:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(key)
        return STALLED
    started = time.monotonic()
    finished = done.wait(wait / 1000.0)
    _charge(started)
    if not finished:
        return STALLED
    ok, value = box[0]
    if ok:
        return value
    raise value


def _load_sizing_doc(root: Path, sizing_rel: str) -> dict:
    """The sizing mapping; an unreadable sizing reads `{}` and is left to the engine's own refusal.
    A stalled read raises `_SizingStalled`: `{}` would drop the ask_parent marker and the research block."""
    from coordinator_core.ops.dispatch_emit.halt_resume_record import STALLED
    from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, load_sizing

    try:
        doc = _bounded_call(("sizing", str(root), sizing_rel), lambda: load_sizing(root, sizing_rel))
    except SizingFireRefused:
        return {}
    if doc is STALLED:
        raise _SizingStalled(sizing_rel)
    return doc if isinstance(doc, dict) else {}


#: Wall time one batch of sibling sizing reads may take; the batch has its own clock, so reads
#: here never draw on the shared `_BUDGET` and a slow earlier read cannot starve a later one.
_SIZING_BATCH_DEADLINE_MS = 300.0


def _load_sizing_docs(root: Path, sizing_rels: Sequence[str]) -> dict:
    """{sizing_rel: doc} for every rel, read concurrently under one `_SIZING_BATCH_DEADLINE_MS` clock.
    A read that outlives it, or whose key is already in flight, maps to `STALLED`; an unreadable sizing maps to `{}`."""
    import time

    from coordinator_core.ops.dispatch_emit.halt_resume_record import STALLED
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

    slots: dict = {}
    for rel in sizing_rels:
        key = ("sizing", str(root), rel)
        with _INFLIGHT_LOCK:
            if key in _INFLIGHT:
                slots[rel] = None
                continue
            _INFLIGHT.add(key)
        box: list = []
        done = threading.Event()

        def work(rel=rel, key=key, box=box, done=done) -> None:
            try:
                box.append(load_sizing(root, rel))
            except BaseException:  # SizingFireRefused included: an unreadable sizing is left to the engine's refusal
                box.append({})
            finally:
                with _INFLIGHT_LOCK:
                    _INFLIGHT.discard(key)
                done.set()

        try:
            threading.Thread(target=_in_scope(work), daemon=True).start()
        except BaseException:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(key)
            slots[rel] = None
            continue
        slots[rel] = (box, done)
    end = time.monotonic() + _SIZING_BATCH_DEADLINE_MS / 1000.0
    out: dict = {}
    for rel, slot in slots.items():
        if slot is None or not slot[1].wait(max(0.0, end - time.monotonic())):
            out[rel] = STALLED
            continue
        doc = slot[0][0]
        out[rel] = doc if isinstance(doc, dict) else {}
    return out


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
        unknown = unknown_keys_refusal(f"asks[{i}]", item, _HOOK_ITEM_KEYS)
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
        repo = item.get("repo")
        if repo is not None and (not isinstance(repo, str) or not repo.strip()):
            return None, f"asks[{i}].repo must be a repo path (a non-empty string)."
        writes = item.get("writes")
        if writes is not None and not (isinstance(writes, list) and all(isinstance(w, str) for w in writes)):
            return None, f"asks[{i}].writes must be a list of repo-relative paths."
        if item.get("after") is not None:
            earlier = [(asks[k].get("label") or "").strip() or f"slice-{k + 1}" for k in range(i)]
            try:
                resolve_after(f"asks[{i}]", item["after"], earlier, i)
            except ValueError as exc:
                return None, str(exc)
        stop_why = _stop_after_refusal(f"asks[{i}]", item.get("stop_after"))
        if stop_why:
            return None, stop_why
    confirm = args.get("confirm_split")
    if confirm is not None and not isinstance(confirm, bool):
        return None, "confirm_split must be true or false."
    return asks, None


def _join(tool_input: Mapping[str, Any]) -> tuple[Optional[dict], Optional[str]]:
    """(join, refusal) from `args.join`: `{ask, inputs: [{run_id, repo?}]}`, valid beside `asks` only."""
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import JOIN_ITEM_KEYS

    args = tool_input.get("args")
    if not isinstance(args, dict) or "join" not in args:
        return None, None
    join = args["join"]
    if "asks" not in args:
        return None, "join rides an asks submission, not a single ask or sizing."
    if not isinstance(join, dict):
        return None, "join must be an object {ask, inputs: [{run_id, repo?}]}."
    unknown = unknown_keys_refusal("join", join, JOIN_ITEM_KEYS)
    if unknown:
        return None, unknown
    if not isinstance(join.get("ask"), str) or not join["ask"].strip():
        return None, "join carries no ask text."
    inputs = join.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return None, "join.inputs must be a non-empty list of {run_id, repo?}."
    for i, item in enumerate(inputs):
        if not isinstance(item, dict) or not isinstance(item.get("run_id"), str) or not item["run_id"].strip():
            return None, f"join.inputs[{i}] carries no run_id."
        unknown = unknown_keys_refusal(f"join.inputs[{i}]", item, _INPUT_KEYS)
        if unknown:
            return None, unknown
        repo = item.get("repo")
        if repo is not None and (not isinstance(repo, str) or not repo.strip()):
            return None, f"join.inputs[{i}].repo must be an absolute repo path (a string)."
    return join, None


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


def _read_parent(root: Path, parent_id: str, *, run_id: Optional[str] = None):
    """The parent record for `parent_id`; with `run_id`, a missing scratch record falls back to
    that run's tracked copy, accepted only when it names the same parent."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import (
        ParentRecord, parent_record_rel, run_parent_copy_rel,
    )

    try:
        return ParentRecord.from_json(json.loads((root / parent_record_rel(parent_id)).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not run_id:
        return None
    try:
        copy = ParentRecord.from_json(json.loads((root / run_parent_copy_rel(run_id)).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return copy if copy.parent_id == parent_id else None


def _reserved_token_spent(root: Path, run_id: str) -> bool:
    """Whether `run_id`'s token is spent: the gate refused it, or a landing attempt took it and it is not live.

    TRAP: a fan-out slice's sizing pins its original run id via `ask_parent`; re-reserving a
    spent one re-emits the same id, the gate refuses the same way, and the refusal's own
    remedy (this {sizing} call) loops forever. A spent reservation is dropped on the first
    refire so the emit mints a fresh run id; a never-fired one keeps its id.
    """
    from coordinator_core.ops.dispatch_emit.ask_gate import token_retired

    try:
        return token_retired(root, run_id)
    except (OSError, ValueError):
        return False


def _spent_after_ensure(root: Path, run_id: str) -> bool:
    """Whether `run_id` still holds no live run token after the launcher ensured one; a stalled read is not spent."""
    from coordinator_core.ops.dispatch_emit import run_token

    try:
        return not run_token.is_live(root, run_id) and run_token.escrowed(root, run_id) is None
    except (OSError, ValueError):
        return False


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
    parent = _read_parent(root, parent_id, run_id=run_id)
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
        if state is None:
            theirs = next((r for r in (parent.runs if parent else ()) if r.run_id == pred), None)
            sizing = theirs.slice_sizing if theirs and theirs.slice_sizing else "<its slice sizing>"
            return (
                f"Predecessor {pred} has not landed (not started). Fire it first: engine-ask {{sizing: {sizing}}}; "
                f"{run_id} then starts when it lands."
            )
        return (
            f"Predecessor {pred} has not landed ({state}). "
            f"Resubmit {run_id} once its close lands it; no call releases it sooner."
        )
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import external_status

    for ref in mine.after_external:
        if external_status(root, ref) == STATUS_LANDED:
            continue
        where = f" in {ref.repo}" if ref.repo else ""
        return (
            f"Join input {ref.run_id}{where} has not landed, so {run_id} cannot start. "
            f"Check it: coordinator-invoke dispatch.sibling_status {{parent_id: {parent_id}}}."
        )
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
    from coordinator_core.ops.dispatch_emit.fan_launcher import LAUNCHER_CHILD_EMIT_PARAM, write_launcher

    # Every child is a deferred-emit script: the launcher child runs `dispatch.emit` and starts the run, so the hook
    # pays no per-child emit. A refusal inside that emit is named by the launcher as the child's `emit_refused`.
    emitted: list = []
    # The defer marker is hook-internal: it must reach neither the deferred script nor the launcher's per-child emit literal.
    clean: list = []
    for child in children:
        params = {**child.emit_params, LAUNCHER_CHILD_EMIT_PARAM: True}
        params.setdefault("session_id", session_id or "")
        params.pop(_DEFER_EMIT_KEY, False)
        note = "Not emitted in the hook: the launcher child emits and starts it."
        emitted.append(_deferred_child_reply(root, child, params, note))
        clean.append(dataclasses.replace(child, emit_params={k: v for k, v in child.emit_params.items() if k != _DEFER_EMIT_KEY}))
    return write_launcher(root, parent_id, clean, session_id, emitted)


def _deferred_child_reply(root: Path, child: Any, params: dict, note: str) -> dict:
    """The launcher's emitted reply for a child whose emit the hook did not finish: a deferred-emit script
    that mints `child.run_id`; the error reply when the script cannot be written."""
    try:
        script = _write_deferred_script(root, params)
    except OSError:
        return {"error": note}
    return {"path": script.relative_to(root).as_posix(), "run_id": child.run_id, "fire_args": {}, "deferred": note}


def _default_fanout(params: dict, repo_root: Path) -> dict:
    """`fanout` held to the emit budget: past it the writer rolls back what it created and the submit is denied,
    so a submission the caller never saw taken leaves no sizing behind."""
    import time

    from coordinator_core.ops.dispatch_emit.ask_fanout import FANOUT_ABORTED, FanoutLedger, fanout

    deadline = time.monotonic() + _caller_budget_s() * _EMIT_BUDGET_SHARE
    ledger = FanoutLedger()
    box: list = []

    def work() -> None:
        try:
            box.append((True, fanout(params, repo_root, lambda: ledger.closed or time.monotonic() > deadline, ledger)))
        except BaseException as exc:
            box.append((False, exc))

    thread = threading.Thread(target=_in_scope(work), daemon=True)
    thread.start()
    thread.join(max(deadline - time.monotonic(), 0.0))
    if thread.is_alive():
        # Removed here, on the hook thread: the worker may not outlive the process to unlink its own writes.
        if ledger.rollback():
            raise RuntimeError(FANOUT_ABORTED)
        thread.join(_FANOUT_TAIL_GRACE_S)
        if thread.is_alive() or not box:
            raise RuntimeError(FANOUT_ABORTED)
    ok, value = box[0]
    if not ok:
        raise value
    return value


def _persist_em_context(root: Path, run_id: str, brief: Any) -> None:
    """Append the refire's EM brief to the run's `notes.jsonl` before any emit or deferral, so the resume
    path carries it whichever script starts the run. A repeat of the same text appends nothing; a run
    directory that is absent or unwritable persists nothing (the brief still rides the emit params)."""
    if not isinstance(brief, str) or not brief.strip():
        return
    import json
    from datetime import datetime, timezone

    from coordinator_core.atomic_append import append_line
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl, run_notes
    from coordinator_core.session.declared_writes import declare_write

    text = brief.strip().encode("utf-8")[: rl.VERBATIM_BYTE_CAP].decode("utf-8", "ignore")
    try:
        dest = rl.run_dir(root, run_id)
        if not dest.is_dir():
            return
        notes = dest / rl.NOTES_FILE
        if any(e["note"].strip() == text for e in run_notes.entries(notes, run_id)):
            return
        line = {"note": text, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        append_line(notes, (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
        declare_write(notes)
    except (OSError, ValueError, rl.LedgerRefused):
        pass


def _declared_writes(root: Path, tool_input: Mapping[str, Any], sizing_doc: Mapping[str, Any]) -> list[str]:
    """`args.writes`, else the cited sizing's plan-spine writes; empty when neither is declared."""
    args = tool_input.get("args")
    writes = args.get("writes") if isinstance(args, dict) else None
    if isinstance(writes, list) and writes and all(isinstance(w, str) and w.strip() for w in writes):
        return [w.strip() for w in writes]
    plan_rel = sizing_doc.get("plan")
    if not isinstance(plan_rel, str) or not plan_rel.strip():
        return []
    from coordinator_core.ops.dispatch_emit.sibling_footprint import inferred_writes

    try:
        spine = _bounded_call(("spine", str(root), plan_rel.strip()), lambda: list(inferred_writes(root, sizing_doc)))
    except OSError:
        return []
    return spine if isinstance(spine, list) else []


FRICTION_LOG_REL = ".coordinator-local/engine-friction.jsonl"


def _record_skipped_overlap_check(root: Path) -> None:
    """Append an engine-friction row naming the live-write overlap check a stalled index read skipped. Never raises."""
    try:
        import json
        from datetime import datetime, timezone

        log = root / FRICTION_LOG_REL
        log.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": "engine-friction",
            "source": "submit_engine_ask._live_write_overlap_deny",
            "event": "live-write overlap check skipped: live-run index read stalled; the run started unchecked",
        }
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _run_row_dead(root: Path, run_id: str) -> bool:
    """True when an index row is permanently stale: its `halted.json` is landed, superseded, closed or
    resumed (a successor owns the work), or its run dir is gone (a ghost). A halted run is resumable, so not dead."""
    import json

    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    try:
        rd = ask_contract.record_dir(root, run_id)
        if not rd.is_dir():
            return True
        marker = json.loads((rd / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(marker, dict) and marker.get("status") in (
        halted_run.STATUS_LANDED, halted_run.STATUS_SUPERSEDED, halted_run.STATUS_CLOSED, halted_run.STATUS_RESUMED,
    )


def _halt_marker_ended(root: Path, run_id: str) -> bool:
    """True when the run holds no write claim: a halted marker, or a dead row (`_run_row_dead`)."""
    import json

    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    if _run_row_dead(root, run_id):
        return True
    try:
        marker = json.loads((ask_contract.record_dir(root, run_id) / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    # The dead statuses were handled above; the marker is read again only to tell `halted` from live.
    return isinstance(marker, dict) and marker.get("status") == halted_run.STATUS_HALTED


def _stage_halted(root: Path, run_id: str) -> bool:
    """True when the run's `run-stage.json` reads `halted`: a halted run holds no write claim."""
    import json

    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

    if not safe_id(run_id):
        return False
    try:
        stage = json.loads((root / RECORD_DIR_ROOT / run_id / RUN_STAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(stage, dict) and stage.get("stage") == "halted"


def _ended_on_evidence(root: Path, run_id: str) -> bool:
    """True when the harness's own evidence shows the run ended though it wrote no `halted.json`
    (a run that refused at the nested-chain start). A run with no live token record reads not ended."""
    from coordinator_core import warp_live

    for record in warp_live._token_records(root):
        if record.get("run_id") == run_id:
            return warp_live.run_ended_on_evidence(root, record)
    return False


def _staged_without_workflow(root: Path, run_id: str) -> bool:
    """True when `run_id` sits at run-stage `staged` and no live token record names it: no workflow
    runs it, so its stage file holds no write claim. A record present defers to `_ended_on_evidence`."""
    import json

    from coordinator_core import warp_live
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

    if not safe_id(run_id):
        return False
    try:
        stage = json.loads((root / RECORD_DIR_ROOT / run_id / RUN_STAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(stage, dict) or stage.get("stage") != "staged":
        return False
    return not any(record.get("run_id") == run_id for record in warp_live._token_records(root))


def _never_ran_without_workflow(root: Path, run_id: str) -> bool:
    """True when `run_id`'s record dir exists but holds no `halted.json`, `run-stage.json` or
    `sibling-status.json` (state or scratch) and no live token record names it and the dir was last
    touched over `_DEAD_OWNER_IDLE_SECONDS` ago: the run never ran, so its index row holds no write claim
    (`dispatch.record_halted_run_landed` refuses the same id as never ran). A younger dir may be mid-mint."""
    import time
    from coordinator_core import warp_live
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import status_rel

    if not safe_id(run_id):
        return False
    try:
        record_dir = ask_contract.record_dir(root, run_id)
        if not record_dir.is_dir():
            return False
        newest = max([record_dir.stat().st_mtime, *(e.stat().st_mtime for e in record_dir.iterdir())])
        if time.time() - newest <= _DEAD_OWNER_IDLE_SECONDS:
            return False
        evidence = (
            *(root / base / run_id / name for base in (ask_contract.RECORD_DIR_ROOT, "scratch/warp")
              for name in (halted_run.HALTED_FILE, ask_contract.RUN_STAGE_FILE)),
            root / status_rel(run_id),
        )
        if any(path.is_file() for path in evidence):
            return False
    except (OSError, ValueError):
        return False
    return not any(record.get("run_id") == run_id for record in warp_live._token_records(root))


_STALE_STAGED_SECONDS = 6 * 3600
# Must equal ops.dispatch_emit.abandoned_runs.TOKENLESS_QUIET_SECONDS (copied, not imported: hook import cost); a drift lets this gate clear a run the reaper still keeps.
_DEAD_OWNER_IDLE_SECONDS = 3600


def _staged_idle_seconds(root: Path, run_id: str) -> Optional[float]:
    """Seconds since a `staged` run's `run-stage.json` `last_activity_at`; None when the stage is not
    `staged` or the stamp is absent or unparseable."""
    import json
    import time

    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE
    from coordinator_core.ops.dispatch_emit.halted_hunk_attribution import _iso_epoch

    if not safe_id(run_id):
        return None
    try:
        stage = json.loads((root / RECORD_DIR_ROOT / run_id / RUN_STAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stage, dict) or stage.get("stage") != "staged":
        return None
    epoch = _iso_epoch(stage.get("last_activity_at"))
    return None if epoch is None else time.time() - epoch


def _stale_token_without_workflow(root: Path, run_id: str) -> bool:
    """True when `run_id` sits at run-stage `staged` idle past `_STALE_STAGED_SECONDS` and its live token
    record joins no launch (`warp_live` records-only read): no workflow runs it, so the token holds no claim.
    Reads records only, never a transcript; a run with launch ids reads live."""
    idle = _staged_idle_seconds(root, run_id)
    if idle is None or idle <= _DEAD_OWNER_IDLE_SECONDS:
        return False
    if idle <= _STALE_STAGED_SECONDS and _orphan_verdict(root, run_id).get("orphaned") is not True:
        return False
    from coordinator_core import warp_live

    for record in warp_live._token_records(root):
        if record.get("run_id") == run_id:
            return not warp_live._record_live(
                root, record, None, None, warp_live._launch_dirs(None), records_only=True
            )
    return False


def _sibling_record_run_stopped(root: Path, run_id: str, deliverable_id: str) -> bool:
    """True when `run_id` stopped between stages (run-stage `staged`) bound to another sizing record of
    `deliverable_id`: a refire citing a different record of the deliverable resumes that run's footprint.
    Reads only this run's record dir and its one bound sizing."""
    import json

    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE
    from coordinator_core.ops.dispatch_emit.halted_run import bound_sizing_rel
    from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, load_sizing

    if not safe_id(run_id):
        return False
    try:
        stage = json.loads((root / RECORD_DIR_ROOT / run_id / RUN_STAGE_FILE).read_text(encoding="utf-8"))
        bound = bound_sizing_rel(root, run_id)
        doc = load_sizing(root, bound) if bound else None
    except (OSError, ValueError, SizingFireRefused):
        return False
    return (
        isinstance(stage, dict)
        and stage.get("stage") == "staged"
        and isinstance(doc, dict)
        and doc.get("deliverable_id") == deliverable_id
    )


_SUCCESSOR_DEPTH = 4


def _orphan_verdict(root: Path, run_id: str) -> dict:
    """`orphan_adopt.orphan_verdict` as seen by no session; `{}` when it cannot be read."""
    from coordinator_core.ops.dispatch_emit.orphan_adopt import orphan_verdict

    try:
        verdict = orphan_verdict(root, run_id, "")
    except (OSError, ValueError):
        return {}
    return verdict if isinstance(verdict, dict) else {}


def _superseded_successor_ended(root: Path, run_id: str, ended: Any, depth: int = _SUCCESSOR_DEPTH) -> bool:
    """True when `run_id` is superseded (orphan_adopt's verdict) and its successor holds no claim:
    the successor's own ended-state decides, followed through a chain of at most `_SUCCESSOR_DEPTH`."""
    verdict = _orphan_verdict(root, run_id)
    successor = verdict.get("superseded_by") if verdict.get("reason") == "superseded" else None
    if not isinstance(successor, str) or depth <= 0:
        return False
    return bool(ended(successor) or _superseded_successor_ended(root, successor, ended, depth - 1))


def _stale_blocker_remedy(root: Path, run_id: str) -> str:
    """The adopt call when `run_id`'s owner session is gone and no later run supersedes it, else empty."""
    from coordinator_core.ops.dispatch_emit.orphan_adopt import adopt_call

    verdict = _orphan_verdict(root, run_id)
    if verdict.get("orphaned") is True and verdict.get("recorded_session"):
        return (
            f" Its owner session is gone; `{adopt_call(run_id, root)}` takes it over or closes it,"
            " and `dispatch.reap_abandoned_runs` retires it once abandoned."
        )
    return ""


def _live_write_overlap_deny(
    root: Path, tool_input: Mapping[str, Any], sizing_doc: Mapping[str, Any], sizing_rel: Optional[str] = None
) -> Optional[str]:
    """Deny text when a single ask's declared writes overlap a live run's staged footprint; else None.

    Reads the live-run index once; a missing or stalled index denies nothing. The halted run a refire of
    `sizing_rel` resumes (a marked halt, else an unmarked staged run) is that refire's own footprint, never a clash."""
    writes = _declared_writes(root, tool_input, sizing_doc)
    if not writes:
        return None
    from coordinator_core.ops.dispatch_emit import live_run_index

    try:
        own = None
        if sizing_rel:
            from coordinator_core.ops.dispatch_emit.halted_run import halted_run_to_resume

            named = _resume_run_id(tool_input)
            own = _bounded_call(
                ("halted", str(root), sizing_rel),
                # Trap: `run_id` only when named; a bare (root, rel) lookup stays callable by test stubs.
                (lambda: halted_run_to_resume(root, sizing_rel, run_id=named)) if named
                else (lambda: halted_run_to_resume(root, sizing_rel)),
            )
        if own is None and sizing_rel:
            # A halt that wrote no halted.json leaves run-stage `staged`; the resume route refires that run.
            own = _bounded_call(("emitted", str(root), sizing_rel), lambda: _unmarked_emitted_run(root, sizing_rel))
        own = own if isinstance(own, str) else None
        index = _bounded_call(
            ("index", str(root)), lambda: live_run_index.read(root, _READ_DEADLINE_MS, exclude_run_id=own)
        )
    except OSError:
        return None
    if not isinstance(index, dict):
        if index is live_run_index.STALLED:
            _record_skipped_overlap_check(root)
        return None
    hits = live_run_index.overlaps(writes, index)
    if hits:
        # The index is pruned in the background; an ended run holds nothing (read per hit only).
        # A fan-out child records its end in sibling-status.json, never halted.json.
        deliverable = sizing_doc.get("deliverable_id") if sizing_rel else None

        def self_ended(rid: str) -> bool:
            return bool(
                _halt_marker_ended(root, rid) or _stage_halted(root, rid) or _status_of(root, rid) in ("halted", "landed")
                or _ended_on_evidence(root, rid) or live_run_index.run_is_abandoned(root, rid)
                or _staged_without_workflow(root, rid)
                or _stale_token_without_workflow(root, rid) or _never_ran_without_workflow(root, rid)
                or (isinstance(deliverable, str) and _sibling_record_run_stopped(root, rid, deliverable))
            )

        def ended(rid: str) -> bool:
            return self_ended(rid) or _superseded_successor_ended(root, rid, self_ended)

        live: dict = {}
        for rid, paths in hits.items():
            verdict = _bounded_call(("ended", str(root), rid), lambda rid=rid: ended(rid))
            if verdict is True:
                if _bounded_call(("dead", str(root), rid), lambda rid=rid: _run_row_dead(root, rid)) is True:
                    from coordinator_core.ops.dispatch_emit import live_run_writes

                    live_run_writes.index_remove(root, rid)
            elif verdict is False:
                live[rid] = paths
            else:
                # Trap: a STALLED liveness read is unknown, not live. Denying on it refused a refire
                # behind a run halted 90 min earlier; an unproven clash denies nothing.
                _record_skipped_overlap_check(root)
        hits = live
    if not hits:
        return None
    run_id, paths = next(iter(sorted(hits.items())))
    shown = ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")
    remedy = _bounded_call(("remedy", str(root), run_id), lambda: _stale_blocker_remedy(root, run_id))
    remedy = remedy if isinstance(remedy, str) else ""
    return f"Run {run_id} is live and writes {shown}, which this ask also writes. Resubmit when {run_id} lands.{remedy}"


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


def _cause(exc: BaseException) -> str:
    """The exception's message, or its class name when the message is empty (a bare TimeoutError)."""
    text = str(exc).strip()
    return text or type(exc).__name__


def _no_run_detail(result: Any) -> str:
    """Why an emit reply carried no run: its findings or error text, else the reply's own shape."""
    if isinstance(result, dict):
        for key in ("findings", "error", "reason", "message"):
            value = result.get(key)
            if value:
                return str(value)
        if result.get("ok") is False:
            return f"ok was false with no findings (reply keys: {', '.join(sorted(result)) or 'none'})"
        return (
            f"the reply carried no path and no findings (reply keys: {', '.join(sorted(result)) or 'none'}); "
            "the engine may have timed out or restarted. Re-submit once."
        )
    if result:
        return str(result)
    return "the engine returned an empty reply (a timeout or an engine restart). Re-submit once."


#: Marker in a child's emit params: its sizing read stalled, so `_default_launch` hands the child to a deferred-emit
#: script without emitting it in the hook. Stripped before the params reach `dispatch.emit`.
_DEFER_EMIT_KEY = "_defer_emit"


def _child_params(
    root: Path,
    tool_input: Mapping[str, Any],
    session_id: Optional[str],
    run: Any,
    call_stop: Any,
    approved: bool,
    sizing_doc: Optional[Mapping[str, Any]] = None,
) -> dict:
    """The `dispatch.emit` params the direct path sends for one fan-out slice; the launcher's emit leg sends these.

    `sizing_doc` is the already-read sizing; None reads it here (and a stalled read raises `_SizingStalled`)."""
    # `dispatch.emit` is repo-unkeyed over the CLI: without target_root a relative sizing_path
    # resolves against the engine's own root, not the submitting repo.
    params: dict = {"session_id": session_id or "", "target_root": Path(root).as_posix()}
    if _has_research_block(_load_sizing_doc(root, run.slice_sizing) if sizing_doc is None else sizing_doc):
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


def _fan_prefire_refusal(root: Path, payload: Mapping[str, Any], ready: Sequence[Any], approved: bool) -> Optional[str]:
    """The refusal text of the checks a multi-ask submission runs once, over every ready child, before the launcher
    is written; None when all hold.

    B1 live-run clash: no ready child's sizing is already worked by a live run. B2 cross-repo writes: every ask's
    declared `writes` stay inside the repo or reach an approved sibling. The child's own `dispatch.emit` runs the
    rest in the launcher child."""
    from types import SimpleNamespace

    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import CrossRepoWriteError, check_cross_repo_writes

    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None
    with warp_live.listing_scope():
        for r in ready:
            live = warp_live.live_run_for_sizing(root, r.slice_sizing, session_id=session_id, transcript_path=transcript)
            if live and live != r.run_id:
                return f"Run {live} is already working {r.slice_sizing}; its completion is your one wake. Do not poll or verify it meanwhile; unrelated work proceeds."
    args = payload["tool_input"].get("args")
    asks = args.get("asks") if isinstance(args, dict) else None
    rows = []
    for i, item in enumerate(asks if isinstance(asks, list) else ()):
        writes = item.get("writes") if isinstance(item, dict) else None
        if isinstance(writes, list):
            rows.append(SimpleNamespace(id=f"asks[{i}]", writes=[w.strip() for w in writes if isinstance(w, str) and w.strip()], writes_under=()))
    try:
        check_cross_repo_writes(rows, root, approved=approved)
    except CrossRepoWriteError as exc:
        return str(exc)
    return None


def _launch_all(
    root: Path,
    payload: Mapping[str, Any],
    fan_reply: Mapping[str, Any],
    ready: Sequence[Any],
    call_stop: Any,
    launch: Optional[Callable[..., Path]],
    approved: bool,
    sizing_root: Optional[Path] = None,
    advisory: str = "",
) -> dict:
    """The rewrite to one launcher script that emits and starts every ready run of an asks submission.

    `sizing_root` is the root the fan-out wrote the child sizings under; each child emit resolves its sizing there."""
    script, refusal = _write_group_launcher(root, payload, fan_reply, ready, call_stop, launch, approved, sizing_root)
    if script is None:
        return _refuse(refusal or "The engine wrote no launcher.")
    return _launch_envelope(root, payload, fan_reply, ready, script, advisory)


def _write_group_launcher(
    root: Path,
    payload: Mapping[str, Any],
    fan_reply: Mapping[str, Any],
    ready: Sequence[Any],
    call_stop: Any,
    launch: Optional[Callable[..., Path]],
    approved: bool,
    sizing_root: Optional[Path] = None,
) -> tuple[Optional[Path], Optional[str]]:
    """(launcher script, None) for `ready` under `root`, or (None, refusal text)."""
    from coordinator_core.ops.dispatch_emit.fan_launcher import LaunchChild

    tool_input = payload["tool_input"]
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    from coordinator_core.ops.dispatch_emit.halt_resume_record import STALLED

    children = []
    docs = _load_sizing_docs(sizing_root or root, [r.slice_sizing for r in ready])
    for r in ready:
        doc = docs[r.slice_sizing]
        if doc is STALLED:
            # The route is unknown, so the background emit reads the sizing itself; the run is minted and the launcher is written.
            params = {**_child_params(sizing_root or root, tool_input, session_id, r, call_stop, approved, {}), _DEFER_EMIT_KEY: True}
        else:
            params = _child_params(sizing_root or root, tool_input, session_id, r, call_stop, approved, doc)
        children.append(LaunchChild(run_id=r.run_id, sizing_rel=r.slice_sizing, emit_params=params))
    parent_id = str(fan_reply["parent_id"])
    refusal = _fan_prefire_refusal(root, payload, ready, approved)
    if refusal:
        return None, refusal
    try:
        return (launch or _default_launch)(root, parent_id, children, session_id), None
    except Exception as exc:  # the launcher's refusal text is the whole message
        return None, f"The engine wrote no launcher: {exc}"


def _launch_envelope(
    root: Path, payload: Mapping[str, Any], fan_reply: Mapping[str, Any], ready: Sequence[Any], script: Any, advisory: str
) -> dict:
    """Bind `script` to each ready run and build the allow/rewrite envelope onto it."""
    from coordinator_core import warp_live

    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None
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
        f"Submitted to the engine as runs {', '.join(started)} under one launcher.{advisory} {tail} {_revise_hint(started[0])}",
    )
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = f"Engine submission: wrote {Path(script).name} this call."
    return envelope


_MULTI_ROOT_JS = r"""export const meta = { name: 'engine-ask-multi-repo', description: 'Starts one launcher per repo of a multi-repo asks submission' }
const LAUNCHERS = __LAUNCHERS__;
const _settled = await parallel(LAUNCHERS.map((scriptPath) => () => workflow({ scriptPath: scriptPath })));
return { launchers: LAUNCHERS.map((scriptPath, i) => ({
  scriptPath: scriptPath,
  result: (_settled[i] === null || _settled[i] === undefined) ? { status: 'failed', error: 'launcher threw' } : _settled[i],
})) };
"""


def _write_multi_root_script(root: Path, launchers: Sequence[Path]) -> Path:
    """A script under `root/scratch/warp/` that starts every per-repo launcher in parallel."""
    import json

    body = _MULTI_ROOT_JS.replace("__LAUNCHERS__", json.dumps([Path(p).as_posix() for p in launchers]))
    script = root / "scratch" / "warp" / f"multi-repo-{uuid.uuid4().hex[:12]}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    return script


def _entry_roots(
    asks: Sequence[Mapping[str, Any]], repo_arg: Optional[str], payload: dict
) -> tuple[list[Optional[str]], list[Path], Optional[str]]:
    """(repo string per entry, resolved root per entry, refusal): an entry's own `repo`, else the top-level one,
    else the session's. One `show_toplevel` per distinct string."""
    strings = [(item.get("repo") or repo_arg or None) for item in asks]
    strings = [s.strip() if isinstance(s, str) else s for s in strings]
    memo: dict = {}
    roots: list[Path] = []
    for i, s in enumerate(strings):
        if s not in memo:
            memo[s] = _repo_root(s, payload)
        root = memo[s]
        if root is None:
            where = s or payload.get("cwd") or "the session cwd"
            return strings, roots, f"asks[{i}]: {where} is in no git repo. Name an absolute `repo`."
        roots.append(root)
    return strings, roots, None


def _asks_by_root(roots: Sequence[Path]) -> dict:
    """Entry indices grouped by resolved root, in first-seen order."""
    groups: dict = {}
    for i, root in enumerate(roots):
        groups.setdefault(str(root.resolve()), []).append(i)
    return groups


def _multi_root_asks(
    payload: dict,
    asks: list,
    strings: Sequence[Optional[str]],
    roots: Sequence[Path],
    join: Optional[dict],
    supersedes: Optional[str],
    call_stop: Any,
    fanout: Optional[FanoutFn],
    launch: Optional[Callable[..., Path]],
) -> dict:
    """The rewrite for an asks submission whose entries resolve to more than one repo.

    Every refusal that needs no fan-out reply (cross-repo, missing context, `after` or `join` across repos,
    declared writes) runs for all groups before any group's fan-out. Each group then fans out once under its own
    root and gets its own launcher; one script starts the launchers in parallel."""
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import is_remote_venue

    if join is not None:
        return _refuse(f"A join has one writer root, but these asks resolve to {len(set(map(str, roots)))} repos. Join runs of one repo.")
    if supersedes:
        return _refuse("supersedes rides one ask or sizing, not an asks fan-out across repos.")
    groups = _asks_by_root(roots)
    group_of = {i: key for key, members in groups.items() for i in members}
    labels = [(item.get("label") or "").strip() or f"slice-{k + 1}" for k, item in enumerate(asks)]
    for i, item in enumerate(asks):
        if item.get("after") is None:
            continue
        try:
            preds = resolve_after(f"asks[{i}]", item["after"], labels, i)
        except ValueError as exc:
            return _refuse(str(exc))
        for k in preds:
            if group_of[k] != group_of[i]:
                return _refuse(
                    f"asks[{i}].after names asks[{k}], but asks[{i}] runs in {roots[i]} and asks[{k}] in {roots[k]}. "
                    "An after edge stays within one repo."
                )

    call_args = payload["tool_input"].get("args")
    own = _repo_root(None, payload)
    base_ok = is_remote_venue() or (isinstance(call_args, dict) and call_args.get("cross_repo_approved") is True)
    approved_of: dict = {}
    for key, members in groups.items():
        root = roots[members[0]]
        _note_root(root)
        approved_of[key] = base_ok or _names_registered_sibling(strings[members[0]], root)
        if own is not None and root.resolve() != own.resolve():
            cross = _cross_repo_refusal(root, own, False, approved_of[key])
            if cross:
                return _refuse(cross)

    notes: list[str] = []
    for i, item in enumerate(asks):
        paths = [c.strip() for c in _context_items(item.get("context") or [], "context")[0] if is_path_shaped(c)]
        missing = _missing_context(roots[i], paths)
        if missing:
            return _refuse(f"asks[{i}].context {missing}: no such file. Pass inline prose as context text, or fix the path.")
        notes += [f"asks[{i}] {n}" for n in _resolved_memos(roots[i], paths)]
    advisory = f" Advisory: context memo {'; '.join(notes)}. The work may already be done." if notes else ""

    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    payloads: dict = {}
    fan_items: dict = {}
    for key, members in groups.items():
        local = {g: n for n, g in enumerate(members)}
        items = []
        for g in members:
            item = {k: v for k, v in asks[g].items() if k not in (_CHATTY_KEY, "repo")}
            if item.get("after") is not None:
                item["after"] = [local[k] for k in resolve_after(f"asks[{g}]", asks[g]["after"], labels, g)]
            items.append(item)
        fan_items[key] = items
        sub_args = {**call_args, "asks": [asks[g] for g in members]}
        payloads[key] = {**payload, "tool_input": {**payload["tool_input"], "args": sub_args}}
        why = _fan_prefire_refusal(roots[members[0]], payloads[key], [], approved_of[key])
        if why:
            return _refuse(why)

    scripts: list[Path] = []
    started: list[str] = []
    tails: list[str] = []
    recorded: list[tuple[Path, Any, Any]] = []
    written: list[str] = []

    def _late(text: str, *, refuse: bool = False) -> dict:
        # Earlier groups already fanned out: their parent records stay on disk with no launcher started.
        left = f" Already written by earlier groups, no launcher started: parents {', '.join(written)}." if written else ""
        return _refuse(text + left) if refuse else deny("PreToolUse", text + left)

    for key, members in groups.items():
        root = roots[members[0]]
        fan_params: dict = {"asks": fan_items[key], "session_id": session_id or ""}
        if own is not None and root.resolve() != own.resolve():
            fan_params["session_root"] = str(own)
        if isinstance(call_args.get("sender"), str):
            fan_params["sender"] = call_args["sender"]
        if call_args.get("confirm_split") is True:
            fan_params["confirm_split"] = True
        try:
            reply = (fanout or _default_fanout)(fan_params, root)
        except Exception as exc:  # the engine's refusal text is the whole message
            return _late(_cause(exc))
        if not isinstance(reply, dict):
            return _late(f"The fan-out returned no reply: {reply!r}.")
        if reply.get("error") or reply.get("exit_code") or reply.get("applied") is False:
            return _late(str(reply.get("error") or "The fan-out was refused."))
        written.append(str(reply.get("parent_id")))
        writer_root = Path(reply["writer_root"]) if isinstance(reply.get("writer_root"), str) else root
        parent = _read_parent(writer_root, str(reply.get("parent_id")))
        ready = [r for r in (parent.runs if parent else ()) if r.status == "ready"]
        if not ready:
            return _late(f"The fan-out under {root} produced no ready run to fire.")
        unwritten = next((r.slice_sizing for r in ready if not (writer_root / r.slice_sizing).is_file()), None)
        if unwritten:
            return _late(f"The fan-out reported {unwritten} written under {writer_root}, but it is not on disk there.")
        script, why = _write_group_launcher(
            root, payloads[key], reply, ready, call_stop, launch, approved_of[key], writer_root
        )
        if script is None:
            return _late(why or "The engine wrote no launcher.", refuse=True)
        scripts.append(Path(script))
        ids = [r.run_id for r in ready]
        started += ids
        tails.append(f"{root}: {_fan_tail(root, reply, ids, launcher=True)}")
        recorded.append((root, ready, script))

    first_root = roots[next(iter(groups.values()))[0]]
    entry = _write_multi_root_script(first_root, scripts)
    call_id = payload.get("tool_use_id")
    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None
    with contextlib.suppress(OSError):
        for root, ready, _script in recorded:
            for r in ready:
                warp_live.record_submission(
                    root,
                    r.run_id,
                    session_id=session_id,
                    transcript_path=transcript,
                    script_path=str(entry),
                    sizing_rel=r.slice_sizing,
                    tool_use_id=call_id if isinstance(call_id, str) else None,
                )
    envelope = rewrite_input(
        "PreToolUse",
        {"scriptPath": str(entry)},
        f"Submitted to the engine as runs {', '.join(started)} under {len(scripts)} launchers, one per repo.{advisory} "
        f"{' '.join(tails)} {_revise_hint(started[0])}",
    )
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = f"Engine submission: wrote {entry.name} this call."
    return envelope


def _reusable_envelope(
    root: Path,
    payload: Mapping[str, Any],
    fan_params: Mapping[str, Any],
    advisory: str,
    call_stop: Any = None,
    launch: Optional[Callable[..., Path]] = None,
    approved: bool = False,
) -> Optional[dict]:
    """Reap aged unstarted parents, then the rewrite onto the launcher of a minted-not-started parent for the
    same submission; a parent minted with no launcher gets one written over its minted slices. None mints a fresh
    parent. Both index reads are budget-bounded and fail open."""
    import time

    from coordinator_core.ops.dispatch_emit import minted_parent

    now = time.time()
    digest = minted_parent.asks_digest(fan_params)
    try:
        _bounded_call(("reap", str(root)), lambda: minted_parent.reap_orphans(root, now))
        hit = _bounded_call(("reuse", str(root), digest), lambda: minted_parent.find_reusable(root, digest, now))
    except Exception:  # reuse is an optimisation: any read fault mints fresh
        return None
    if not isinstance(hit, minted_parent.Reusable):
        return None
    parent = _read_parent(root, hit.parent_id)
    wanted = {run_id for run_id, _ in hit.ready}
    ready = [r for r in (parent.runs if parent else ()) if r.run_id in wanted]
    if not ready:
        return None
    if hit.launcher is None:
        note = f" Re-submission of {hit.parent_id}: its minted slices are reused with a fresh launcher; nothing new minted."
        from coordinator_core.ops.fleet._common import main_worktree_root

        sizing_root = main_worktree_root(root)
        if any(not (sizing_root / r.slice_sizing).is_file() for r in ready):
            return None
        return _launch_all(root, payload, {"parent_id": hit.parent_id}, ready, call_stop, launch, approved, sizing_root, note + advisory)
    note = f" Re-submission of {hit.parent_id}: its unstarted launcher is reused; nothing new minted."
    return _launch_envelope(root, payload, {"parent_id": hit.parent_id}, ready, hit.launcher, note + advisory)


def _spawn_recompose(root: Path, run_id: str) -> bool:
    """Start `halt_resume_build` for `run_id` detached; False when it could not start (never raises)."""
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    script = Path(__file__).resolve().parent.parent / "ops" / "dispatch_emit" / "halt_resume_build.py"
    try:
        return bool(spawn_detached(str(root), str(script), [str(root), run_id]))
    except Exception:
        return False


_LOG_REL = "state/housekeeping-failures.log"


def _prior_recompose_failure(root: Path) -> Optional[str]:
    """The newest `halt_resume_build` line of the housekeeping-failures log (a `SPAWN FAILED` or `CHILD
    FAILED` record), else None. A non-destructive read; the log is shared, so the line may name another run."""
    from coordinator_core.ops.ceremony.detached_spawn import read_failures_log

    try:
        text = read_failures_log(str(root))
    except Exception:
        return None
    for line in reversed(text.splitlines()):
        if "halt_resume_build" in line and ("CHILD FAILED" in line or "SPAWN FAILED" in line):
            return line.strip()[:300]
    return None


def _recompose_note(spawn: str, refusal: Optional[str], prior_failure: Optional[str]) -> str:
    """The sentence of the refire envelope telling the EM what became of the halt-time record.

    `spawn` is `started`, `failed` (the detached build could not start), `inflight` (an earlier
    recompose still holds the claim) or `unused` (no recompose was called for)."""
    head = {
        "started": "the halt-time record is missing or stale, so a background recompose was started and the run starts now.",
        "failed": "the halt-time record is missing or stale and the background recompose could NOT start, so "
        "the run starts now without a rebuilt script.",
        "inflight": "the halt-time record is missing or stale and an earlier background recompose is still "
        "running, so none was started and the run starts now.",
        "unused": "the stored script is not used (the call carries its own parameters, or the record holds "
        "none) and no background recompose was started.",
    }[spawn]
    parts = [head]
    if refusal is not None:
        parts.append(f"The stored record holds a refusal: {refusal}")
    if prior_failure is not None:
        parts.append(f"A recompose failure is on record in {_LOG_REL}: {prior_failure}")
    return " ".join(parts)

#: Params every refire carries; any other key is the call's own (stop_after, brief, chatty, writes,
#: cross-repo approval) and the halt-time script cannot honour it, so such a call goes through the launcher.
_RESUME_NEUTRAL_PARAMS = frozenset({"session_id", "ask", "sizing_path", "reserved_run_id"})


def _stored_refusal(record: dict) -> Optional[str]:
    """The refusal text of a record whose build was refused (`gate_verdict` carries `halt` and/or
    `refused`), else None."""
    verdict = record.get("gate_verdict")
    if not isinstance(verdict, dict):
        return None
    halt = verdict.get("halt")
    if isinstance(halt, dict):
        return f"The engine refused the submission: {halt.get('kind')}: {halt.get('reason')}"
    if verdict.get("refused"):
        return f"The engine refused the submission: {verdict.get('refused')}"
    return None


def _unmarked_emitted_run(root: Path, sizing_rel: str) -> Optional[str]:
    """The newest `warp-<stem>-*` run bound to `sizing_rel` that stopped between stages without a halt
    marker (run.json bound, run-stage `staged` or `halted`, no halted.json), else None. One scandir and
    three small reads, no git spawn; a landed, minted-only or marked run is not one."""
    import json

    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE
    from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, _norm
    from coordinator_core.ops.dispatch_emit.requirements_ledger import read_run

    norm = _norm(sizing_rel)
    prefix = f"warp-{Path(norm).stem}-"
    base = root / RECORD_DIR_ROOT
    with os.scandir(base) as it:
        names = sorted((e.name for e in it if e.name.startswith(prefix) and e.is_dir()), reverse=True)
    for name in names:
        rd = base / name
        if not safe_id(name) or (rd / HALTED_FILE).exists():
            return None
        try:
            stage = json.loads((rd / RUN_STAGE_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # a minted-only shell (e.g. a timed-out refire) hides no older run
        except (OSError, ValueError):
            return None
        kind = stage.get("stage") if isinstance(stage, dict) else None
        if kind in (None, "minted"):
            continue
        run = read_run(rd)
        if run is None or run.get("sizing_rel") != norm:
            return None
        return name if kind in ("staged", "halted") else None
    return None


def _adopt_unmarked_run(root: Path, run_id: str, session_id: str) -> None:
    """Hand an unmarked staged run to the refiring session when its owner session is gone: the halt
    marker first, then the run-stage rewrite. A live owner, the refiring session itself or a failed
    write leaves the run as it was; the resume proceeds either way."""
    from coordinator_core.ops.dispatch_emit.orphan_adopt import adopt_orphaned_run

    try:
        adopt_orphaned_run(root, run_id, session_id)
    except (OSError, ValueError):
        pass


def _resume_fire_args(root: Path, run_id: str, sizing_rel: str, session_id: Optional[str]) -> dict:
    """`{"fire_args": ...}` handing the stored resume script a run token, else `{}`.

    The stored script is composed with `script_path` `<RUN_DIR_ROOT>/<run_id>.workflow.mjs` (not the
    stored file's path), and its commit leg presents that path to `run_token.admits`, so the token
    binds to it. A mint that cannot bind returns `{}`: the run hands its commit back, as before."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    state, token = _write_leg(
        "run_token.mint",
        lambda: run_token.mint(
            root, run_id, f"{RUN_DIR_ROOT}/{run_id}.workflow.mjs", session_id=session_id, sizing_rel=sizing_rel
        ),
        gated=True,
    )
    if state == "detached":
        return {"detached": ["run_token.mint"]}
    if state == "raised":
        if isinstance(token, (OSError, ValueError)):
            return {}
        raise token
    return {"fire_args": {"repoRoot": root.as_posix(), "runToken": token}}


def _ensure_launcher_run_token(root: Path, run_id: str, sizing_rel: str, session_id: Optional[str]) -> None:
    """Leave the resumed run holding a live escrowed token the launcher's staged twin inlines.

    The child emit escrows one; a launcher whose child emit was deferred, or a `launch` that
    escrowed nothing, would start a run whose commit presents a spent or absent token. An
    escrowed live token is left alone; otherwise one is minted for the commit leg's script path
    (`carry_prior` keeps a token a running script holds) and escrowed. A mint that cannot bind
    leaves the run handing its commit back, as before. The check runs inside the gated write leg,
    which the remaining hook window bounds: a stalled box detaches the mint and still starts the run."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    def ensure() -> None:
        if run_token.escrowed(root, run_id) is not None:
            return
        token = run_token.mint(
            root, run_id, f"{RUN_DIR_ROOT}/{run_id}.workflow.mjs",
            session_id=session_id, sizing_rel=sizing_rel, carry_prior=True,
        )
        run_token.escrow(root, run_id, token)

    if _wait_ms(None) <= 0:
        return
    state, err = _write_leg("run_token.mint", ensure, gated=True)
    if state == "raised" and not isinstance(err, (OSError, ValueError)):
        raise err


def _record_refire(rd: Path, script, kind: str, composed_by: Optional[str]) -> None:
    """Best-effort per-refire launch record in the run dir; a failed write never blocks the refire."""
    import time

    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr

    try:
        hrr.write_refire_record(rd, script, kind, composed_by, time.time())
    except OSError:
        pass


def _resume_route(
    root: Path,
    sizing_rel: str,
    payload: Mapping[str, Any],
    params: dict,
    session_id: Optional[str],
    launch: Optional[Callable[..., Path]],
    reserved_run_id: Optional[str] = None,
    ask_parent_id: Optional[str] = None,
) -> tuple[Optional[dict], Optional[dict]]:
    """(envelope, emit result) for a refire of a halted sizing, else (None, None).

    A halted marker for the sizing wins over a fan-out slice's reserved run id: the halted run
    resumes, and the revisions queued under the reserved or parent ask id are carried into its record.

    A stored script whose fingerprint matches is the emit result. A missing, stale or stalled
    record spawns the detached recompose and answers a one-child launcher envelope. A halted-run
    lookup that finds nothing is (None, None): the plain emit follows. One that stalls takes the run id
    from the halt-time hint (`hrr.read_hint`); with no hint the submission is deferred, and the deferred emit resolves the halted run off the hook."""
    import time

    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.halted_run import halted_run_to_resume

    named_id = _resume_run_id(payload.get("tool_input") or {})
    try:
        halted_id = _bounded_call(
            ("halted", str(root), sizing_rel),
            lambda: halted_run_to_resume(root, sizing_rel, build=False, run_id=named_id),
        )
    except (OSError, ValueError):
        return None, None
    if halted_id is hrr.STALLED:
        halted_id = hrr.read_hint(root, sizing_rel)
        if halted_id is None:
            # The deferred emit resolves the halted run off the hook path through the same lookup; when
            # its script cannot be written the plain emit follows (None, None), never a refusal.
            return _taken_as_deferred(root, params), None
    if halted_id is None and reserved_run_id is not None:
        # A lookup-index miss must not hand a chained sizing to its parent's stale script: the
        # `warp-<stem>-*` listing (one scandir, no index) still names a halt the index lacks.
        from coordinator_core.ops.dispatch_emit.halted_run import halted_run_for

        try:
            halted_id = _bounded_call(("halted-listing", str(root), sizing_rel), lambda: halted_run_for(root, sizing_rel))
        except (OSError, ValueError):
            return None, None
    if halted_id is None and reserved_run_id is None:
        try:
            halted_id = _bounded_call(("emitted", str(root), sizing_rel), lambda: _unmarked_emitted_run(root, sizing_rel))
        except (OSError, ValueError):
            return None, None
        if halted_id is hrr.STALLED:
            return _taken_as_deferred(root, params), None
        if isinstance(halted_id, str) and session_id:
            _adopt_unmarked_run(root, halted_id, session_id)
    if not isinstance(halted_id, str):
        return None, None
    _note_resume(halted_id)
    rd = record_dir(root, halted_id)
    filed_under = [i for i in (reserved_run_id, ask_parent_id) if isinstance(i, str) and i != halted_id]
    if filed_under:
        from coordinator_core.ops.dispatch_emit.requirements_ledger import carry_inbox

        try:
            carry_inbox(root, filed_under, halted_id)
        except OSError:
            pass  # the resume proceeds; the queued revisions stay under their own id
    _persist_em_context(root, halted_id, params.get("ask_brief"))

    stored: Optional[dict] = None
    record = _budgeted_read(lambda wait: hrr.read(rd, wait))
    if isinstance(record, dict):
        try:
            current = _bounded_call(
                ("fingerprint", str(root), halted_id),
                lambda: hrr.fingerprint_for_run(root, rd, sizing_rel, hrr.resolve_fingerprint_plan(root, rd, sizing_rel)),
            )
        except OSError:
            current = None
        if isinstance(current, dict) and hrr.matches(record, current):
            stored = record
    call_specific = any(k not in _RESUME_NEUTRAL_PARAMS for k in params)
    script_rel = stored.get("composed_script_rel") if stored else None
    refusal = _stored_refusal(stored) if stored else None
    refused_in_backoff = refusal is not None and hrr.in_backoff(stored, time.time())
    if refused_in_backoff:
        return _refuse(refusal), None
    script_missing = False
    if isinstance(script_rel, str) and script_rel and not call_specific:
        script = root / script_rel
        try:
            on_disk = _bounded_call(("script", str(script)), script.is_file)
        except OSError:
            on_disk = False
        if on_disk is True:
            if refusal is not None:
                return _refuse(refusal), None
            _record_refire(rd, script, "stored", (stored.get("fingerprint") or {}).get("engine_sha"))
            return None, {"path": str(script), "run_id": halted_id, **_resume_fire_args(root, halted_id, sizing_rel, session_id)}
        script_missing = refusal is None and on_disk is False

    spawn = "unused"
    if stored is None or refusal is not None or script_missing:
        if not hrr.claim_recompose(rd, time.time(), hrr.RECOMPOSE_TTL_S):
            spawn = "inflight"
        elif _spawn_recompose(root, halted_id):
            spawn = "started"
        else:
            hrr.release_recompose(rd)  # a claim with no builder behind it would block the next refire for the TTL
            spawn = "failed"
    from coordinator_core.ops.dispatch_emit.fan_launcher import LaunchChild

    child = LaunchChild(run_id=halted_id, sizing_rel=sizing_rel, emit_params={**params, "target_root": str(root)})
    parent_id = f"resume-{halted_id}-{uuid.uuid4().hex[:6]}"
    try:
        script = (launch or _default_launch)(root, parent_id, [child], session_id)
    except Exception as exc:  # the launcher's refusal text is the whole message
        return _refuse(f"The engine wrote no launcher: {exc}"), None
    _ensure_launcher_run_token(root, halted_id, sizing_rel, session_id)
    if call_specific and _spent_after_ensure(root, halted_id):
        # A call carrying `writes` recomposes the run, and a recomposed gate refuses a run whose token is not live;
        # retire the halt so the plain emit below mints a fresh run id and token.
        from coordinator_core.ops.dispatch_emit.halted_run import mark_resumed

        mark_resumed(rd)
        return None, None
    _record_refire(rd, script, "launcher", None)
    from coordinator_core import warp_live

    call_id = payload.get("tool_use_id")
    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None
    try:
        warp_live.record_submission(
            root,
            halted_id,
            session_id=session_id,
            transcript_path=transcript,
            script_path=str(script),
            sizing_rel=sizing_rel,
            tool_use_id=call_id if isinstance(call_id, str) else None,
        )
    except OSError:
        pass  # liveness then reads the run token alone
    envelope = rewrite_input(
        "PreToolUse",
        {"scriptPath": str(script)},
        f"Submitted to the engine as run {halted_id} under one launcher: "
        f"{_recompose_note(spawn, refusal, _prior_recompose_failure(root))} "
        f"It runs to its commit in the background; its completion notification is your one wake. "
        f"Do not poll or verify this run meanwhile; unrelated work proceeds. {_revise_hint(halted_id)}",
    )
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = f"Engine submission: wrote {Path(script).name} this call."
    return envelope, None


REFUSAL_LOG_REL = ".coordinator-local/engine-ask-refusals.jsonl"
_LEAD_CHARS = 120
_DENY_CAP = 280
_REFUSAL_LOG_CAP_BYTES = 1 << 20  # past this the log keeps its newest half, whole lines only


def _durable_refusal(payload: Mapping[str, Any], envelope: dict) -> dict:
    """Append the whole refusal to the repo's refusal log and lead the deny with its cause and that path.

    A downstream surface caps the deny near 280 characters; the lead keeps cause and path inside it, the
    full text follows. A log failure leaves the deny as it was."""
    hso = envelope.get("hookSpecificOutput") if isinstance(envelope, dict) else None
    if not isinstance(hso, dict) or hso.get("permissionDecision") != "deny":
        return envelope
    full = str(hso.get("permissionDecisionReason") or "")
    if not full:
        return envelope
    try:
        import json
        from datetime import datetime, timezone

        root = _repo_root(None, dict(payload))
        if root is None:
            cwd = payload.get("cwd")
            root = Path(cwd) if isinstance(cwd, str) and cwd else None
        if root is None:
            return envelope
        log = root / REFUSAL_LOG_REL
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.is_file() and log.stat().st_size > _REFUSAL_LOG_CAP_BYTES:
            from coordinator_core.atomic_replace import atomic_write_bytes

            # Atomic so a crash never empties the log; an append racing the trim can lose its one line.
            tail = log.read_bytes()[-(_REFUSAL_LOG_CAP_BYTES // 2):]
            atomic_write_bytes(log, tail[tail.find(b"\n") + 1:])
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "session_id": payload.get("session_id"),
                "refusal": full,
            },
            ensure_ascii=False,
        )
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        return envelope
    if len(full) <= _DENY_CAP:
        return envelope
    cause = full.split("\nInstead:")[0].strip().replace("\n", " ")
    lead = cause if len(cause) <= _LEAD_CHARS else cause[: _LEAD_CHARS - 3] + "..."
    tail = full[full.find("\nInstead:"):] if lead == cause and "\nInstead:" in full else f"\n{full}"
    hso["permissionDecisionReason"] = f"{lead} [full text: {REFUSAL_LOG_REL}]{tail}"
    return envelope


def evaluate(
    payload: Mapping[str, Any],
    *,
    emit: Optional[EmitFn] = None,
    fanout: Optional[FanoutFn] = None,
    launch: Optional[Callable[..., Path]] = None,
) -> dict:
    """The PreToolUse envelope for a Workflow call: the fire rewrite for a submission, else nothing.

    A refusal is logged whole and leads with its cause (see `_durable_refusal`)."""
    global _BUDGET
    import time

    _BUDGET = [_READ_DEADLINE_MS]
    try:
        if is_submission(payload):
            from coordinator_core.ops.dispatch_emit.run_token import WriteGate
            from coordinator_core.ops.dispatch_emit.sizing_fire import sizing_memo

            session = payload.get("session_id")
            call = {
                "t0": time.monotonic(), "wall0": time.time(), "roots": [], "minted": [], "gate": WriteGate(), "gates": [],
                "session": session if isinstance(session, str) and session else None, "payload": payload,
            }
            box: dict = {}

            def body() -> None:
                _CALL.set(call)
                try:
                    with sizing_memo():  # the hook's and every child emit's reads of one slice sizing parse it once
                        box["envelope"] = _evaluate(payload, emit=emit, fanout=fanout, launch=launch)
                except BaseException as exc:  # re-raised on the caller's thread
                    box["error"] = exc

            worker = threading.Thread(target=_in_scope(body), daemon=True)
            worker.start()
            worker.join(_caller_budget_s() * _ANSWER_SHARE)
            if worker.is_alive():
                envelope = _answer_overran(call)
            elif "error" in box:
                raise box["error"]
            else:
                envelope = box["envelope"]
        else:
            envelope = _evaluate(payload, emit=emit, fanout=fanout, launch=launch)
    finally:
        _BUDGET = None
    if is_submission(payload):
        return _durable_refusal(payload, envelope)
    return envelope


def _evaluate(
    payload: Mapping[str, Any],
    *,
    emit: Optional[EmitFn] = None,
    fanout: Optional[FanoutFn] = None,
    launch: Optional[Callable[..., Path]] = None,
) -> dict:
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
    join, join_refusal = _join(payload["tool_input"])
    if join_refusal:
        return _refuse(join_refusal)
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
    em_size, em_size_refusal = _em_size_param(payload["tool_input"], asks is not None and len(asks) > 1)
    if em_size_refusal:
        return _refuse(em_size_refusal)
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
        if len(asks) == 1 and join is None and not (
            asks[0].get("sender") or asks[0].get("relayed_text") or payload["tool_input"]["args"].get("sender")
        ):
            ask, source = asks[0]["ask"], asks[0].get("source")
            if asks[0].get("repo"):
                repo_arg = asks[0]["repo"].strip()
            route_pref, em_rationale = asks[0].get("route_preference"), asks[0].get("em_rationale")
            call_stop = asks[0].get("stop_after")
            if asks[0].get(_CHATTY_KEY) is not None:
                call_chatty = asks[0][_CHATTY_KEY]
            item_em_context = asks[0].get("em_context")
            item_context, _ = _context_items(asks[0].get("context") or [], "context")
            asks = None

    repo_arg, key_refusal = _registry_repo(repo_arg)
    if key_refusal:
        return _refuse(key_refusal)
    if asks is not None:
        resolved_asks = []
        for i, item in enumerate(asks):
            value, key_refusal = _registry_repo(item.get("repo"))
            if key_refusal:
                return _refuse(f"asks[{i}]: {key_refusal}")
            resolved_asks.append({**item, "repo": value} if item.get("repo") else item)
        asks = resolved_asks

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
    _add_context(brief, item_context)
    if isinstance(item_em_context, str) and item_em_context.strip():
        brief["ask_brief"] = with_em_context(brief.get("ask_brief"), item_em_context)
        brief[EM_CONTEXT_DIGEST_PARAM] = em_context_digest(item_em_context)
    if asks is not None:
        if "ask_brief" in brief or "ask_brief_file" in brief:
            return _refuse("A brief rides one run, not a set of sibling runs. Put each run's directives in its ask text.")

    if sizing_rel is not None and repo_arg is None and Path(sizing_rel).is_absolute():
        sizing_rel, repo_arg, home_refusal = _sizing_home(sizing_rel)
        if home_refusal:
            return _refuse(home_refusal)

    names_all = asks is not None and any(it.get("repo") for it in asks) and all(
        isinstance(s, str) and Path(s.strip()).is_absolute() for s in ((it.get("repo") or repo_arg) for it in asks)
    )
    disagree = None if ((repo_arg and Path(repo_arg).is_absolute()) or names_all) else _anchor_disagreement(payload)
    if disagree:
        return _refuse(
            f"Session anchor {disagree[0]} and launch repo {disagree[1]} are different repos; "
            "the run would resolve to the anchor. Pass an absolute `repo` to choose one."
        )

    if asks is not None and any(it.get("repo") for it in asks):
        strings, entry_roots, why = _entry_roots(asks, repo_arg, payload)
        if why:
            return _refuse(why)
        if len(_asks_by_root(entry_roots)) > 1:
            return _multi_root_asks(payload, asks, strings, entry_roots, join, supersedes, call_stop, fanout, launch)
        asks = [{k: v for k, v in it.items() if k != "repo"} for it in asks]
        repo_arg = repo_arg or next(s for s in strings if s)

    root = _repo_root(repo_arg, payload)
    if root is None:
        where = repo_arg or payload.get("cwd") or "the session cwd"
        return _refuse(f"{where} is in no git repo. Add `repo: \"<abs path>\"` to args.")

    target_root = root
    _note_root(root)
    if (ask is not None or asks is not None) and repo_arg is None and brief.get("ask_context"):
        root = _context_home(root, brief["ask_context"])
        _note_root(root)
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
    resolved_notes: list[str] = []
    for i, item in enumerate(asks or ()):
        entry_paths = [c.strip() for c in _context_items(item.get("context") or [], "context")[0] if is_path_shaped(c)]
        missing = _missing_context(root, entry_paths)
        if missing:
            return _refuse(f"asks[{i}].context {missing}: no such file. Pass inline prose as context text, or fix the path.")
        resolved_notes += [f"asks[{i}] {n}" for n in _resolved_memos(root, entry_paths)]
    resolved_notes += _resolved_memos(root, brief.get("ask_context", []))
    memo_advisory = (
        f" Advisory: context memo {'; '.join(resolved_notes)}. The work may already be done." if resolved_notes else ""
    )

    if supersedes:
        from coordinator_core.ops.dispatch_emit.sibling_hold import supersede_refusal

        refusal = supersede_refusal(root, supersedes, "pending-supersessor")
        if refusal:
            return _refuse(refusal)

    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None

    from coordinator_core import warp_live

    fan_reply: Optional[dict] = None
    reserved_run_id: Optional[str] = None
    ask_parent_id: Optional[str] = None
    if asks is not None:
        fan_asks = [{k: v for k, v in item.items() if k != _CHATTY_KEY} for item in asks]
        fan_params: dict = {"asks": fan_asks, "session_id": session_id or ""}
        if session_root:
            fan_params["session_root"] = session_root
        if join is not None:
            fan_params["join"] = join
        if isinstance(payload["tool_input"]["args"].get("sender"), str):
            fan_params["sender"] = payload["tool_input"]["args"]["sender"]
        if payload["tool_input"]["args"].get("confirm_split") is True:
            fan_params["confirm_split"] = True
        if fanout is None:
            reused = _reusable_envelope(root, payload, fan_params, memo_advisory, call_stop, launch, approved)
            if reused is not None:
                return reused
        try:
            fan_reply = (fanout or _default_fanout)(fan_params, root)
        except Exception as exc:  # the engine's refusal text is the whole message
            return deny("PreToolUse", _cause(exc))
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
        _note_entries(_CALL.get(), asks, ready)
        if len(ready) >= 2:
            return _launch_all(root, payload, fan_reply, ready, call_stop, launch, approved, writer_root, memo_advisory)
        first = ready[0]
        # Trap: rebinding moves every later `root` use (cross-repo, live-run, overlap, emit target_root), not only sizing.
        root = writer_root
        _note_root(root)
        sizing_rel, reserved_run_id = first.slice_sizing, first.run_id

    if not sizing_rel and ask and asks is None:
        with warp_live.listing_scope():
            held = warp_live.run_for_ask(
                root, normalized_ask_hash(ask), session_id=session_id, transcript_path=transcript
            )
        if held is not None:
            held_state, held_run, held_sizing = held
            if held_state == "live":
                return deny(
                    "PreToolUse",
                    f"Run {held_run} is already working this ask; its completion is your one wake. "
                    "Do not poll or verify it meanwhile; unrelated work proceeds.",
                )
            if held_sizing:
                sizing_rel = held_sizing

    try:
        sizing_doc = _load_sizing_doc(root, sizing_rel) if sizing_rel else {}
    except _SizingStalled:
        from coordinator_core.ops.dispatch_emit import halt_resume_record as _hrr

        hinted = None if asks is not None else _hrr.read_hint(root, sizing_rel)
        if hinted is None:
            return _refuse(f"The sizing read of {sizing_rel} stalled; its ask_parent marker is unknown. Re-submit once.")
        sizing_doc, reserved_run_id = {}, hinted
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
            if not _reserved_token_spent(root, marker[1]):
                reserved_run_id = marker[1]
            ask_parent_id = marker[0]
    if reserved_run_id and _reserved_token_spent(root, reserved_run_id):
        reserved_run_id = None

    clash = None
    with warp_live.listing_scope():
        if sizing_rel:
            from coordinator_core.ops.dispatch_emit import run_token

            stale = warp_live.unlaunched_run_for_sizing(root, sizing_rel, session_id)
            if stale:
                _write_or_detach("run_token.consume", lambda: run_token.consume(root, stale))
            live = warp_live.live_run_for_sizing(
                root, sizing_rel, session_id=session_id, transcript_path=transcript
            )
            stranded = _bounded_call(
                ("runner_less", str(root), sizing_rel), lambda: _runner_less_run(root, sizing_rel, live)
            )
            if not isinstance(stranded, str):
                stranded = None
            if stranded:
                import time

                return _minted_start_envelope(root, [stranded], time.monotonic() + _ENVELOPE_GRACE_S)
            if live:
                return deny(
                    "PreToolUse",
                    f"Run {live} is already working {sizing_rel}; its completion is your one wake. "
                    "Do not poll or verify it meanwhile; unrelated work proceeds.",
                )
        if asks is None and reserved_run_id is None:
            clash = _live_write_overlap_deny(root, payload["tool_input"], sizing_doc, sizing_rel)
    if clash:
        return deny("PreToolUse", clash)

    retire_note = ""
    if sizing_rel and reserved_run_id is None:
        from coordinator_core.ops.dispatch_emit.ask_gate import retire_token_spent_halt

        try:
            _bounded_call(("retire", str(root), sizing_rel), lambda: retire_token_spent_halt(root, sizing_rel))
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
        if em_size is not None:
            params["em_size"] = em_size
    if approved:
        params["cross_repo_approved"] = True

    result: Any = None
    if sizing_rel and asks is None and "from_sizing" not in params:
        resume_envelope, result = _resume_route(
            root, sizing_rel, payload, params, session_id, launch,
            reserved_run_id=reserved_run_id, ask_parent_id=ask_parent_id,
        )
        if resume_envelope is not None:
            return resume_envelope
    if result is None and emit is None and _premintable(params, sizing_rel, asks, supersedes):
        minted = None
        from coordinator_core.ops.dispatch_emit import run_token as _rt

        chosen = _rt.new_ask_run_id()
        try:
            minted = _emit_within_budget(lambda p, r: _premint_marker(p, r, chosen), params, root, scan=False)
        except _EmitOverran as over:
            if over.stalled:  # the disk is stalled: a deferred script would stall on it too
                return _refuse(over.message)
            if over.minted:  # the marker published as the budget ran out: the chosen id is the run
                _note_minted(chosen)
            # Unminted overrun: the deferred emit adopts the chosen id and mints it in the background.
            return _premint_deferred_envelope(root, params, chosen)
        except (OSError, ValueError):  # no git dir to hold the marker: the inline emit hands the commit back to the EM
            pass
        if minted is not None:
            _note_minted(minted["run_id"])
            return _premint_deferred_envelope(root, params, minted["run_id"])
    if result is None:
        try:
            result = _emit_within_budget(emit or _default_emit, params, root)
        except _EmitOverran as over:
            if over.minted:
                import time

                return _minted_start_envelope(root, over.run_ids, time.monotonic() + _ENVELOPE_GRACE_S)
            return _refuse(over.message) if over.stalled else _deferred_envelope(root, params, over)
        except Exception as exc:  # the engine's refusal text is the whole message
            why = _unavailable(exc)
            live = _CALL.get()
            nothing_minted = live is None or _minted_since_start(live) == []
            spooled = _spool_envelope(payload, root, why) if why and nothing_minted else None
            if spooled is not None:
                return spooled
            return _refuse(_name_root_tried(f"The engine refused the submission: {_cause(exc)}", root, sizing_rel))

    if isinstance(result, dict) and result.get("status") == "handover_pending" and not result.get("run_id"):
        spooled = _spool_envelope(payload, root, "handover-pending")
        if spooled is not None:
            return spooled
    if isinstance(result, dict):
        _note_minted(result.get("run_id"))
    script = result.get("path") if isinstance(result, dict) else None
    if not script or result.get("ok") is False:
        stale = result.get("run_id") if isinstance(result, dict) else None
        if stale:
            from coordinator_core.ops.dispatch_emit import run_token

            _write_or_detach("run_token.consume", lambda: run_token.consume(root, str(stale)))
        return _refuse(_name_root_tried(f"The engine emitted no run: {_no_run_detail(result)}", root, sizing_rel))

    run_id = result.get("run_id") or Path(script).name
    call_id = payload.get("tool_use_id")
    try:
        _write_or_detach(
            "warp_live.record_submission",
            lambda: warp_live.record_submission(
                root,
                str(run_id),
                session_id=session_id,
                transcript_path=transcript,
                script_path=str(script),
                sizing_rel=sizing_rel,
                tool_use_id=call_id if isinstance(call_id, str) else None,
            ),
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
            "Do not poll or verify this run meanwhile; unrelated work proceeds."
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
    derived += memo_advisory
    if asks is None and not (sizing_rel and _has_research_block(sizing_doc)):
        found = _existing_research(root, ask, sizing_rel)
        if found:
            derived += (
                f" Research artifacts for this ask already exist: {', '.join(found)}. "
                "Read them before the build reruns that work."
            )
    detached_steps = list(result.get("detached") or []) if isinstance(result, dict) else []
    call_now = _CALL.get()
    for step in (call_now or {}).get("detached", ()):
        if step not in detached_steps:
            detached_steps.append(step)
    detached_note = _detached_note(detached_steps) if detached_steps else ""
    envelope = rewrite_input(
        "PreToolUse",
        new_input,
        f"{lead}Submitted to the engine as run {run_id}.{marked}{carries}{derived}{retire_note}{detached_note} {tail} {_revise_hint(str(run_id))}",
    )
    hso = envelope["hookSpecificOutput"]
    if detached_steps:
        hso["detached"] = True
        hso["detachedSteps"] = detached_steps
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


def _denies(envelope: Any) -> bool:
    hso = envelope.get("hookSpecificOutput") if isinstance(envelope, dict) else None
    return isinstance(hso, dict) and hso.get("permissionDecision") == "deny"


def _named_deny(envelope: dict) -> dict:
    """The deny with a non-blank reason: the caller renders a blank one as "emitted no run: it said nothing"."""
    hso = envelope["hookSpecificOutput"]
    if not str(hso.get("permissionDecisionReason") or "").strip():
        hso["permissionDecisionReason"] = (
            "The submit hook denied the submission and recorded no reason. "
            "Check for an existing run record before re-submitting."
        )
    return envelope


def _through_shared_gates(payload: Mapping[str, Any]) -> dict:
    """The hook's envelope for a submission, reached through `_engine_ask_gates.submit_ask`.

    The payload's explicit fields (ask or sizing, source, session, cwd, agent) go in as submit_ask's
    arguments; the gate runner it calls is `evaluate` on the ORIGINAL payload, so transcript_path,
    tool_use_id, env and the multi-ask or resume args survive. A shape submit_ask refuses before running
    a gate (no session, a relative cwd, `asks`, `resume_run_id`) runs the same `evaluate` directly."""
    from coordinator_core.hooks._engine_ask_gates import submit_ask

    args = payload["tool_input"].get("args")
    args = args if isinstance(args, dict) else {}
    seen: dict = {}

    def relay(_rebuilt: Mapping[str, Any]) -> dict:
        try:
            seen["envelope"] = evaluate(payload)
        except BaseException as exc:
            seen["error"] = exc
            raise
        return seen["envelope"]

    ask, sizing = args.get("ask"), args.get("sizing")
    submit_ask(
        ask=ask if isinstance(ask, str) else None,
        sizing=sizing if isinstance(sizing, str) else None,
        ask_source=args.get("source") if isinstance(args.get("source"), str) else None,
        session_id=payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
        caller="hook",
        cwd=payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
        agent_id=payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None,
        evaluate=relay,
    )
    if "error" in seen:
        raise seen["error"]
    return seen["envelope"] if "envelope" in seen else evaluate(payload)


@register_op("hooks.submit_engine_ask")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Workflow) door onto the shared gates; a deny resolves at the guard-level policy point."""
    payload = payload_of(params)
    from coordinator_core.machine_profile import apply_guard_level

    try:
        envelope = _through_shared_gates(payload) if is_submission(payload) else evaluate(payload)
    except Exception as exc:
        envelope = deny("PreToolUse", f"The submit hook failed: {type(exc).__name__}: {exc}. Check for an existing run record before re-submitting.")
    submission = is_submission(payload)
    if submission and _denies(envelope):
        return _named_deny(envelope)  # a deny skips apply_guard_level: warn/off lowers it to a rewrite-less allow
    resolved = apply_guard_level(GUARD_NAME, envelope)
    if not resolved and submission:
        return deny("PreToolUse", "The submit hook resolved to an empty reply. Check for an existing run record before re-submitting.")
    if submission and resolved and resolved.get("hookSpecificOutput", {}).get("permissionDecision") != "deny":
        if not resolved.get("hookSpecificOutput", {}).get("updatedInput"):
            return deny(
                "PreToolUse",
                "The engine answered the submission with no run and no refusal. Check for an existing run record before re-submitting.",
            )
    return resolved or no_advisory()


__all__ = [
    "GUARD_NAME",
    "SIZING_SCOUT_TYPES",
    "evaluate",
    "is_submission",
    "sizing_scout_deny",
]
