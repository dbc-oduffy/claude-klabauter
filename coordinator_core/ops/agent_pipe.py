"""
coordinator_core.ops.agent_pipe -- JSON-RPC "agent_pipe.start" and "agent_pipe.send".

Both validate a request, write `<settings_home>/agent-pipe/runs/<run_id>/request.json`, spawn
`python -m coordinator_core.agent_pipe <run_dir>` detached and return at spawn. The worker relays
the agent's messages into the addressed session's mailbox and exits with its agent child.

Self-registration: importing this module registers both ops. Wired in `ops/__init__.py`
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
from coordinator_core.agent_pipe import contract
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
