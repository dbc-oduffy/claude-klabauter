"""coordinator_core.ops.engine_ask_submit -- JSON-RPC "engine_ask.submit": the engine-ask door for a
caller with no Workflow tool (Codex).

Wire params: `ask` xor `sizing`; `ask_source` ("pm" | "relayed", required with `ask`); `session_id`,
`caller` (identity string) and absolute `cwd`, all required. A launcher-spawned headless EM may omit
them: with `session_id` absent, `COORDINATOR_SESSION_ID` from the caller env is taken only when the
headless-EM grant binds it (`COORDINATOR_ALLOW_HEADLESS_EM_TOKEN`), `caller` defaults to `<family>-em`
from that grant and `cwd` to the op's repo root; a native Codex thread may supply its own registered EM identity when the launcher id is absent. Optional
`repo`, `agent_id`, `notify_to`, the hook's optional args (`em_context`, `context`, `brief`, `brief_file`, `route_preference`,
`em_rationale`, `em_size`, `stop_after`, `chatty`, `cross_repo_approved`, `supersedes`, `writes`), and
`fire` (bool, default true: hand the run to `workflow.fire` with `expected=<sha256>`; false mints without starting),
`executor` (headless by default, or claude), and `backend` (codex by default; claude, grok or agy opt-in).
Completion defaults to the submitting session UUID; notify_to explicitly overrides it.
An empty params object reads the object in `.coordinator-local/em-submit.json` under the repo; a missing file
is created as an empty-ask scaffold and refused as `bad-request` naming that path, a non-object file likewise. Non-empty params are used as given.

Reply, never empty: `{run_id, script_path, sha256}` (plus `fire_args`, `detached_steps`, and `fire` when
`fire` is not false) or `{refused, reason}` with `refused` in `_engine_ask_gates.REFUSAL_CODES`. A fire that
is refused or raises after a successful submit keeps the run handle and adds `fire_error` naming the cause.

Spec backlink: docs/plans/2026-11/2026-11-07-7402bb-2-engine-ask-op-side-submit.md § C1
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.workflow_fire.backends import BACKENDS

CANONICAL_PARAMS_PATH = ".coordinator-local/em-submit.json"
_SCAFFOLD = {"ask": "", "ask_source": "pm"}

_EXTRA_KEYS = (
    "em_context", "context", "brief", "brief_file", "route_preference", "em_rationale", "em_size",
    "stop_after", "chatty", "cross_repo_approved", "supersedes", "writes", "resume_run_id", "resume_in_place", "takeover", "fresh",
)
_KNOWN_KEYS = frozenset(
    ("ask", "sizing", "ask_source", "session_id", "caller", "cwd", "repo", "agent_id", "fire", "notify_to", "executor", "backend", "private_engine", *_EXTRA_KEYS)
)


def _with_bound_identity(params: dict, repo_root: Optional[Path]) -> dict:
    """Resolve an omitted identity from a bound launch grant or the registered native Codex EM."""
    sid = params.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return params
    from coordinator_core.ops.workflow_fire.headless_em_grant import HEADLESS_EM_FAMILIES, headless_em_grant_matches
    from coordinator_core.session.caller_env import effective_caller_env

    env = effective_caller_env()
    env_sid = env.get("COORDINATOR_SESSION_ID")
    if isinstance(env_sid, str) and env_sid.strip():
        family = next((f for f in HEADLESS_EM_FAMILIES if headless_em_grant_matches(env, env_sid, f)), None)
        if family is None:
            return params
    else:
        env_sid = env.get("CODEX_THREAD_ID")
        if not env_sid:
            return params
        from coordinator_core.ops.workflow_fire.authority import caller_principal

        principal = caller_principal(str(repo_root) if repo_root else None)
        if principal.session_id != env_sid or principal.role != "em":
            return params
        family = "codex"
    filled = dict(params, session_id=env_sid)
    if not (isinstance(params.get("caller"), str) and params["caller"].strip()):
        filled["caller"] = f"{family}-em"
    if not (isinstance(params.get("cwd"), str) and params["cwd"].strip()) and repo_root is not None:
        filled["cwd"] = str(repo_root)
    return filled


def _canonical_params(repo_root: Path) -> dict:
    """Load `.coordinator-local/em-submit.json` under the main worktree; a refusal dict names the path.

    `repo_root` is the op's common-dir key (`<worktree>/.git`) when served through the CLI.
    """
    path = (repo_root.parent if repo_root.name == ".git" else repo_root) / CANONICAL_PARAMS_PATH
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as fh:
                json.dump(_SCAFFOLD, fh, indent=2)
        except OSError:
            pass
        return {"refused": "bad-request",
                "reason": f"No ask staged: set `ask` to the PM's words verbatim in {CANONICAL_PARAMS_PATH}, then repeat this call."}
    except (OSError, ValueError) as exc:
        return {"refused": "bad-request",
                "reason": f"No params given and {CANONICAL_PARAMS_PATH} is unreadable ({type(exc).__name__}); write the params object there."}
    if not isinstance(loaded, dict):
        return {"refused": "bad-request", "reason": f"{CANONICAL_PARAMS_PATH} must hold a JSON object of params."}
    return {"params": loaded}


@register_op("engine_ask.submit")
def _engine_ask_submit(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Submit one ask or sizing through the shared gates; see the module docstring for the wire contract."""
    from coordinator_core.hooks._engine_ask_gates import submit_ask

    if not isinstance(params, dict):
        return {"refused": "bad-request", "reason": "engine_ask.submit takes an object of params."}
    if not params and repo_root is not None:
        canonical = _canonical_params(Path(repo_root))
        if "refused" in canonical:
            return canonical
        params = canonical["params"]
    unknown = sorted(set(params) - _KNOWN_KEYS)
    if unknown:
        return {
            "refused": "bad-request",
            "reason": f"Unknown param(s): {', '.join(unknown)}. Accepted: {', '.join(sorted(_KNOWN_KEYS))}.",
        }
    params = _with_bound_identity(params, repo_root)
    # Session UUIDs resolve through the owned external-host binding. Display
    # aliases may not yet be exported by the installed transport on this box.
    target = params.get("notify_to", params.get("session_id"))
    executor = params.get("executor", "headless")
    backend = params.get("backend", "codex")
    if executor not in ("headless", "claude") or backend not in BACKENDS:
        return {"refused": "bad-request", "reason": f"executor must be headless or claude; backend must be {'|'.join(BACKENDS)}."}
    if target is not None and (not isinstance(target, str) or not target.strip()):
        return {"refused": "bad-request", "reason": "notify_to must be a non-empty peer name."}
    if not isinstance(params.get("fire", True), bool):
        return {"refused": "bad-request", "reason": "fire must be a boolean (default true; false mints without starting)."}
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
    if params.get("fire", True) is not False and "refused" not in reply:
        from coordinator_core.ops.workflow_fire.op import _workflow_fire
        from coordinator_core.warm.sandboxed_spawn import SpawnIndeterminate

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
            fired = reply["fire"]
            if isinstance(fired, dict) and fired.get("refused"):
                reply["fire_error"] = f"{fired['refused']}: {fired.get('reason', '')}"
            elif not isinstance(fired, dict) or not isinstance(fired.get("fire_id"), str) or not fired["fire_id"].strip():
                reply["fire_error"] = "Requested fire returned no fire_id; reconcile the minted run before retrying."
        except SpawnIndeterminate as exc:
            reply["fire_error"] = str(exc)
            reply["fire"] = {"state": "indeterminate", "run_id": exc.run_id, "error": exc.error}
        except Exception as exc:  # the submit stands; the caller fires by hand with the handle
            reply["fire_error"] = f"{type(exc).__name__}: {exc}"
    return reply
