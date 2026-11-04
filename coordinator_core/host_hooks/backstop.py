"""Engine-independent backstop: irreversible-external guards run in-process, with no warm engine.

`match(host_event)` returns a deny verdict when an irreversible-external bash guard refuses the
event's shell command, else None. Guards are imported lazily; one that raises is skipped (fails
open). Nothing here imports `coordinator_core.warm` or `coordinator_core.ops`.
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, Optional

from coordinator_core.host_hooks.contract import HostEvent

# Pinned against deny_registry.GUARD_DENY_CLASS by tests/test_backstop.py.
GUARD_MODULES = {
    "block-push-pr-non-owned-remote": "coordinator_core.bash_guards.block_push_pr_non_owned_remote",
    "block-perforce-submit": "coordinator_core.bash_guards.block_perforce_submit",
    "p4-verb-fence": "coordinator_core.bash_guards.p4_verb_fence",
}

_COMMAND_KEYS = ("command", "CommandLine", "cmd")


def _command(host_event: HostEvent) -> str:
    tool_input = host_event.tool_input or {}
    for key in _COMMAND_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _payload(host_event: HostEvent) -> Optional[Dict[str, Any]]:
    from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

    command = _command(host_event)
    if not command:
        return None
    tool_name = host_event.tool_name if host_event.tool_name in COMMAND_TOOL_NAMES else COMMAND_TOOL_NAMES[0]
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "cwd": host_event.cwd,
        "session_id": host_event.session_id,
    }


def _reason(record: Any, guard: str) -> str:
    text = getattr(record, "em_text", None) or getattr(record, "summary", None)
    if text:
        return str(text)
    if isinstance(record, dict):
        return str(record.get("reason") or record.get("message") or guard)
    return "BLOCKED by %s" % guard


def match(host_event: HostEvent) -> Optional[Dict[str, str]]:
    """The deny verdict `{"decision": "deny", "reason": ...}` for an irreversible command, else None."""
    try:
        payload = _payload(host_event)
    except Exception:  # noqa: BLE001 -- the backstop never breaks the hook
        return None
    if payload is None:
        return None
    for guard, module_name in GUARD_MODULES.items():
        try:
            record = importlib.import_module(module_name).check(dict(payload))
        except Exception:  # noqa: BLE001 -- a raising guard is skipped
            continue
        if record:
            return {"decision": "deny", "reason": _reason(record, guard)}
    return None
