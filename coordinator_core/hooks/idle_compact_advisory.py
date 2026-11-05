"""coordinator_core.hooks.idle_compact_advisory — UserPromptSubmit advisory for a
watcher-injected ``/compact``.

The idle-compaction watcher types ``/compact`` into an idle EM's console, so the
prompt arrives looking PM-typed. When the newest ``injected`` record for the
session in ``injections.jsonl`` is fresh, this op tells the session the
compaction was machine-injected and names the run id. Advisory only: it never
denies, and any failure returns no advisory.

``fresh_injection_run_id`` is the shared reader; ``context_pressure_precompact``
uses it for its steering line.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from coordinator_core._hook_envelope import no_advisory, payload_of
from coordinator_core.hooks._envelope import context_only
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op

GENERATES: list = []

FRESH_SECONDS = 300
_TAIL_BYTES = 65536
_INJECTED = "injected"


def _parse_ts(ts: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def fresh_injection_run_id(session_id: str, now: Optional[float] = None) -> Optional[str]:
    """Run id of the newest ``injected`` log record for ``session_id`` when it is
    younger than ``FRESH_SECONDS``; ``None`` otherwise. Never raises."""
    try:
        from coordinator_core.idle_compact.contract import injection_log_path

        path = injection_log_path()
        if not session_id or not path.is_file():
            return None
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (not isinstance(record, dict) or record.get("session_id") != session_id
                    or record.get("action") != _INJECTED):
                continue
            stamp = _parse_ts(str(record.get("ts") or ""))
            if stamp is None:
                return None
            current = datetime.now(timezone.utc).timestamp() if now is None else now
            if current - stamp > FRESH_SECONDS:
                return None
            run_id = str(record.get("run_id") or "")
            return run_id or None
        return None
    except Exception:
        return None


def _is_compact_prompt(prompt: str) -> bool:
    head = prompt.strip().split(None, 1)
    return bool(head) and head[0] == "/compact"


@register_op("hooks.idle_compact_advisory")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        if not _is_compact_prompt(field(params, "prompt")):
            return no_advisory()
        run_id = fresh_injection_run_id(field(params, "session_id"))
        if run_id is None:
            return no_advisory()
        return context_only(
            "UserPromptSubmit",
            f"This /compact was injected by the idle-compaction watcher (run {run_id}), "
            "not typed by the PM. Preserve in-flight engine-ask run ids across it.",
        )
    except Exception:
        return no_advisory()
