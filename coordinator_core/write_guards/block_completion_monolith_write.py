"""coordinator_core.write_guards.block_completion_monolith_write — advisory guard.

Originally a Python engine-ification of DoE's retired
``coordinator/hooks/scripts/block-completion-monolith-write.sh`` PreToolUse
(Write|Edit|NotebookEdit|MultiEdit) hook (deleted 2026-07-16, DoE
``2f8b8450``), per the naked-Python hook migration (write_guards/INTERFACE.md).

Purpose (ported verbatim from the reference hook, deny condition unchanged):
flags runtime writes to the legacy monolith shape
``archive/completed/<YYYY-MM>.md``, which Phase 1 of the completion-log
release-loop replaced with per-entry files under
``archive/completed/YYYY-MM/YYYY-MM-DD-<chain-slug>-<sid6>.md``. A static-grep
tripwire (``coordinator_core.ops.check_no_monolith_completion_append``) catches
literal references to the monolith path in source files, and a git-diff
review at commit time catches a stray monolith write before it lands —
neither the tripwire nor review depend on this guard having denied the write
outright.

CLASS = "advisory" (2026-08-06 write-guard classification pass,
docs/plans/2026-08-06-... B5): reclassified from hard-deny. The harm this
guard flags is a MISROUTED write, not a silent or total loss — the target
path is a plain file the writer fully controls, nothing downstream treats it
as authoritative before a human or the static-grep tripwire can catch it, and
the correction is a second write to the right path, not a recovery from lost
work. That is the "irreversible harm" bar this family's hard-deny band is
reserved for (see this package's classification test), and this guard does
not clear it — the discharge test/tripwire.

This is otherwise a faithful port: it preserves the reference hook's escape
hatch, the tool_name pre-filter, the backslash/slash-run normalization (F5
fix), the ``archive/completed/<YYYY-MM>.md`` tail match (never matching
``legacy/`` or per-entry-subdir forms, by construction of the regex), and the
reason text verbatim — only the envelope shape (advisory, not deny) and the
lead-in sentence changed.

Ported from the retired DoE bash guard ``block-completion-monolith-write.sh``
  (deleted 2026-07-16, DoE ``2f8b8450``).

Negative-spec:
  - Does NOT block writes under ``archive/completed/legacy/`` (post-migration
    canonical home for frozen pre-migration history) — the ``legacy/`` segment
    means the trailing-tail regex never matches.
  - Does NOT block writes under ``archive/completed/YYYY-MM/*.md`` (the
    correct Phase 1 per-entry subdir form) — the extra path segment after
    ``YYYY-MM`` means the trailing-tail regex never matches.
  - Does NOT read stdin — the engine passes ``payload`` directly.
  - Does NOT deny — advisory only; the write always lands, with the
    alternative path surfaced via ``additionalContext``.
  - Never raises: any unexpected input shape or internal error is treated as
    ALLOW/no-op (fail-open on error), matching the reference hook's
    ``set -uo pipefail`` fail-open discipline.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 171  # advisory band; next slot after nudge_prose_queue_append (170)

#: Escape hatch (reference hook lines 25-27, 44-46).
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_COMPLETION_MONOLITH"

#: Legacy monolith tail match: archive/completed/<YYYY-MM>.md (reference hook
#: line 96). Never matches archive/completed/legacy/* (extra segment) or
#: archive/completed/YYYY-MM/*.md (extra segment after YYYY-MM).
_MONOLITH_RE = re.compile(r"archive/completed/[0-9]{4}-[0-9]{2}\.md$")


def _extract_file_path(tool_name: str, tool_input: Dict[str, Any]) -> str:
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path") or ""
    return tool_input.get("file_path") or ""


def _normalize(file_path: str) -> str:
    """Backslash -> forward slash, then collapse slash runs (reference hook
    lines 74-87 / F5 fix)."""
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        # Honor escape hatch first (reference hook lines 43-46).
        if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        file_path = _extract_file_path(tool_name, tool_input)
        if not file_path:
            return None

        file_path_norm = _normalize(file_path)

        if not _MONOLITH_RE.search(file_path_norm):
            return None

        _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
        reason = (
            "Use instead:\n"
            "  archive/completed/YYYY-MM.md is the retired monolith shape "
            "(Phase 1 moved it to per-entry files) -- write "
            "archive/completed/YYYY-MM/YYYY-MM-DD-<slug>-<sid6>.md instead, "
            "or archive/completed/legacy/YYYY-MM.md for a frozen "
            "pre-migration edit."
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
        # fail-open-on-error discipline (never fail-closed on a hard guard's
        # own internal error).
        return None
