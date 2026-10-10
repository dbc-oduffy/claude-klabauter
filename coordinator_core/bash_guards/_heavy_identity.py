"""Identity leg of guard-heavy-command-admission: may this caller launch heavy work at all?

Keys on payload agent_id / agent_type / transcript_path alone. Unlike
check_test_suite_invocation._caller_is_subagent, a bare agent_type (main thread under --agent)
with no agent_id is still the main thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Dict, FrozenSet, Optional

from coordinator_core.bash_guards._heavy_admission_contract import ALLOWLIST_PATH


@dataclass(frozen=True)
class IdentityVerdict:
    allowed: bool
    reason: str = ""


_ALLOW = IdentityVerdict(True)


def load_allowlist(path: Optional[Path] = None) -> FrozenSet[str]:
    """Exact agent_type lines of the allow-list file; unreadable or missing yields empty."""
    try:
        text = (path or ALLOWLIST_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return frozenset()
    types = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            types.add(line)
    return frozenset(types)


def _has_agent_id(payload: Dict[str, Any]) -> bool:
    raw = payload.get("agent_id")
    if isinstance(raw, str):
        return bool(raw.strip())
    return bool(raw)


def _transcript_in_subagents(payload: Dict[str, Any]) -> bool:
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str):
        return False
    return "subagents" in transcript.replace("\\", "/").split("/")[:-1]


def identity_verdict(
    payload: Dict[str, Any],
    *,
    allowlist: Collection[str],
    workflow_runs: Collection[str] = (),
) -> IdentityVerdict:
    """Allow the main thread, or a caller whose agent_type exactly matches allowlist with no live
    workflow run in workflow_runs; deny everything else, naming the cause."""
    if not _has_agent_id(payload):
        if _transcript_in_subagents(payload):
            return IdentityVerdict(False, "subagent transcript without agent_id")
        return _ALLOW
    if not isinstance(payload.get("agent_id"), str):
        return IdentityVerdict(False, "unparseable agent_id")
    agent_type = payload.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type or agent_type == "unknown":
        return IdentityVerdict(False, "agent_type missing or unknown")
    if agent_type not in allowlist:
        return IdentityVerdict(False, f"agent_type {agent_type} not on the heavy-command allow-list")
    if workflow_runs:
        return IdentityVerdict(False, f"agent_type {agent_type} inside a live workflow run")
    return _ALLOW
