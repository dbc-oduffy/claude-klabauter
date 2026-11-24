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
from .render import render
from coordinator_core.group_em.atomic_record import holder_lock
from coordinator_core._settings_home import settings_home

EVENTS = ("Stop", "SubagentStop", "PostCompact")
# TRAP: a cold proxy spawn plus upgrade/initialize measures ~0.6s on Windows; a 0.2s handshake
# never completes ("no upgrade reply") and the title silently never lands. This runs off the
# hook's wait (warm op thread or detached child), so the bound can cover a cold spawn; the
# worst case is HANDSHAKE_TIMEOUT_S + REQUEST_TIMEOUT_S, enforced by _connect and the request deadline.
HANDSHAKE_TIMEOUT_S = 5.0
REQUEST_TIMEOUT_S = 2.0


def status_title(value: dict) -> str:
    return render(value)


def apply_title(value: dict, cwd: str) -> dict:
    """One bounded metadata transaction, only in the confirmed loaded namespace."""
    sid = value["session_id"]
    title = status_title(value)
    preserve = getattr(external_registry, "preserve_task_title", None)
    if preserve is None:
        return {"updated": False, "reason": "owned original-task preservation API is unavailable"}
    argv = CodexAdapter().proxy_argv()
    with codex_app_server._connect(argv, timeout=HANDSHAKE_TIMEOUT_S) as connection:
        deadline = time.monotonic() + REQUEST_TIMEOUT_S
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


def record_result(sid: object, event: str, result: dict) -> None:
    """Persist the last refresh outcome (reason included) so a failed title is never invisible."""
    try:
        key = str(uuid.UUID(sid)) if isinstance(sid, str) else None
        if key is None:
            return
        directory = settings_home() / "state" / "codex-status"
        directory.mkdir(parents=True, exist_ok=True)
        entry = {"event": event, "at": time.time(), "updated": bool(result.get("updated")),
                 "reason": result.get("reason"), "title": result.get("title")}
        (directory / f"{key}.last-refresh.json").write_text(json.dumps(entry), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 -- diagnostics never fail the refresh
        print(f"codex status: refresh outcome not recorded: {type(exc).__name__}: {exc}", file=sys.stderr)


def handle(payload: dict, event: str) -> dict:
    """Run one status refresh and record its outcome; emits no hook stdout."""
    result = _handle(payload, event)
    record_result(payload.get("session_id") if isinstance(payload, dict) else None, event, result)
    return result


def _handle(payload: dict, event: str) -> dict:
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
        # A missing alias renders the `unregistered` label; a cwd or a guessed
        # UUID suffix is never substituted as a SendMessage identity.
        cache = Path(value["cache_path"])
        # Cached application history cannot establish the currently displayed title.
        # apply_title reads current native metadata before deciding unchanged.
        try:
            result = apply_title(value, cwd)
        except Exception as exc:
            return {"updated": False, "reason": f"{type(exc).__name__}: {exc}", "snapshot": value}
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
        if not result.get("updated") and result.get("reason") != "unchanged":
            print(f"codex status: {result.get('reason')}", file=sys.stderr)
    except Exception as exc:
        print(f"codex status: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
