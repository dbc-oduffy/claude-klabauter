"""coordinator_core.bash_guards.guard_disk_fill -- PreToolUse(Bash | PowerShell)
guard that denies a bulk write or copy whose declared or estimated output would
leave the target volume under ``disk_budget.Policy.volume_free_floor``.

POSTURE -- box-degrading deny, strict by ``machine_profile.GUARD_DEFAULT_LEVEL``
and deliberately not a ``FLOOR_GUARDS`` member: ``coordinator.guard_level`` or
the per-guard key lowers it to a warning. Every command that is not a bulk
write, and every bulk write that cannot be sized, is allowed. Classification,
sizing and the deny text live in ``_disk_fill_contract``.
"""

from __future__ import annotations

import shutil
from typing import Any, Callable, Dict, Optional

from coordinator_core.bash_guards import _disk_fill_contract as _contract
from coordinator_core.bash_guards._dialect import dialect_from_tool_name
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 43


def check(
    payload: Dict[str, Any],
    resolve_wiki_citation: Optional[Callable[[str], str]] = None,
) -> Any:
    """None (allow) or a deny record for the first bulk write that breaches the floor.

    Any internal failure allows: this guard never blocks on its own malfunction.
    """
    del resolve_wiki_citation
    if not isinstance(payload, dict) or payload.get("tool_name") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input")
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    try:
        dialect = dialect_from_tool_name(payload["tool_name"])
        policy = None
        for bulk in _contract.iter_bulk_writes(cmd.replace("\r", ""), dialect):
            est = _contract.estimate_output_bytes(bulk, cwd)
            if est is None:
                continue
            if policy is None:
                policy = _contract.load_policy()
            verdict = _contract.verdict(
                bulk.target, est, policy, disk_usage=_disk_usage, cwd=cwd
            )
            if verdict.deny:
                return _deny_record(verdict.text)
    except Exception:
        return None
    return None


def _disk_usage(path: Any) -> Any:
    # Late-bound: the contract's `disk_usage=shutil.disk_usage` default is frozen at import.
    return shutil.disk_usage(path)


def _deny_record(text: str) -> Any:
    from coordinator_core.guard_messages.alternative_record import (
        AdaptableOffer,
        AlternativeRecord,
    )

    return AlternativeRecord(
        verdict_kind="deny",
        guard_name=_contract.GUARD_NAME,
        summary=text,
        members=(
            AdaptableOffer(
                template="write to a volume with room",
                what_to_adapt="the destination path",
            ),
        ),
        em_text=text,
    )
