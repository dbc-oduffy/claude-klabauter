"""coordinator_core.ops.engine_ask_submit -- JSON-RPC "engine_ask.submit": the engine-ask door for a
caller with no Workflow tool (Codex).

Wire params: `ask` xor `sizing`; `ask_source` ("pm" | "relayed", required with `ask`); `session_id`,
`caller` (identity string) and absolute `cwd`, all required and never read from the environment; optional
`repo`, `agent_id`, `notify_to`, the hook's optional args (`em_context`, `context`, `brief`, `brief_file`, `route_preference`,
`em_rationale`, `em_size`, `stop_after`, `chatty`, `cross_repo_approved`, `supersedes`, `writes`), and
`fire` (bool: hand the run to `workflow.fire` with `expected=<sha256>`),
`executor` (headless by default, or claude), and `backend` (codex by default; claude, grok or agy opt-in).
Completion defaults to the submitting session UUID; notify_to explicitly overrides it.

Reply, never empty: `{run_id, script_path, sha256}` (plus `fire_args`, `detached_steps`, and `fire` when
`fire` is true) or `{refused, reason}` with `refused` in `_engine_ask_gates.REFUSAL_CODES`. A fire that
fails after a successful submit keeps the run handle and adds `fire_error`.

Spec backlink: docs/plans/2026-11/2026-11-07-7402bb-2-engine-ask-op-side-submit.md § C1
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.workflow_fire.backends import BACKENDS

_EXTRA_KEYS = (
    "em_context", "context", "brief", "brief_file", "route_preference", "em_rationale", "em_size",
    "stop_after", "chatty", "cross_repo_approved", "supersedes", "writes",
)
_KNOWN_KEYS = frozenset(
    ("ask", "sizing", "ask_source", "session_id", "caller", "cwd", "repo", "agent_id", "fire", "notify_to", "executor", "backend", "private_engine", *_EXTRA_KEYS)
)


@register_op("engine_ask.submit")
def _engine_ask_submit(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Submit one ask or sizing through the shared gates; see the module docstring for the wire contract."""
    from coordinator_core.hooks._engine_ask_gates import submit_ask

    if not isinstance(params, dict):
        return {"refused": "bad-request", "reason": "engine_ask.submit takes an object of params."}
    unknown = sorted(set(params) - _KNOWN_KEYS)
    if unknown:
        return {
            "refused": "bad-request",
            "reason": f"Unknown param(s): {', '.join(unknown)}. Accepted: {', '.join(sorted(_KNOWN_KEYS))}.",
        }
    # Session UUIDs resolve through the owned external-host binding. Display
    # aliases may not yet be exported by the installed transport on this box.
    target = params.get("notify_to", params.get("session_id"))
    executor = params.get("executor", "headless")
    backend = params.get("backend", "codex")
    if executor not in ("headless", "claude") or backend not in BACKENDS:
        return {"refused": "bad-request", "reason": f"executor must be headless or claude; backend must be {'|'.join(BACKENDS)}."}
    if target is not None and (not isinstance(target, str) or not target.strip()):
        return {"refused": "bad-request", "reason": "notify_to must be a non-empty peer name."}
    private_spec = None
    if params.get("private_engine") is not None:
        from coordinator_core.ops.workflow_fire.headless import validate_private_engine_config

        try:
            if executor != "headless":
                raise ValueError("a private native engine requires the headless executor")
            private_spec = validate_private_engine_config(params["private_engine"], backend=backend)
        except ValueError as exc:
            return {"refused": "bad-request", "reason": str(exc)}
    reply = submit_ask(
        ask=params.get("ask"),
        sizing=params.get("sizing"),
        ask_source=params.get("ask_source"),
        session_id=params.get("session_id"),
        caller=params.get("caller"),
        cwd=params.get("cwd"),
        repo=params.get("repo"),
        agent_id=params.get("agent_id"),
        require_em=True,
        extras={k: params.get(k) for k in _EXTRA_KEYS},
    )
    if not isinstance(reply, dict) or (
        "refused" not in reply and not (reply.get("script_path") and reply.get("sha256"))
    ):
        return {
            "refused": "no-run",
            "reason": "Submission returned no emitted script and hash. Check existing run records before resubmitting.",
        }
    if params.get("fire") is True and "refused" not in reply:
        from coordinator_core.ops.workflow_fire.op import _workflow_fire

        try:
            fire_params = {"script_path": reply["script_path"], "expected": reply["sha256"], "notify_to": target,
                           "executor": executor, "backend": backend, "caller_session_id": params["session_id"]}
            if private_spec is not None:
                fire_params["private_engine"] = private_spec
            if reply.get("fire_args"):
                fire_params["args"] = reply["fire_args"]
            if reply.get("run_id"):
                fire_params["run_id"] = reply["run_id"]
            reply["fire"] = _workflow_fire(
                fire_params, Path(params["cwd"])
            )
        except Exception as exc:  # the submit stands; the caller fires by hand with the handle
            reply["fire_error"] = f"{type(exc).__name__}: {exc}"
    return reply
