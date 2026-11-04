"""
coordinator_core.ops.agent_pipe -- JSON-RPC "agent_pipe.start", "agent_pipe.send" and "agent_pipe.list_live".

Start and send validate a request, write `<settings_home>/agent-pipe/runs/<run_id>/request.json`, spawn
`python -m coordinator_core.agent_pipe <run_dir>` detached and return at spawn. The worker relays
the agent's messages into the addressed session's mailbox and exits with its agent child.

"agent_pipe.bridge_send", "agent_pipe.roster_bind" and "agent_pipe.route_set" front the resident bridge:
they write the bridge inbox, roster and routes files and spawn nothing.

Self-registration: importing this module registers every op. Wired in `ops/__init__.py`
(`_EAGER_OP_MODULES`), `ops/_registry_map.py`, `op_scopes.py` and `authz/classification.py`.

Negative-spec:
    - Neither op waits on the worker; the agent's duration is the worker's, not the op's.
    - No argv, flag or env is accepted from the wire: the adapter alone builds the agent argv.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core._settings_home import settings_home_child_env
from coordinator_core.agent_pipe import bridge, contract, live, roster
from coordinator_core.ipc import CallerFacingValidationError, register_op

_REQUEST = "request.json"
_LOG = "worker.log"
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])


def _detach_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


def _text(op: str, p: dict, key: str, *, required: bool = True) -> Optional[str]:
    value = p.get(key)
    if value is None:
        if required:
            raise CallerFacingValidationError(f"{op}: {key} (string) is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise CallerFacingValidationError(f"{op}: {key} must be a non-empty string")
    return value


def _spawn(op: str, p: dict, request: dict) -> dict:
    agent = _text(op, p, "agent")
    if agent not in contract.AGENTS:
        raise CallerFacingValidationError(f"{op}: agent must be one of {list(contract.AGENTS)}")
    reply_to = _text(op, p, "reply_to")
    try:
        mailbox = contract.mailbox_path(reply_to)
    except ValueError as exc:
        raise CallerFacingValidationError(f"{op}: {exc}") from exc
    cwd = _text(op, p, "cwd", required=False)
    if cwd is not None and not os.path.isdir(cwd):
        raise CallerFacingValidationError(f"{op}: cwd is not a directory: {cwd}")

    run_id = f"{agent}-{uuid.uuid4().hex[:12]}"
    run_dir = contract.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / _REQUEST).write_text(
        json.dumps({"agent": agent, "reply_to": reply_to, **request}), encoding="utf-8"
    )

    env = settings_home_child_env(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [_PACKAGE_ROOT, env.get("PYTHONPATH")]))
    with open(run_dir / _LOG, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "coordinator_core.agent_pipe", str(run_dir)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=cwd or str(run_dir),
            env=env,
            **_detach_kwargs(),
        )
    return {"run_id": run_id, "mailbox": str(mailbox), "run_dir": str(run_dir), "pid": proc.pid}


@register_op("agent_pipe.start")
def _agent_pipe_start(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Start a headless agent session whose messages land in a Claude session's mailbox.

    Params: agent ("antigravity" | "codex"), prompt, reply_to (the receiving Claude session id);
    cwd optional (the agent's working directory; default the run dir).
    Returns: {"run_id", "mailbox", "run_dir", "pid"} -- the pid is the worker's, not the agent's.
    """
    p = params if isinstance(params, dict) else {}
    return _spawn("agent_pipe.start", p, {"prompt": _text("agent_pipe.start", p, "prompt")})


@register_op("agent_pipe.list_live")
def _agent_pipe_list_live(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Map a live agy or codex TUI (by pid or cwd) to its conversation id; read-only.

    Params: pid (int) and/or cwd (string), both optional (neither lists every live session).
    Returns: {"sessions": [{agent, pid, cwd, conversation}], "unsupported"?: reason}.
    """
    p = params if isinstance(params, dict) else {}
    pid = p.get("pid")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise CallerFacingValidationError("agent_pipe.list_live: pid must be a positive integer")
    return live.find_live(pid=pid, cwd=_text("agent_pipe.list_live", p, "cwd", required=False))


def _identity(op: str, p: dict, key: str) -> str:
    value = _text(op, p, key)
    try:
        return contract._checked_id(key, value)
    except ValueError as exc:
        raise CallerFacingValidationError(f"{op}: {exc}") from exc


@register_op("agent_pipe.bridge_send")
def _agent_pipe_bridge_send(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Queue a labelled, provenance-stamped message for the resident bridge; never waits on delivery.

    Params: from, to (roster names), text.
    Returns: {"id", "inbox", "from_kind"}. The bridge resolves `to` at dispatch time.
    """
    p = params if isinstance(params, dict) else {}
    op = "agent_pipe.bridge_send"
    msg = bridge.make_message(_identity(op, p, "from"), _identity(op, p, "to"), _text(op, p, "text"))
    return {"id": msg.id, "inbox": str(bridge.enqueue(msg)), "from_kind": msg.from_kind}


@register_op("agent_pipe.roster_bind")
def _agent_pipe_roster_bind(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Bind (or rebind) a durable roster name to a kind and current id.

    Params: name, kind ("claude" | "codex" | "agy"), id; token optional (stored as data, never a gate).
    A codex binding gets the adapter's app-server argv; no argv is accepted from the wire.
    Returns: {"name", "binding"}.
    """
    p = params if isinstance(params, dict) else {}
    op = "agent_pipe.roster_bind"
    kind = _text(op, p, "kind")
    if kind not in contract.KINDS:
        raise CallerFacingValidationError(f"{op}: kind must be one of {list(contract.KINDS)}")
    argv = None
    if kind == contract.KIND_CODEX:
        from coordinator_core.agent_pipe.codex import CodexAdapter

        argv = CodexAdapter().proxy_argv()
    name = _identity(op, p, "name")
    try:
        binding = roster.bind(name, kind, _text(op, p, "id"), token=_text(op, p, "token", required=False), argv=argv)
    except ValueError as exc:
        raise CallerFacingValidationError(f"{op}: {exc}") from exc
    return {"name": name, "binding": binding}


@register_op("agent_pipe.route_set")
def _agent_pipe_route_set(params: dict, repo_root: Optional[Path] = None) -> dict:
    """PM routing control: pause or resume a pair, or mute or unmute an identity.

    Params: either from and to (a pair; "*" matches any side) with paused (bool, default true),
    or identity with muted (bool, default true).
    Returns: the routes document.
    """
    p = params if isinstance(params, dict) else {}
    op = "agent_pipe.route_set"
    flag = p.get("paused" if "identity" not in p else "muted", True)
    if not isinstance(flag, bool):
        raise CallerFacingValidationError(f"{op}: paused/muted must be a boolean")
    try:
        if "identity" in p:
            return bridge.set_mute(_text(op, p, "identity"), flag)
        return bridge.set_pause(_text(op, p, "from"), _text(op, p, "to"), flag)
    except ValueError as exc:
        raise CallerFacingValidationError(f"{op}: {exc}") from exc


@register_op("agent_pipe.send")
def _agent_pipe_send(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Deliver `text` into an existing agent conversation through a new run.

    Params: agent, conversation (the id on a mailbox line's sender_conversation), text, reply_to;
    cwd optional.
    Returns: {"run_id", "mailbox", "run_dir", "pid"}.
    """
    p = params if isinstance(params, dict) else {}
    return _spawn(
        "agent_pipe.send",
        p,
        {
            "conversation": _text("agent_pipe.send", p, "conversation"),
            "text": _text("agent_pipe.send", p, "text"),
        },
    )
