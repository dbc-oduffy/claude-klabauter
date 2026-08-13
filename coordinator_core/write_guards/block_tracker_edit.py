"""coordinator_core.write_guards.block_tracker_edit — advisory guard.

Python engine-ification of example-doctrine-repo's retired
``coordinator/hooks/scripts/block-tracker-edit.sh`` PreToolUse
(Write|Edit|MultiEdit|NotebookEdit) hook (deleted 2026-07-16, example-doctrine-repo
``2f8b8450``), per the naked-Python hook migration (write_guards/INTERFACE.md).

Purpose (ported verbatim from the reference hook): blocks runtime Write/Edit
to the generated handoff tracker files (``state/handoff-tracker.md``,
``state/doe-handoff-tracker.md``). The tracker is a DISPOSABLE RENDER
produced by ``coordinator/bin/render-handoff-tracker.py`` from handoff
frontmatter (the single source of truth). A hand-edit is silently clobbered
on the next render and, if committed first, masquerades as source. This hook
keeps the render authoritative by redirecting edits back to the renderer.

Renderer note: the deny text names the live Python trampoline, NOT the
strangled ``bin/render-handoff-tracker.js`` the reference hook cited — that
node entry point no longer runs, so the ported-verbatim reason text was a
remediation instruction that could only crash. Its flag surface is
``[--root <path>] [--stdout]`` (see
``coordinator_core.ops.ceremony.render_handoff_tracker.main``); ``--root``
is optional (git-toplevel auto-discovery), and ``--stdout`` PRINTS instead
of writing, so the offer deliberately omits both — the operator wants the
regenerated file on disk. There is no ``--all-repos`` mode: it was removed
2026-07-23 (PM-ratified) and passing it exits 1 on the unknown-argument
branch.

This is a faithful engine-ification, not a redesign: it ports the reference
hook's escape hatch, the path-tail match (not full-prefix, so worktrees and
project moves work), the backslash/slash-run normalization, and the deny
reason text — the latter with one deliberate divergence, the renderer
command named in the offer (see the renderer note above).

Ported from the retired example-doctrine-repo bash guard ``block-tracker-edit.sh``
  (deleted 2026-07-16, example-doctrine-repo ``2f8b8450``).

CLASS is "advisory" per DR-277 (2026-08-06 guard-class census;
``docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md``) — was
"hard-deny" at PRIORITY 60. Re-slotted to PRIORITY 113 in the advisory phase
(``docs/wiki/write-guard-priority-bands.md``); the old hard-deny-phase number
carried no meaning across the phase boundary. On a match this now returns the
``additionalContext`` advisory envelope, never ``permissionDecision: "deny"``
— the tracker write proceeds, with the redirect-to-renderer offer surfaced
alongside it.

Negative-spec:
  - Does NOT match on ``tasks/`` — the reference hook explicitly notes this
    was a stale relic from before the ``state/``-vs-``tasks/`` split that
    silently disabled the guard; only ``state/handoff-tracker.md`` and
    ``state/doe-handoff-tracker.md`` (by path tail) match.
  - Does NOT read stdin — the engine passes ``payload`` directly.
  - Does NOT deny/block anything, per DR-277 — CLASS is advisory; the prior
    hard-deny behavior is retired, not merely softened in wording.
  - Never raises: any unexpected input shape or internal error is treated as
    ALLOW (fail-open on error), matching the reference hook's ``set -uo
    pipefail`` (``-e`` deliberately omitted) fail-open discipline.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note

CLASS = "advisory"  # DR-277 — was "hard-deny" at PRIORITY 60; re-slotted, not renamed.
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 113  # advisory phase — see docs/wiki/write-guard-priority-bands.md

#: Escape hatch.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_TRACKER_EDIT"

#: Path-tail match: state/handoff-tracker.md or state/doe-handoff-tracker.md.
_TRACKER_RE = re.compile(r"(^|/)state/(handoff-tracker|doe-handoff-tracker)\.md$")


def _extract_file_path(tool_name: str, tool_input: Dict[str, Any]) -> str:
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path") or ""
    return tool_input.get("file_path") or ""


def _normalize(file_path: str) -> str:
    """Backslash -> forward slash, then collapse slash runs (F5 fix parity
    with block-completion-monolith-write.sh, example-doctrine-repo ``2f8b8450``, 2026-07-16)."""
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

        if not _TRACKER_RE.search(file_path_norm):
            return None

        _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
        reason = (
            "OFFER: Use instead:\n"
            "  edit the handoff's frontmatter (the source of truth), then run:\n"
            "    python3 coordinator/bin/render-handoff-tracker.py\n\n"
            f"  {file_path} is a generated render — a hand-edit here is overwritten "
            "on the next render."
            + ("\n\n" + _note if _note else "")
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-open on any unexpected error — mirrors the reference hook's
        # `-e`-omitted, fail-open-on-error discipline (never fail-closed on
        # a hard guard's own internal error).
        return None
