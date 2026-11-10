"""Event-driven status refresh; no monitor, thread resume, or service repair."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

from coordinator_core.agent_pipe import codex_app_server
from coordinator_core.agent_pipe.codex import CodexAdapter
from coordinator_core.session import external_registry
from .__main__ import snapshot
from .collect import load, save, bind_subagent_stop
from .model import warn
from coordinator_core.group_em.atomic_record import holder_lock
from coordinator_core._settings_home import settings_home

EVENTS = ("Stop", "SubagentStop", "PostCompact")


def status_title(value: dict) -> str:
    alias = value.get("alias")
    if not alias:
        raise ValueError("no exact registered route alias")
    cost = f"~${value['usd']:.2f}" if value.get("priced_events") else "unknown"
    partial = (not value.get("backlog_complete") or value.get("unpriced_events") or
               value.get("warnings") or not all(value.get("coverage", {}).values()))
    return f"{alias} · {cost}" + ("+" if partial else "")


def apply_title(value: dict, cwd: str) -> dict:
    """One bounded metadata transaction, only in the confirmed loaded namespace."""
    sid = value["session_id"]
    title = status_title(value)
    preserve = getattr(external_registry, "preserve_task_title", None)
    if preserve is None:
        return {"updated": False, "reason": "owned original-task preservation API is unavailable"}
    argv = CodexAdapter().proxy_argv()
    with codex_app_server._connect(argv, timeout=0.2) as connection:
        deadline = time.monotonic() + 0.2
        loaded = connection.request("thread/loaded/list", {"limit": 256}, deadline) or {}
        if sid not in loaded.get("data", []):
            return {"updated": False, "reason": "thread is not in this loaded app-server namespace"}
        response = connection.request("thread/read", {"threadId": sid, "includeTurns": False}, deadline) or {}
        actual = response.get("thread") or {}
        if actual.get("id") != sid or not isinstance(actual.get("cwd"), str) or os.path.normcase(os.path.realpath(actual["cwd"])) != os.path.normcase(os.path.realpath(cwd)):
            return {"updated": False, "reason": "thread identity/repository did not match the status event"}
        # The identity owner performs the atomic metadata update. A status title
        # never becomes the source of truth for the task that was assigned.
        record = preserve(sid, actual.get("name"))
        if not isinstance(record, dict) or record.get("session_id") != sid or "original_task_title" not in record:
            return {"updated": False, "reason": "original task title was not durably preserved"}
        if actual.get("name") == title:
            return {"updated": False, "reason": "unchanged", "title": title}
        connection.request("thread/name/set", {"threadId": sid, "name": title}, deadline)
    return {"updated": True, "title": title}


def handle(payload: dict, event: str) -> dict:
    """Lifecycle launcher API: consumes supplied data, emits no hook stdout."""
    if event not in EVENTS:
        return {"updated": False, "reason": "not a status refresh event"}
    try:
        sid, cwd = payload.get("session_id"), payload.get("cwd")
        if not isinstance(sid, str) or not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return {"updated": False, "reason": "event lacks a genuine session identity/repository"}
        sid = str(uuid.UUID(sid))
        paths = []
        for key in ("transcript_path", "agent_transcript_path"):
            raw = payload.get(key)
            if isinstance(raw, str) and Path(raw).is_absolute():
                paths.append(raw)
        ownership = None
        child_path = payload.get("agent_transcript_path")
        if event == "SubagentStop" and isinstance(child_path, str) and Path(child_path).is_absolute():
            cache = settings_home() / "state" / "codex-status" / f"{sid}.json"
            with holder_lock(cache, timeout=0.05, poll_interval=0.005):
                state = load(cache, sid)
                try:
                    ownership = bind_subagent_stop(state, sid, Path(child_path))
                except (OSError, ValueError, TypeError, AttributeError):
                    warn(state, "SubagentStop child ownership could not be confirmed from its transcript")
                save(cache, state)
        value = snapshot({"session_id": sid, "rollouts": paths})
        if ownership is not None:
            value["subagent_stop_ownership"] = ownership
        # No alias means no title write. A cwd or a guessed UUID suffix is not
        # a readable SendMessage identity.
        if not value.get("alias"):
            return {"updated": False, "reason": "no exact registered route alias", "snapshot": value}
        cache = Path(value["cache_path"])
        # Cached application history cannot establish the currently displayed title.
        # apply_title reads current native metadata before deciding unchanged.
        result = apply_title(value, cwd)
        if result.get("updated"):
            with holder_lock(cache, timeout=0.05, poll_interval=0.005):
                state = load(cache, sid)
                state["last_applied_title"] = result["title"]
                save(cache, state)
        return {**result, "snapshot": value}
    except Exception as exc:
        return {"updated": False, "reason": f"{type(exc).__name__}: {exc}"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", choices=EVENTS, required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        result = handle(payload, args.event)
        if not result.get("updated") and result.get("reason") not in {"unchanged", "no exact registered route alias"}:
            print(f"codex status: {result.get('reason')}", file=sys.stderr)
    except Exception as exc:
        print(f"codex status: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
