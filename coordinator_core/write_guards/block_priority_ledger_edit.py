"""coordinator_core.write_guards.block_priority_ledger_edit — advisory guard.

Path-matched deny on hand-edits to the resolved central `priority-ledger/`
directory (`docs/plans/2026-07-26-priority-ledger.md` § Storage location,
chunk C9a). The ledger is central state (`state/ledgers/` per
`coordinator/docs/wiki/state-placement-law.md:36`), resolved through
`coordinator-state-root.py --central` — never a literal `state/
priority-ledger/` string baked into an op. This guard's OWN match, though,
is deliberately a path-TAIL regex (mirroring `block_tracker_edit.py`'s
shape exactly), not a resolve-and-compare against
`coordinator_core.state_root.coordinator_state_root(central=True)`: a
tail match survives worktree moves and avoids paying that resolver's
subprocess-fallback cost on every Write/Edit/MultiEdit/NotebookEdit in the
fleet, and the varying part of the physical root (the machine-local
Claude-klabauter-repo prefix) is exactly what a tail match never hardcodes — only the
constant `state/priority-ledger/` segment is literal, same discipline as
`block_tracker_edit.py`'s `state/(handoff-tracker|doe-handoff-tracker)\\.md`.

`priority.set` (CLI trampoline `coordinator/bin/priority-set.py`, mirroring
`set-goal-kr-status.py`'s naming) is the SOLE ledger writer (plan chunk C3):
it stamps `set_by`/`set_at`/`source` and writes atomically via
`locked_rmw`, with post-write schema validation gating the write. A
hand-edit bypasses all three — the exact failure shape
`block_tracker_edit.py`/`block_memo_status_hand_edit.py` already close for
their own disk-truth surfaces; this is not a novel escalation.

Design-as-offers (load-bearing, not stylistic — plan C9a): the denial reason
LEADS with the op to call instead ("did you mean `priority-set`?"), never a
bare refusal — a guard that only says no fights the agent's eagerness
instead of redirecting it.

Guards match CONDITIONS, not containers — a dedicated directory is what
makes this guard trivially correct, and is one of the reasons the PM
priority tier is a ledger directory rather than frontmatter (plan C9a body).

Negative-spec:
  - Does NOT match the intent inbox (a sibling directory under the same
    central root, per § Storage location) — scope is exactly
    `priority-ledger/`, nothing adjacent.
  - Does NOT resolve the central root at check-time (see rationale above) —
    a path-tail match on the constant directory segment only.
  - Does NOT read stdin — the engine passes `payload` directly.
  - Never raises: any unexpected input shape or internal error is treated as
    ALLOW (fail-open on error), matching every sibling advisory guard's
    `set -uo pipefail` (`-e` omitted)-equivalent fail-open discipline.
  - Does NOT return `permissionDecision: "deny"` — advisory envelope only
    (`additionalContext`), per DR-277 (guards are advisory by default) and
    the write-guard PRIORITY-band re-slot in
    `docs/wiki/write-guard-priority-bands.md`.

CLASS/PRIORITY history: flipped from `hard-deny` @ 65 to `advisory` @ 114 by
plan chunk C5 of `docs/plans/2026-08-06-apply-guard-class-census.md`, per
DR-277. This incidentally resolves the pre-existing hard-deny-phase 65/65
PRIORITY collision with `block_goals_log_hand_write` (which stays hard-deny,
AC6) by vacating slot 65. 114 is a fresh advisory-phase slot chosen against
that phase's own occupancy (`write-guard-priority-bands.md`'s nine-guard
slot map) — the old 65 carried no meaning across the phase boundary.

Spec backlink: DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 (chunk C9a);
docs/plans/2026-08-06-apply-guard-class-census.md (chunk C5);
docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 114  # advisory band; see docs/wiki/write-guard-priority-bands.md

#: Escape hatch — recovery-only, mirrors the sibling guards' override pattern.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_PRIORITY_LEDGER_EDIT"

#: Path-tail match: any file directly under a `.../state/priority-ledger/`
#: directory, regardless of the resolved central root's machine-local
#: prefix. Deliberately not anchored past the directory name — every entry
#: under it (today: `<target_id>.yaml`) is in scope, not just today's
#: extension.
_LEDGER_RE = re.compile(r"(^|/)state/priority-ledger/[^/]+$")


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

        if not _LEDGER_RE.search(file_path_norm):
            return None

        _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
        reason = (
            "Use instead:\n"
            f"  {file_path}: run `priority-set` (op `priority.set`) instead of "
            "hand-editing this disk-truth ledger — skips its provenance stamp, "
            "atomic write, and schema check."
            + ("\n\n" + _note if _note else "")
        )

        # Advisory envelope (DR-277) — additionalContext only, NEVER
        # permissionDecision:"deny". See INTERFACE.md § Envelope — advisory.
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-open on any unexpected error — mirrors every sibling guard's
        # fail-open-on-error discipline (never fail-closed on an advisory
        # guard's own internal error).
        return None
