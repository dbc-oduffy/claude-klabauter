"""agent_pipe relay worker: `python -m coordinator_core.agent_pipe <run_dir>`.

Spawned detached by `agent_pipe.start` / `agent_pipe.send`. Reads `<run_dir>/request.json`
({agent, reply_to, prompt} or {agent, reply_to, conversation, text}), runs the agent CLI headless
in its native streaming mode, appends every line the adapter parses as a message to the addressed
session's mailbox, writes `result.json` atomically and exits with its child. No resident process.

The adapter is looked up by agent name; `COORDINATOR_AGENT_PIPE_ADAPTER=<module>:<Class>` replaces
that lookup (tests drive a fake CLI through it).
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from coordinator_core.agent_pipe import mailbox
from coordinator_core.agent_pipe.contract import Adapter, PipeMessage

ADAPTER_ENV = "COORDINATOR_AGENT_PIPE_ADAPTER"
REQUEST_FILENAME = "request.json"
RESULT_FILENAME = "result.json"
ARGV_FILENAME = "argv.json"
AGENT_LOG_FILENAME = "agent.stderr.log"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

_ADAPTERS = {
    "antigravity": ("coordinator_core.agent_pipe.agy", "AgyAdapter"),
    "codex": ("coordinator_core.agent_pipe.codex", "CodexAdapter"),
}


def load_adapter(agent: str) -> Adapter:
    """The adapter for `agent`, or the one named by COORDINATOR_AGENT_PIPE_ADAPTER."""
    override = os.environ.get(ADAPTER_ENV)
    if override:
        module, _, cls = override.partition(":")
        return getattr(importlib.import_module(module), cls)()
    if agent not in _ADAPTERS:
        raise ValueError(f"unknown agent: {agent!r}")
    module, cls = _ADAPTERS[agent]
    return getattr(importlib.import_module(module), cls)()


def _write_result(run_dir: Path, result: dict) -> None:
    final = run_dir / RESULT_FILENAME
    tmp = run_dir / f"{RESULT_FILENAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(result), encoding="utf-8")
    os.replace(tmp, final)


def _child_flags() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _relay(run_dir: Path, req: dict, adapter: Adapter) -> dict:
    agent = req["agent"]
    reply_to = req["reply_to"]
    if req.get("conversation"):
        argv = adapter.resume_argv(req["conversation"], req["text"])
    else:
        argv = adapter.spawn_argv(req["prompt"])
    (run_dir / ARGV_FILENAME).write_text(json.dumps(argv), encoding="utf-8")

    conversation = req.get("conversation") or ""
    messages = 0
    with open(run_dir / AGENT_LOG_FILENAME, "wb") as err:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            encoding="utf-8",
            errors="replace",
            **_child_flags(),
        )
        try:
            if child.stdout is None:
                raise RuntimeError("child stdout pipe missing")
            for line in child.stdout:
                parsed = adapter.parse_line(line.rstrip("\r\n"))
                if parsed is None:
                    continue
                conversation, content = parsed
                mailbox.append(
                    PipeMessage(
                        id=uuid.uuid4().hex,
                        sender_agent=agent,
                        sender_conversation=conversation,
                        to_session=reply_to,
                        content=content,
                        ts=datetime.now(timezone.utc).isoformat(),
                    )
                )
                messages += 1
            exit_code = child.wait()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    return {
        "run_id": run_dir.name,
        "status": STATUS_COMPLETED if exit_code == 0 else STATUS_FAILED,
        "exit_code": exit_code,
        "conversation": conversation,
        "messages": messages,
        "error": None if exit_code == 0 else f"agent exited {exit_code}",
    }


def run_worker(run_dir: Path) -> dict:
    """Relay the request in `run_dir` to completion and persist the result; never raises."""
    try:
        req = json.loads((run_dir / REQUEST_FILENAME).read_text(encoding="utf-8"))
        result = _relay(run_dir, req, load_adapter(req["agent"]))
    except BaseException as exc:  # noqa: BLE001 -- the worker must always leave a result
        result = {
            "run_id": run_dir.name,
            "status": STATUS_FAILED,
            "exit_code": None,
            "conversation": "",
            "messages": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    _write_result(run_dir, result)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: python -m coordinator_core.agent_pipe <run_dir>\n")
        return 2
    run_worker(Path(args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
