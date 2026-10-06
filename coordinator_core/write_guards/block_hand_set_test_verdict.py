"""coordinator_core.write_guards.block_hand_set_test_verdict -- denies a write
that sets a test verdict (``test_verdict:`` or a terminal ``status:``) in a
run-report sidecar leaf unless the agent the leaf names makes it.

Purpose: the review-stamp mint trusts the verdict a test-runner sidecar
carries, so a hand-set verdict is a hand-stamped attestation. The sanctioned
route is ``test-verdict record``, which writes through Python ``open()`` and is
never a PreToolUse event.

Leg 1 (pure string, no I/O except one read of an existing file for Write):
the target is a ``<label>.<agent_id>.md`` sidecar leaf AND the incoming text
carries a verdict line that the edit does not leave unchanged.
Leg 2: no ``agent_id`` (EM main loop) denies; a caller whose raw or resolved id
equals the leaf's id allows; a positively different resolved id denies; an
unresolvable identity allows (fail-open, sibling posture).

Negative-spec:
  - Does NOT import ``completion_receipts.test_verdict`` (yaml is too heavy for
    leg 1); the verb is re-stated as a literal and pinned equal to
    ``RECORD_VERB`` by test.
  - Does NOT deny a write that leaves an existing verdict line unchanged.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from coordinator_core.write_guards._subagent_identity import _resolve_subagent_identity
from coordinator_core.write_guards.block_foreign_family_sidecar_write import (
    _extract_file_path,
    _normalize_path,
    _split_sidecar_leaf,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit"]
#: Next free HARD-DENY slot after block_foreign_family_sidecar_write's 31.
PRIORITY = 32
GENERATES = []

_RECORD_VERB = "test-verdict record"

_VERDICT_LINE_RE = re.compile(
    r"^(?:test_verdict\s*:|status\s*:\s*['\"]?(?:pass|fail|failed|error|errored|not_run)\b)",
    re.MULTILINE,
)


def _verdict_lines(text: Any) -> List[str]:
    if not isinstance(text, str):
        return []
    return [line for line in text.splitlines() if _VERDICT_LINE_RE.match(line)]


def _new_verdict_lines(new: Any, old: Any) -> List[str]:
    """Verdict lines in ``new`` that ``old`` does not already carry verbatim."""
    existing = set(_verdict_lines(old))
    return [ln for ln in _verdict_lines(new) if ln not in existing]


def _read_existing(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _sets_verdict(tool_name: str, tool_input: Dict[str, Any], path: str) -> bool:
    if tool_name == "Write":
        new_lines = _verdict_lines(tool_input.get("content"))
        if not new_lines:
            return False
        return bool(_new_verdict_lines(tool_input.get("content"), _read_existing(path)))
    if tool_name == "Edit":
        return bool(_new_verdict_lines(tool_input.get("new_string"), tool_input.get("old_string")))
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        return any(
            isinstance(e, dict) and _new_verdict_lines(e.get("new_string"), e.get("old_string"))
            for e in edits
        )
    return False


def _deny(path: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "BLOCKED: a sidecar's test verdict is set by its own runner or by %s.\n"
                "  %s\n"
                "Record it: %s --result-json <runner result>." % (_RECORD_VERB, path, _RECORD_VERB)
            ),
        }
    }


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return None (allow) or the hard-deny envelope."""
    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    file_path = _extract_file_path(payload)
    if not file_path:
        return None
    normalized = _normalize_path(file_path)
    leaf_info = _split_sidecar_leaf(normalized)
    if leaf_info is None:
        return None
    disk_path = file_path
    if not os.path.isabs(disk_path) and payload.get("cwd"):
        disk_path = os.path.join(payload["cwd"], file_path)
    if not _sets_verdict(tool_name, tool_input, disk_path):
        return None

    raw_agent_id = payload.get("agent_id") or ""
    if not raw_agent_id:
        return _deny(normalized)
    resolved = _resolve_subagent_identity(raw_agent_id, payload.get("session_id") or "")
    if not resolved:
        return None
    leaf_id = leaf_info["agent_id"].casefold()
    for own_id in (resolved, raw_agent_id):
        if own_id and leaf_id == own_id.casefold():
            return None
    return _deny(normalized)
