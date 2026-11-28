"""coordinator_core.bash_guards.refuse_unrecordable_detached_spawn -- PreToolUse(Bash,
PowerShell) deny: no detached launch whose command the spawn ledger cannot name.

A detached job (`nohup`, `setsid`, `disown`, trailing `&`, `Start-Process`) outlives its shell and is
reparented out of the session's process tree; the ledger recorder attributes it by corroborating
the launch text against the new process's argv. A detached stage headed by `eval`, a `$VAR` or
`$(...)` expansion, or a non-literal `-c` body gives it nothing to corroborate, so the job could
never be stopped by its owner. `_spawn_ledger.detach_shape` decides; only "opaque" denies.

In-process, no spawn: `_PREFILTER` returns at once on text naming no detach word. Any internal
error allows.
"""

from __future__ import annotations

from typing import Any, Dict

from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 44

GUARD_NAME = "refuse-unrecordable-detached-spawn"
ANCHOR = "REFUSE-UNRECORDABLE-DETACHED-SPAWN"

#: Lowercase substrings a command must contain for this guard to possibly fire;
#: the crash-path trigger table reads them.
COMMAND_WORDS = ("&", "nohup", "setsid", "disown", "start-process", "saps")

_PREFILTER_WORDS = COMMAND_WORDS


def _prefilter(command: str) -> bool:
    low = command.lower()
    return any(word in low for word in _PREFILTER_WORDS)


def _deny_record() -> Any:
    from coordinator_core.bash_guards._rewrite_support import _deny_record as _build
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer

    text = (
        f"BLOCKED {ANCHOR}: detached launch with a non-literal command cannot be ledgered; "
        "launch the literal command, or record it: "
        "coordinator-invoke proc.stop_own_child '{\"pid\":<pid>,\"record\":true}'."
    )
    return _build(
        GUARD_NAME,
        text,
        em_text=text,
        members=(
            AdaptableOffer(
                template="nohup <program> <args> & (the program and its arguments spelled out, no $VAR or eval)",
                what_to_adapt="<program> <args>, the literal command to detach",
            ),
        ),
    )


def check(payload: Dict[str, Any]) -> Any:
    """The chain verdict for one PreToolUse payload: a deny record, or None to allow."""
    try:
        if (payload.get("tool_name") or "") not in MATCHERS:
            return None
        tool_input = payload.get("tool_input") or {}
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str) or not _prefilter(command):
            return None
        from coordinator_core.ops._spawn_ledger import detach_shape

        if detach_shape(command) != "opaque":
            return None
        return _deny_record()
    except Exception:
        return None
