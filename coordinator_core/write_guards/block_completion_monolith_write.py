"""coordinator_core.write_guards.block_completion_monolith_write — hard-deny guard.

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

CLASS = "hard-deny"; the policy point (``machine_profile.apply_guard_level``)
leaves the deny on an author box and downgrades it to a warning on a consumer box.

This is otherwise a faithful port: it preserves the reference hook's escape
hatch, the tool_name pre-filter, the backslash/slash-run normalization (F5
fix), the ``archive/completed/<YYYY-MM>.md`` tail match (never matching
``legacy/`` or per-entry-subdir forms, by construction of the regex), and the
reason text.

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
  - Does NOT name the alternative path in an allow: a match is always a deny
    whose reason surfaces the per-entry path.
  - Never raises: any unexpected input shape or internal error is treated as
    ALLOW/no-op (fail-open on error), matching the reference hook's
    ``set -uo pipefail`` fail-open discipline.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core._hook_envelope import deny

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 171

_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_COMPLETION_MONOLITH"

_MONOLITH_RE = re.compile(r"archive/completed/[0-9]{4}-[0-9]{2}\.md$")


def _extract_file_path(tool_name: str, tool_input: Dict[str, Any]) -> str:
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path") or ""
    return tool_input.get("file_path") or ""


def _normalize(file_path: str) -> str:
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
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

        return deny("PreToolUse", reason)
    except Exception:
        return None
