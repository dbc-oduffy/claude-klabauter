"""agent_pipe relay worker: `python -m coordinator_core.agent_pipe <run_dir>`.

Spawned detached by `agent_pipe.start` / `agent_pipe.send`. Reads `<run_dir>/request.json`
({agent, reply_to, prompt} or {agent, reply_to, conversation, text}), runs the agent CLI headless
in its native streaming mode, appends every line the adapter parses as a message to the addressed
session's mailbox, writes `result.json` atomically and exits with its child. No resident process.

Transport: a send to a conversation an adapter's `proxy_argv` app-server has loaded goes in through
the app-server (`turn/start`, no second writer); headless `start`, an unreachable control socket, or
a conversation the app-server does not list as loaded uses the adapter's `resume_argv` exec path.

`roster bind|resolve|list` and `bridge send|serve|route` are the durable-identity CLI (see bridge.py).

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

from coordinator_core.agent_pipe import codex_app_server, mailbox
from coordinator_core.agent_pipe.contract import Adapter, PipeMessage
from coordinator_core.win_portability import no_console_creationflags

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


def _append(agent: str, conversation: str, reply_to: str, content: str) -> None:
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


def _relay_app_server(run_dir: Path, req: dict, argv: list) -> Optional[dict]:
    """Deliver into a thread the live app-server has loaded; None when it is not reachable or not loaded.

    A JSON-RPC refusal of `turn/start` on a loaded thread is this run's failure, never an exec-resume retry.
    TRAP: the loaded check and the delivery use separate connections, so a thread unloaded between them fails the run.
    """
    conversation = req["conversation"]
    try:
        if conversation not in codex_app_server.loaded_threads(argv):
            return None
    except codex_app_server.AppServerUnreachable:
        return None
    except codex_app_server.AppServerError as exc:
        return _app_server_failure(run_dir, argv, conversation, 0, exc)
    (run_dir / ARGV_FILENAME).write_text(json.dumps(argv), encoding="utf-8")
    messages = 0
    (run_dir / AGENT_LOG_FILENAME).write_bytes(b"")
    try:
        for text in codex_app_server.deliver(argv, conversation, req["text"]):
            _append(req["agent"], conversation, req["reply_to"], text)
            messages += 1
    except codex_app_server.AppServerError as exc:
        return _app_server_failure(run_dir, argv, conversation, messages, exc)
    return {
        "run_id": run_dir.name,
        "status": STATUS_COMPLETED,
        "exit_code": 0,
        "conversation": conversation,
        "messages": messages,
        "error": None,
    }


def _app_server_failure(run_dir: Path, argv: list, conversation: str, messages: int, exc: Exception) -> dict:
    (run_dir / ARGV_FILENAME).write_text(json.dumps(argv), encoding="utf-8")
    (run_dir / AGENT_LOG_FILENAME).write_text(str(exc), encoding="utf-8")
    return {
        "run_id": run_dir.name,
        "status": STATUS_FAILED,
        "exit_code": None,
        "conversation": conversation,
        "messages": messages,
        "error": f"{type(exc).__name__}: {exc}",
    }


def _relay(run_dir: Path, req: dict, adapter: Adapter) -> dict:
    agent = req["agent"]
    reply_to = req["reply_to"]
    proxy_argv = getattr(adapter, "proxy_argv", None)
    if req.get("conversation") and proxy_argv is not None:
        delivered = _relay_app_server(run_dir, req, proxy_argv())
        if delivered is not None:
            return delivered
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
            **no_console_creationflags(),
        )
        try:
            if child.stdout is None:
                raise RuntimeError("child stdout pipe missing")
            for line in child.stdout:
                parsed = adapter.parse_line(line.rstrip("\r\n"))
                if parsed is None:
                    continue
                conversation, content = parsed
                _append(agent, conversation, reply_to, content)
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


_LIVE_KIND = {"antigravity": "agy", "codex": "codex"}


def _live_binding(name: str, kind: Optional[str]) -> Optional[dict]:
    """Roster fields for the one live session matching `kind` (any kind when None); None unless exactly one."""
    from coordinator_core.agent_pipe import live

    found = [s for s in live.find_live().get("sessions", []) if kind is None or _LIVE_KIND.get(s["agent"]) == kind]
    if len(found) != 1:
        sys.stderr.write(f"roster bind --live {name}: {len(found)} live sessions match (need exactly 1)\n")
        return None
    sess = found[0]
    out = {"kind": _LIVE_KIND[sess["agent"]], "id": sess["conversation"], "argv": None}
    if out["kind"] == "codex":
        from coordinator_core.agent_pipe.codex import CodexAdapter

        out["argv"] = CodexAdapter().proxy_argv()
    return out


def _roster_cmd(rest: List[str]) -> int:
    import argparse

    from coordinator_core.agent_pipe import roster
    from coordinator_core.agent_pipe.contract import KINDS

    ap = argparse.ArgumentParser(prog="agent_pipe roster")
    sub = ap.add_subparsers(dest="verb", required=True)
    b = sub.add_parser("bind", help="bind a durable name to a session, replacing any prior binding")
    b.add_argument("name")
    b.add_argument("--kind", choices=KINDS)
    b.add_argument("--id")
    b.add_argument("--token")
    b.add_argument("--argv", help="JSON list: app-server argv for a codex binding")
    b.add_argument("--live", action="store_true", help="pick the current id from the live session")
    r = sub.add_parser("resolve")
    r.add_argument("name")
    sub.add_parser("list")
    args = ap.parse_args(rest)
    try:
        if args.verb == "bind":
            kind, sid, argv = args.kind, args.id, json.loads(args.argv) if args.argv else None
            if args.live:
                picked = _live_binding(args.name, kind)
                if picked is None:
                    return 1
                kind, sid, argv = picked["kind"], picked["id"], argv or picked["argv"]
            if not kind or not sid:
                sys.stderr.write("roster bind: --kind and --id (or --live) are required\n")
                return 2
            print(json.dumps(roster.bind(args.name, kind, sid, args.token, argv)))
        elif args.verb == "resolve":
            binding = roster.resolve(args.name)
            print(json.dumps(binding))
            return 0 if binding else 1
        else:
            print(json.dumps(roster.list()))
    except ValueError as exc:
        sys.stderr.write(f"roster {args.verb}: {exc}\n")
        return 2
    return 0


def _bridge_cmd(rest: List[str]) -> int:
    import argparse

    from coordinator_core.agent_pipe import bridge

    ap = argparse.ArgumentParser(prog="agent_pipe bridge")
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("send", help="queue a labelled message for the resident bridge")
    s.add_argument("--from", dest="from_name", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("text", help="message text, or - to read stdin")
    s.add_argument("--now", action="store_true", help="route immediately instead of queueing")
    sv = sub.add_parser("serve", help="resident router loop")
    sv.add_argument("--max-cycles", type=int)
    rt = sub.add_parser("route", help="PM routing control")
    rt.add_argument("--pause", nargs=2, metavar=("FROM", "TO"))
    rt.add_argument("--resume", nargs=2, metavar=("FROM", "TO"))
    rt.add_argument("--mute", metavar="NAME")
    rt.add_argument("--unmute", metavar="NAME")
    args = ap.parse_args(rest)
    try:
        if args.verb == "send":
            text = sys.stdin.read() if args.text == "-" else args.text
            msg = bridge.make_message(args.from_name, args.to, text)
            if args.now:
                delivery = bridge.route(msg)
                delivery.wait()
                print(json.dumps({"status": delivery.status, "reason": delivery.reason, "id": msg.id}))
                return 0 if delivery.status in ("delivered", "handed-off") else 1
            print(bridge.enqueue(msg))
        elif args.verb == "serve":
            try:
                bridge.serve(max_cycles=args.max_cycles)
            except KeyboardInterrupt:
                pass
        else:
            data = None
            if args.pause:
                data = bridge.set_pause(*args.pause)
            if args.resume:
                data = bridge.set_pause(*args.resume, paused=False)
            if args.mute:
                data = bridge.set_mute(args.mute)
            if args.unmute:
                data = bridge.set_mute(args.unmute, muted=False)
            if data is None:
                data = bridge._load_routes()
            print(json.dumps(data))
    except ValueError as exc:
        sys.stderr.write(f"bridge {args.verb}: {exc}\n")
        return 2
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "roster":
        return _roster_cmd(args[1:])
    if args and args[0] == "bridge":
        return _bridge_cmd(args[1:])
    usage = "usage: python -m coordinator_core.agent_pipe <run_dir> | roster ... | bridge ...\n"
    if args and args[0] in ("-h", "--help"):
        sys.stdout.write(usage)
        return 0
    if len(args) != 1 or args[0].startswith("-"):
        sys.stderr.write(usage)
        return 2
    run_worker(Path(args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
