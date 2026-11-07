"""coordinator_core.ops.engine_ask_submit -- JSON-RPC "engine_ask.submit": the engine-ask door for a
caller with no Workflow tool (Codex).

Wire params: `ask` xor `sizing`; `ask_source` ("pm" | "relayed", required with `ask`); `session_id`,
`caller` (identity string) and absolute `cwd`, all required and never read from the environment; optional
`repo`, `agent_id`, `notify_to`, the hook's optional args (`em_context`, `context`, `brief`, `brief_file`, `route_preference`,
`em_rationale`, `em_size`, `stop_after`, `chatty`, `cross_repo_approved`, `supersedes`, `writes`), and
`fire` (bool: hand the run to `workflow.fire` with `expected=<sha256>`).

Reply, never empty: `{run_id, script_path, sha256}` (plus `fire_args`, `detached_steps`, and `fire` when
`fire` is true) or `{refused, reason}` with `refused` in `_engine_ask_gates.REFUSAL_CODES`. A fire that
fails after a successful submit keeps the run handle and adds `fire_error`.

Spec backlink: docs/plans/2026-11/2026-11-07-7402bb-2-engine-ask-op-side-submit.md § C1
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

_EXTRA_KEYS = (
    "em_context", "context", "brief", "brief_file", "route_preference", "em_rationale", "em_size",
    "stop_after", "chatty", "cross_repo_approved", "supersedes", "writes",
)
_KNOWN_KEYS = frozenset(
    ("ask", "sizing", "ask_source", "session_id", "caller", "cwd", "repo", "agent_id", "fire", "notify_to", *_EXTRA_KEYS)
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
    target = params.get("notify_to", params.get("caller"))
    if target is not None and (not isinstance(target, str) or not target.strip()):
        return {"refused": "bad-request", "reason": "notify_to must be a non-empty peer name."}
    reply = submit_ask(
        ask=params.get("ask"),
        sizing=params.get("sizing"),
        ask_source=params.get("ask_source"),
        session_id=params.get("session_id"),
        caller=params.get("caller"),
        cwd=params.get("cwd"),
        repo=params.get("repo"),
        agent_id=params.get("agent_id"),
        extras={k: params.get(k) for k in _EXTRA_KEYS},
    )
    if params.get("fire") is True and "refused" not in reply:
        from coordinator_core.ops.workflow_fire.op import _workflow_fire

        try:
            reply["fire"] = _workflow_fire(
                {"script_path": reply["script_path"], "expected": reply["sha256"], "notify_to": target}, repo_root
            )
        except Exception as exc:  # the submit stands; the caller fires by hand with the handle
            reply["fire_error"] = f"{type(exc).__name__}: {exc}"
    return reply
