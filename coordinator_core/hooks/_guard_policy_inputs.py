"""The two per-call inputs the Bash guard chain takes beside the payload: the caller's sandbox policy file and
this engine's resolution class.

Same contract as `ops.warm_guard_evaluate._policy_file_for` / `_engine_resolution_class`; this module is the
cold hook child's copy because importing that op module drags `asyncio` into a process that otherwise needs no
event loop. NEVER MEMOIZE `policy_file_for` (a caller fact); `engine_resolution_class` is one fact per process
and is cached, a failed resolution cached as `""` so a failing box does not re-pay the shim load per call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

_RESOLUTION_CLASS: Optional[str] = None


def engine_resolution_class() -> Optional[str]:
    """This engine's own resolution class, or None when it cannot be resolved."""
    global _RESOLUTION_CLASS
    if _RESOLUTION_CLASS is None:
        try:
            from coordinator_core.engine_root import coordinator_engine_root_with_class

            _RESOLUTION_CLASS = coordinator_engine_root_with_class()[1] or ""
        except Exception:  # noqa: BLE001 -- negative-cache; never re-pay the shim load per call
            _RESOLUTION_CLASS = ""
    return _RESOLUTION_CLASS or None


def policy_file_for(payload: Dict[str, Any]) -> Optional[str]:
    """DoE's `subagent-sandbox-policy.yaml` path for this call's caller, from the payload's `plugin_root`;
    None on a miss (the guard then falls back to its hardcoded default ruleset)."""
    from coordinator_core.warm.caller_context import resolve_caller_context

    plugin_root = resolve_caller_context(payload).plugin_root
    if not plugin_root:
        return None
    return str(Path(plugin_root) / "subagent-sandbox-policy.yaml")
