"""coordinator_core.hooks._engine_ask_gates -- the one function that decides whether an ask may be
emitted as a warp run, and as what script.

Both doors call `submit_ask`: the PreToolUse(Workflow) hook (`hooks.submit_engine_ask`) and the
`engine_ask.submit` op a non-Claude caller (Codex) invokes. Every gate the hook runs -- sizing
and mint, cross-repo refusal, live overlap, door deadline, emit -- runs behind it; this module
holds none of them twice.

Contract: explicit arguments in (no hook payload, no environment-derived identity), a result dict
out that is never empty -- `{run_id, script_path, sha256}` (plus `fire_args` when the run needs
them) or `{refused, reason}` where `refused` is one of `REFUSAL_CODES`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

REFUSAL_CODES = ("bad-request", "gate-refused", "no-run")

_RUN_ID = re.compile(r"Submitted to the engine as run (\S+?)[.:,]?(?:\s|$)")
_DEFERRED_RUN_ID = re.compile(r"Engine submission: run (\S+?),")

EvaluateFn = Callable[[Mapping[str, Any]], dict]


def _refused(code: str, reason: str) -> dict:
    return {"refused": code, "reason": reason}


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _args(
    *,
    ask: Optional[str],
    sizing: Optional[str],
    ask_source: Optional[str],
    repo: Optional[str],
    extras: Mapping[str, Any],
) -> dict:
    args: dict = {}
    if ask is not None:
        args["ask"] = ask
        args["source"] = ask_source
    else:
        args["sizing"] = sizing
    if repo is not None:
        args["repo"] = repo
    args.update({k: v for k, v in extras.items() if v is not None})
    return args


def _reply_of(envelope: Any) -> dict:
    """The result dict for the hook's answer envelope; a deny and an empty answer are named refusals."""
    hso = envelope.get("hookSpecificOutput") if isinstance(envelope, dict) else None
    if not isinstance(hso, dict):
        return _refused("no-run", "The gates answered with no run and no refusal. Check for an existing run record before re-submitting.")
    if hso.get("permissionDecision") == "deny":
        reason = str(hso.get("permissionDecisionReason") or "").strip()
        return _refused("gate-refused", reason or "The gates refused the submission and recorded no reason.")
    updated = hso.get("updatedInput")
    script = updated.get("scriptPath") if isinstance(updated, dict) else None
    if not isinstance(script, str) or not script:
        return _refused("no-run", "The gates allowed the submission but emitted no script. Check for an existing run record before re-submitting.")
    path = Path(script)
    try:
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return _refused("no-run", f"The emitted script {script} is unreadable: {type(exc).__name__}: {exc}.")
    context = str(hso.get("additionalContext") or "")
    found = _RUN_ID.search(context) or _DEFERRED_RUN_ID.search(str(hso.get("permissionDecisionReason") or ""))
    reply: dict = {"run_id": found.group(1) if found else None, "script_path": str(path), "sha256": sha}
    if updated.get("args"):
        reply["fire_args"] = updated["args"]
    if hso.get("detached"):
        reply["detached_steps"] = list(hso.get("detachedSteps") or [])
    return reply


def submit_ask(
    *,
    ask: Optional[str] = None,
    sizing: Optional[str] = None,
    ask_source: Optional[str] = None,
    session_id: Optional[str] = None,
    caller: Optional[str] = None,
    cwd: Optional[str] = None,
    repo: Optional[str] = None,
    agent_id: Optional[str] = None,
    extras: Optional[Mapping[str, Any]] = None,
    evaluate: Optional[EvaluateFn] = None,
) -> dict:
    """Run the submission gates for one ask or one sizing; return the run handle or a named refusal.

    `ask` (the PM's or the relayed words) xor `sizing` (a sizing path); an `ask` needs `ask_source`
    in `ask_contract.ASK_SOURCES`. `session_id`, `caller` and absolute `cwd` are required: the live
    overlap and cross-repo gates key on them. `agent_id` names a subagent caller, which only the EM
    may not be. `extras` carries the optional args the hook already accepts (em_context, context,
    brief, route_preference, em_rationale, em_size, stop_after, chatty, cross_repo_approved,
    supersedes, writes). `evaluate` is the gate runner, by default the hook's `evaluate`."""
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_SOURCES
    from coordinator_core.warp_fire import SUBMIT_WORKFLOW_NAME

    ask, sizing, source = _text(ask), _text(sizing), _text(ask_source)
    session, who, where = _text(session_id), _text(caller), _text(cwd)
    if (ask is None) == (sizing is None):
        return _refused("bad-request", "Submit exactly one of ask, sizing.")
    if ask is not None and source not in ASK_SOURCES:
        return _refused("bad-request", f"ask_source must be one of: {', '.join(ASK_SOURCES)}; got {ask_source!r}.")
    if session is None:
        return _refused("bad-request", "session_id is required.")
    if who is None:
        return _refused("bad-request", "caller is required.")
    if where is None or not Path(where).is_absolute() or not Path(where).is_dir():
        return _refused("bad-request", f"cwd must be an absolute existing directory; got {cwd!r}.")

    payload: dict = {
        "tool_name": "Workflow",
        "tool_input": {
            "name": SUBMIT_WORKFLOW_NAME,
            "args": _args(ask=ask, sizing=sizing, ask_source=source, repo=_text(repo), extras=extras or {}),
        },
        "session_id": session,
        "cwd": where,
    }
    if agent_id:
        payload["agent_id"] = agent_id
    if evaluate is None:
        from coordinator_core.hooks.submit_engine_ask import evaluate as hook_evaluate

        evaluate = hook_evaluate
    try:
        envelope = evaluate(payload)
    except Exception as exc:
        return _refused("gate-refused", f"The submission gates failed: {type(exc).__name__}: {exc}. Check for an existing run record before re-submitting.")
    return _reply_of(envelope)


__all__ = ["REFUSAL_CODES", "submit_ask"]
