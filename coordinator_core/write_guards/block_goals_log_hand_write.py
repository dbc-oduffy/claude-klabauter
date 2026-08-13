"""coordinator_core.write_guards.block_goals_log_hand_write — hard-deny guard.

Path-matched deny on hand-edits to the per-machine goals-log append-only
JSONL wire (``<central_state_root>/goals-log.<machine>.jsonl``). The wire's
sole authoritative writer is ``coordinator_core.ops.goal_append.append_goal``
(``goal.append`` op, CLI trampoline
``coordinator/bin/append-goal-event.py``) — it derives the content-hash
``goal_id``, validates the ``status`` enum against the contract's
``GoalStatus`` Literal, normalizes ``coordinator_root_path`` to
repo-root-relative, and quarantines a malformed ``weekly_perceptible`` /
``key_results_status`` before the row ever reaches disk.

Every sibling substrate wire in this repo already closes this exact hole:
``block_tracker_edit.py``, ``block_memo_status_hand_edit.py``,
``block_priority_ledger_edit.py``, ``block_home_dir_memo_delivery.py``. The
goals wire was the only one without one — per ``block_priority_ledger_edit.
py``'s own docstring, adding a guard of this shape "is not a novel
escalation."

This guard's match, like its siblings, is deliberately a path-TAIL regex,
not a resolve-and-compare against the central state root at check-time:
guards match conditions, not containers, and resolving the root would cost
a subprocess on every Write/Edit/MultiEdit/NotebookEdit in the fleet. The
varying parts of the physical path are the machine slug and the root
prefix — neither is literal here; only the constant
``goals-log.<machine>.jsonl`` filename shape is matched, mirroring
``block_tracker_edit.py`` / ``block_priority_ledger_edit.py``'s own
discipline exactly, including the backslash/slash-run normalization.

Design-as-offers (load-bearing, not stylistic): the denial reason LEADS
with the writer to call instead (`append-goal-event.py` / the `goal.append`
op — "did you mean this?"), names concretely what a hand-write silently
loses (the `goal_id` content hash, so the row can never be superseded by a
later close leg; the status-enum and root-path validation), and only then
gives the override.

Negative-spec:
  - Does NOT match any OTHER per-machine or per-repo JSONL shard under
    central state — scope is exactly the ``goals-log.<machine>.jsonl``
    filename shape, nothing adjacent.
  - Does NOT resolve the central root at check-time (see rationale above) —
    a path-tail match on the constant filename segment only.
  - Does NOT read stdin — the engine passes ``payload`` directly.
  - Never raises: any unexpected input shape or internal error is treated as
    ALLOW (fail-open on error), matching every sibling hard-deny guard's
    fail-open-on-error discipline.

Spec backlink: coordinator_core/ops/goal_append.py (module docstring)
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 65

#: Escape hatch — recovery-only, mirrors the sibling guards' override pattern.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_GOALS_LOG_WRITE"

#: Path-tail match: any ``goals-log.<machine>.jsonl`` file, regardless of the
#: resolved central root's machine-local prefix. The machine slug is
#: ``[^/]+`` — deliberately not validated against
#: ``goal_append._machine_slug()``'s exact charset, since this guard must
#: match the filename SHAPE, not re-derive the writer's own slug logic.
_GOALS_LOG_RE = re.compile(r"(^|/)goals-log\.[^/]+\.jsonl$")


def _extract_file_path(tool_name: str, tool_input: Dict[str, Any]) -> str:
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path") or ""
    return tool_input.get("file_path") or ""


def _normalize(file_path: str) -> str:
    """Backslash -> forward slash, then collapse slash runs (parity with
    `block_tracker_edit.py`'s F5-fixed normalizer)."""
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        # Honor escape hatch first.
        if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        file_path = _extract_file_path(tool_name, tool_input)
        if not file_path:
            return None

        file_path_norm = _normalize(file_path)

        if not _GOALS_LOG_RE.search(file_path_norm):
            return None

        _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
        reason = (
            "Use instead:\n"
            f"  {file_path} is append-only disk-truth; a hand-write skips the "
            "goal_id content hash and status/path validation, corrupting "
            "supersession -- run append-goal-event.py (goal.append) instead."
            + ("\n\n" + _note if _note else "")
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    except Exception:
        # Fail-open on any unexpected error — mirrors every sibling guard's
        # fail-open-on-error discipline (never fail-closed on a hard guard's
        # own internal error).
        return None
