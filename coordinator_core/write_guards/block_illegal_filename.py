"""coordinator_core.write_guards.block_illegal_filename — hard-deny guard.

Python engine-ification of the RELIABLE ARM (Write|Edit|NotebookEdit leg
only) of example-doctrine-repo's retired ``coordinator/hooks/scripts/block-illegal-filename.sh``
PreToolUse hook (deleted 2026-07-20, example-doctrine-repo ``e91827a7``), per the naked-Python
hook migration (write_guards/INTERFACE.md).

Purpose (ported verbatim from the reference hook): a file committed from a
non-Windows machine with an NTFS-illegal character in its name (e.g. ``:`` —
the Windows ADS separator) blocks ``git checkout`` on every Windows machine.
This guard intercepts the Write/Edit/NotebookEdit tool boundary and denies
path targets whose basename contains any NTFS-illegal character, a control
character, or a trailing dot/space BEFORE the write lands on disk.

Class-3 universal-safety — FAIL-CLOSED. NO directory exemption: any
git-tracked path is in-scope; an NTFS-illegal char breaks the git tree
regardless of directory.

LEG SPLIT (per migration task instruction): the reference hook was
MULTI-MATCHER (``Write|Edit|NotebookEdit|Bash``) with TWO independent arms —
a RELIABLE filename-legality arm on the structured ``tool_input.file_path``
(this module — the reference hook's ``csn_check`` call) and a BEST-EFFORT
Bash-command-string arm (mv / redirection / ``--out`` target scanning,
advisory-only). This module ports ONLY the reliable
arm; its check function corresponds to the reference hook's inline
``csn_check "$BNAME"`` call plus the ``make_deny_msg`` reason builder. The
Bash leg (``TOOL_NAME == "Bash"``) is deliberately NOT ported here — it
remains a bash-hook-only concern (a distinct migration unit), so this
module's ``MATCHERS`` omits ``"Bash"`` entirely.

This is a faithful engine-ification, not a redesign: it consumes the shared
``coordinator_core.bash_guards._helpers.csn_check`` predicate (trailing-dot,
trailing-space, then each NTFS-illegal char in its exact case-statement
order, then ASCII control chars — see that module's docstring for why the
Bash leg and this Write/Edit leg share ONE port rather than two), the
``${FILE_PATH##*[/\\]}``-equivalent leaf-only basename extraction (both
``/`` and ``\\`` are path separators — Windows delivers backslash-separated
paths), and ``make_deny_msg``'s safe-suggestion derivation + deny-message
text rather than re-deriving them.

Negative-spec:
  - Does NOT extract ``tool_input.notebook_path`` for ``NotebookEdit`` — the
    reference hook's reliable arm reads only ``tool_input.file_path`` for
    all three matched tool names (including ``NotebookEdit``, whose real
    field is ``notebook_path``); this module preserves that exact (in
    practice no-op for genuine NotebookEdit calls) behavior rather than
    "fixing" it, per the fidelity contract.
  - Does NOT port the best-effort Bash-command-string arm (``mv``/redirect/
    ``--out`` scanning, heredoc/quote stripping, the non-blocking
    ``additionalContext`` advisory shape) — that leg stays in the ``.sh``
    per the migration task's explicit instruction.
  - Does NOT fail open on a genuinely illegal basename — Class-3
    universal-safety guards are FAIL-CLOSED with no directory exemption.
    Any *unexpected processing error* (malformed payload, missing field)
    still returns ``None`` (ALLOW) rather than fabricating a deny — that is
    the reference hook's ``set -uo pipefail``-without-``-e`` discipline, not
    a directory/path-shape exemption.

Spec backlink: docs/plans/2026-06-30-cross-platform-file-naming-helper.md § D1
Tripwire entry: docs/wiki/coordinator-tripwires.md § BLOCK-ILLEGAL-FILENAME
Ported from the retired example-doctrine-repo bash guard ``block-illegal-filename.sh``
  (deleted 2026-07-20, example-doctrine-repo ``e91827a7``); csn_check ported from example-doctrine-repo
  coordinator/bin/lib/coordinator-safe-name.sh (example-doctrine-repo ``721a71f4``, 2026-07-21)
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import (
    csn_check as _csn_check,
    operator_override_note,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "NotebookEdit"]
PRIORITY = 20

#: Escape hatch.
_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_ILLEGAL_FILENAME"

# NOTE: the basename-legality predicate (formerly a private inline
# ``_csn_check`` port here) now lives in
# ``coordinator_core.bash_guards._helpers.csn_check`` -- the shared port both
# this Write/Edit leg and the (W3b) Bash leg of block-illegal-filename.sh
# consume, closing the "one shared helper, not two" gap flagged by the W3a
# recipe (bash-to-python-migration/W3a-preuse-bash-recipe.md, Summary item 2).
# Imported above under its original local name (``_csn_check``) so every call
# site below is unchanged.


def _basename(path: str) -> str:
    """Port of ``${FILE_PATH##*[/\\]}`` — the leaf component, splitting on
    EITHER ``/`` or ``\\`` (Windows delivers backslash-separated paths; a
    bare POSIX ``basename`` would mis-parse a drive-letter colon as the leaf
    on those paths).
    """
    idx = max(path.rfind("/"), path.rfind("\\"))
    return path[idx + 1 :] if idx >= 0 else path


def _safe_suggestion(raw_name: str) -> str:
    """Port of ``make_deny_msg``'s safe-suggestion pipeline:
    ``tr ':?*<>|"\\/' '-' | tr -s '-' | sed 's/^-//; s/-$//' | sed 's/[. ]*$//'``
    """
    illegal = ':?*<>|"\\/'
    translated = "".join("-" if c in illegal else c for c in raw_name)
    squeezed = re.sub(r"-+", "-", translated)
    squeezed = re.sub(r"^-", "", squeezed)
    squeezed = re.sub(r"-$", "", squeezed)
    squeezed = re.sub(r"[. ]*$", "", squeezed)
    return squeezed


def _make_deny_msg(
    raw_name: str, illegal_char_hint: str, payload: Optional[Dict[str, Any]] = None
) -> str:
    """Port of ``make_deny_msg``, byte-for-byte reason text (design-as-offers:
    leads with the safe alternative).
    """
    safe_suggestion = _safe_suggestion(raw_name)
    _note = operator_override_note(_OVERRIDE_ENV, payload=payload)
    return (
        f"'{raw_name}': '{illegal_char_hint}' breaks Windows checkout. Use instead: "
        f"`{safe_suggestion}` or `coordinator-safe-name timestamp`."
        + ("\n\n" + _note if _note else "")
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "NotebookEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        # Reliable arm reads ONLY tool_input.file_path for all three matched
        # tool names — including NotebookEdit, whose real field is
        # notebook_path (see module negative-spec).
        file_path = tool_input.get("file_path") or ""
        if not file_path:
            return None

        bname = _basename(file_path)
        if not bname:
            return None

        illegal_char_hint = _csn_check(bname)
        if illegal_char_hint is None:
            return None

        reason = _make_deny_msg(bname, illegal_char_hint, payload)

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    except Exception:
        # Unexpected processing error (malformed payload/missing field) fails
        # open — NOT a directory/path-shape exemption (this guard is
        # Class-3 fail-closed for genuinely-illegal basenames; INTERFACE.md
        # fidelity rule 6 covers guard-crash isolation only).
        return None
