"""coordinator_core.write_guards.nudge_windows_subprocess_popup — advisory guard.

Python engine-ification of example-doctrine-repo's retired
``coordinator/hooks/scripts/nudge-windows-subprocess-popup.sh`` PreToolUse
(Write|Edit|MultiEdit) hook (deleted 2026-07-16, example-doctrine-repo ``2f8b8450``), per the
naked-Python hook migration (write_guards/INTERFACE.md).

Motivation (ported verbatim from the reference hook): Windows console-subsystem
children (python.exe, powershell.exe, netstat.exe, cmd.exe) spawned from the
headless Claude Code Bash-tool parent call ``AllocConsole()`` and steal focus.
At AUTHORING time the PORTABLE suppression form —
``creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)`` — is a stdlib
one-liner available everywhere, prompt-free, no project dependency. This guard
scans authored ``.sh``/``.py``/``.ps1``/``.psm1`` content for a bare
console-subprocess call lacking that suppression and offers the fix.

This started as a faithful engine-ification, not a redesign: it ported the
reference hook's extension gate, throwaway-path exemption, allowlist-marker
escape, and the two detection cascades (Python/.sh
``subprocess.run``/``Popen``/``os.system`` intent-aware check + the
``.sh``-only bare shell-token check; the ``.ps1``/``.psm1`` bare-invocation
checks) rather than re-deriving them. Per-tool CONTENT EXTRACTION is the one
exception, as of DR-077 below — see that section for why the reference hook's
fragment-scoped extraction was replaced rather than ported.

Async-seam note (migration-specific, not present in the reference hook): the
reference hook's tail section backgrounds an ``advisory_call`` to claude-klabauter
(``hooks.nudge_windows_subprocess_popup``) via ``spawn-hidden.sh`` ->
``coordinator_core.client``, gated behind ``COORDINATOR_CLAUDE_KLABAUTER_DENY_AUTHORITATIVE``.
That re-dispatch existed so example-doctrine-repo's bash shim could optionally defer its decision
to a claude-klabauter-side evaluation. Since this module now RUNS INSIDE claude-klabauter's own
engine, re-dispatching to claude-klabauter from here would be a self-call — it is
deliberately NOT ported. The reference hook does not delegate its DECISION
logic to any other pre-existing claude-klabauter engine function (no such function
existed prior to this module); this module IS that decision logic, computed
in-process and returned directly.

CLASS is "advisory" per DR-077 (2026-07-21) — a example-doctrine-repo-authored ruling that
DELIBERATELY DEPARTS from the retired reference hook (see
``docs/decisions/DR-077-console-popup-authoring-guard-whole-file-context-and-advisory.md``
in the example-doctrine-repo repo; routed here by cross-repo memo
``cross-repo/archive/2026-07-21-claude-central-em-popup-guard-fragment-scoped-false-denials-dr077.md``).
On positive detection it returns the advisory ``additionalContext`` envelope
(never ``permissionDecision: "deny"``); on any internal error it fails OPEN
(returns None). This is a NAMED, ruling-backed divergence from
INTERFACE.md fidelity rule 2 ("preserve every ... string BYTE-FOR-BYTE") and
from this module's own prior "faithful port, not a redesign" framing — the
reference hook's deny/hard-deny shape and its fragment-scoped detection are
both retired here, superseded by DR-077.

DETECTION SCOPE is whole-file, not fragment, per DR-077 part 1. Suppression
(``creationflags=``) is a whole-file/whole-call property that a
fragment-scoped scan cannot observe — the reference hook's approach measurably
denied correctly-suppressed code and docstring-only prose (DR-077's Cases A
and C). ``Write`` already receives the whole resulting file
(``tool_input["content"]``); ``Edit``/``MultiEdit`` now read the on-disk file
and apply the same old_string -> new_string substitution(s) the tool itself
would apply, to reconstruct the POST-EDIT whole file before running the
detection cascades. This reconstruction is fail-open and cost-bounded — see
``_read_file_safely`` / ``_MAX_WHOLE_FILE_BYTES`` — and falls back to the
original edit-fragment extraction on any read failure, decode error, missing
``old_string``, or oversized file, never on a raise.

Negative-spec:
  - Does NOT replicate the reference hook's claude-klabauter fast-path re-dispatch —
    see the async-seam note above.
  - Does NOT police git.exe (reference hook code-reviewer F3: git always
    spawns with DETACHED_PROCESS semantics on Windows).
  - Does NOT apply the bare shell-token check to ``.py`` files — real
    subprocess risk in ``.py`` already goes through the intent-aware
    ``subprocess.run(``/``Popen(``/``os.system(`` check; the bare-token
    check only false-positives on ``.py`` prose (e.g. a docstring
    documenting ``python3 -m pkg.module``) and is scoped to ``.sh``.
  - Does NOT fire on ``*/tasks/*`` or ``*/state/scratch/*`` paths — session
    throwaway/driver code never ships to a Windows operator.
  - Does NOT deny/block anything, per DR-077 part 2 — CLASS is advisory; the
    prior "hard-deny" behavior is retired, not merely softened in wording.
  - Does NOT let a whole-file reconstruction failure flip the decision to
    deny — every I/O/decode/size-cap failure falls back to fragment scoping,
    never to an error/deny path.
  - Never raises: any unexpected input shape returns ``None`` (ALLOW/no-op).

Spec backlink: docs/plans/2026-06-19-windows-console-popup-coordinator-doctrine.md § C2
Ruling: DR-077 (example-doctrine-repo docs/decisions/DR-077-console-popup-authoring-guard-whole-file-context-and-advisory.md)
Grep anchors: WINDOWS-CONSOLE-POPUP DR-077
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

CLASS = "advisory"  # DR-077 part 2 — was "hard-deny"; the class no longer blocks.
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 110  # deny-offer: runs after the structural block_* guards (≤90)

#: Whole-file read cap (DR-077 part 1 hook-latency bound). Files larger than
#: this fall back to fragment-scoped extraction rather than being read in
#: full on the PreToolUse hot path — see ``_read_file_safely``.
_MAX_WHOLE_FILE_BYTES = 256 * 1024

#: Allowlist escape markers — TWO canonical, env-agnostic suppression
#: markers honored identically.
_ALLOWLIST_MARKERS = (
    "# popup-intentional-last-resort",
    "# popup-safe-env-suppressed",
)

#: Extension gate.
_EXTENSIONS = (".sh", ".py", ".ps1", ".psm1")

#: Throwaway-path exemption.
_THROWAWAY_MARKERS = ("/tasks/", "/state/scratch/")

# --- .sh / .py detection --------------------
_PY_SUBPROCESS_CALL_RE = re.compile(r"subprocess\.(run|Popen)\(|os\.system\(")
_PY_CONSOLE_TARGET_RE = re.compile(
    r'"(powershell\.exe|netstat\.exe|python\.exe|cmd\.exe)"|sys\.executable'
)
_PY_SUPPRESSION_RE = re.compile(
    r"creationflags=|CREATE_NO_WINDOW|no_console_creationflags\(\)"
)

#: .sh-only bare shell-token check.
_SH_BARE_TOKEN_RE = re.compile(
    r"(^|[\s;&|`])(python3?(\.exe)?[\t ]+(-c|-m)|powershell\.exe)",
    re.MULTILINE,
)
_SH_BARE_TOKEN_SUPPRESSION_RE = re.compile(
    r"creationflags=|CREATE_NO_WINDOW|no_console_creationflags\(\)"
    r"|python-quiet\.sh|pythonw|-WindowStyle[\t ]+Hidden",
    re.MULTILINE | re.IGNORECASE,
)

# --- .ps1 / .psm1 detection -----------------
_PS1_BARE_INVOCATION_RE = re.compile(
    r"&[\t ]+python(\.exe)?|python\.exe[\t ]+-c"
    r"|Invoke-Expression[\t ]+\"python|Invoke-Expression[\t ]+'python",
    re.MULTILINE | re.IGNORECASE,
)
_PS1_BARE_POWERSHELL_RE = re.compile(
    r"(^|[\t ])(powershell\.exe|pwsh)([\t ]|$)",
    re.MULTILINE | re.IGNORECASE,
)
_PS1_SUPPRESSION_RE = re.compile(r"-WindowStyle[\t ]+Hidden", re.IGNORECASE)


def _strip_line_comments(content: str) -> str:
    """Strip ``#``-comments from content before detection regexes run.

    Fixes improvement-queue 2026-06-22-subprocess-popup-write-guard-denies-edit.yaml:
    a bare-call token mentioned only in a comment/prose (e.g. an explanatory
    ``# python -c '...' would add a spawn`` bash comment) must NOT trigger the
    deny. Quote-aware per line: a ``#`` inside a single- or double-quoted string
    is not a comment start, so a real call followed by a trailing comment
    (``python3 -c 'x'  # note``) is still detected.

    NOTE: this is a small private duplicate of the identical helper in
    coordinator_core/hooks/nudge_windows_subprocess_popup.py. The two packages
    are deliberately independent (see this module's docstring), so a shared
    cross-package import was rejected in favor of keeping both copies in
    lockstep by hand — see the shared parametrized test suite.

    LIMITS (deliberate, not fixed here):
      - No full shell/Python/PowerShell parser — only ``'`` and ``"`` are
        tracked as quote chars; backslash-escapes inside quotes are not
        honored.
      - Quote state resets at every newline, so a ``#`` inside a genuine
        multi-line quoted string may still be (incorrectly) treated as a
        comment start. This bias is SAFE for a deny guard: it can only cause
        a missed detection (false negative) on exotic multi-line-string
        content, never a NEW false positive — the correct bias for a guard
        that currently over-fires.
      - ``.ps1``/``.psm1`` ``<# ... #>`` block comments are NOT recognized;
        only line comments starting with an unquoted ``#``.
    """
    out_lines = []
    for line in content.split("\n"):
        in_single = False
        in_double = False
        chars = []
        for ch in line:
            if ch == "'" and not in_double:
                in_single = not in_single
                chars.append(ch)
            elif ch == '"' and not in_single:
                in_double = not in_double
                chars.append(ch)
            elif ch == "#" and not in_single and not in_double:
                break
            else:
                chars.append(ch)
        out_lines.append("".join(chars))
    return "\n".join(out_lines)

#: Advisory offer text. Originally ported BYTE-FOR-BYTE from the reference
#: hook's REASON heredoc, then reworded to offer-framing per DR-077; as of
#: the 2026-08-02 message-size-discipline plan (chunk C8) it is further
#: compressed to fit the 220-prose-byte cap (coordinator_core/bash_guards/
#: _message_size.py) — the RECOMMENDED FIX/WHY/spec-backlink prose was cut,
#: but the fix (creationflags one-liner, PowerShell -WindowStyle Hidden) and
#: both allowlist escape markers are preserved as exempt command spans.
_REASON = """OFFER: this console-subprocess call pops a window under headless Windows Bash. Use instead:
  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
  powershell.exe -WindowStyle Hidden

Escape: `# popup-intentional-last-resort` or `# popup-safe-env-suppressed`."""


def _extract_file_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or ""


def _extract_fragment(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Per-tool FRAGMENT content extraction.

    This is the pre-DR-077 extraction shape, retained ONLY as the fail-open
    fallback for ``_extract_content`` below — never called for its own sake
    on the happy path anymore, since a fragment cannot see whole-file
    suppression (DR-077 part 1).
    """
    if tool_name == "Write":
        return tool_input.get("content") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string") or ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return ""
        parts: List[str] = []
        for edit in edits:
            if isinstance(edit, dict):
                parts.append(str(edit.get("new_string") or ""))
        return "\n".join(parts)
    return ""


def _read_file_safely(file_path: str) -> Optional[str]:
    """Read ``file_path`` as UTF-8 text (errors='replace'), fail-open.

    Returns None — never raises — on any I/O error, or when the file exceeds
    ``_MAX_WHOLE_FILE_BYTES`` (DR-077's hook-latency bound). The caller's
    contract is: None means "fall back to fragment-scoped extraction", not
    "deny" — a read failure must never flip the guard's decision.
    """
    try:
        if os.path.getsize(file_path) > _MAX_WHOLE_FILE_BYTES:
            return None
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _apply_one_edit(
    content: str, old_string: Any, new_string: Any, replace_all: Any
) -> Optional[str]:
    """Apply a single Edit-shaped old_string -> new_string substitution.

    Mirrors Edit tool semantics: replaces the first occurrence unless
    ``replace_all`` is truthy. Returns None (fail-open) if ``old_string``
    is not a non-empty string or is not found in ``content``.
    """
    if not isinstance(old_string, str) or not old_string:
        return None
    if old_string not in content:
        return None
    new_string = new_string if isinstance(new_string, str) else ""
    if replace_all:
        return content.replace(old_string, new_string)
    return content.replace(old_string, new_string, 1)


def _extract_content(tool_name: str, tool_input: Dict[str, Any], file_path: str) -> str:
    """Whole-file content extraction (DR-077 part 1).

    ``Write`` already receives the whole resulting file via ``content``.
    ``Edit``/``MultiEdit`` reconstruct the POST-EDIT whole file by reading
    the current on-disk content at ``file_path`` and applying the same
    old_string -> new_string substitution(s) the tool itself will apply.

    Fail-open, always: any read failure, oversized file, or unresolvable
    ``old_string`` falls back to the fragment-scoped extraction
    (``_extract_fragment``) rather than raising or denying — this function
    must never be the reason a write is (or isn't) denied due to an I/O
    problem unrelated to the content itself.
    """
    if tool_name == "Write":
        return tool_input.get("content") or ""

    fragment = _extract_fragment(tool_name, tool_input)

    if tool_name == "Edit":
        current = _read_file_safely(file_path)
        if current is None:
            return fragment
        result = _apply_one_edit(
            current,
            tool_input.get("old_string"),
            tool_input.get("new_string"),
            tool_input.get("replace_all"),
        )
        return result if result is not None else fragment

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return fragment
        current = _read_file_safely(file_path)
        if current is None:
            return fragment
        for edit in edits:
            if not isinstance(edit, dict):
                return fragment
            result = _apply_one_edit(
                current,
                edit.get("old_string"),
                edit.get("new_string"),
                edit.get("replace_all"),
            )
            if result is None:
                return fragment
            current = result
        return current

    return ""


def _should_deny_sh_or_py(file_path: str, content: str) -> bool:
    if _PY_SUBPROCESS_CALL_RE.search(content) and _PY_CONSOLE_TARGET_RE.search(content):
        if not _PY_SUPPRESSION_RE.search(content):
            return True

    if file_path.endswith(".sh"):
        if _SH_BARE_TOKEN_RE.search(content):
            if not _SH_BARE_TOKEN_SUPPRESSION_RE.search(content):
                return True

    return False


def _should_deny_ps1(content: str) -> bool:
    if _PS1_BARE_INVOCATION_RE.search(content):
        return True
    if _PS1_BARE_POWERSHELL_RE.search(content):
        if not _PS1_SUPPRESSION_RE.search(content):
            return True
    return False


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        raw_file_path = _extract_file_path(payload)
        if not raw_file_path:
            return None

        # Normalize backslashes (Windows path support).
        file_path = raw_file_path.replace("\\", "/")

        if not file_path.endswith(_EXTENSIONS):
            return None

        if any(marker in file_path for marker in _THROWAWAY_MARKERS):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        content = _extract_content(tool_name, tool_input, file_path)
        if not content:
            return None

        # Allowlist escape markers — checked on ORIGINAL content, BEFORE
        # comment stripping. The markers are themselves comments; stripping
        # first would delete the escape hatch they provide.
        if any(marker in content for marker in _ALLOWLIST_MARKERS):
            return None

        # Strip `#`-comments before running detection regexes — see
        # _strip_line_comments docstring for the false-positive fix and limits.
        stripped_content = _strip_line_comments(content)

        if file_path.endswith((".ps1", ".psm1")):
            should_advise = _should_deny_ps1(stripped_content)
        else:
            should_advise = _should_deny_sh_or_py(file_path, stripped_content)

        if not should_advise:
            return None

        # Advisory envelope (DR-077 part 2) — additionalContext only, NEVER
        # permissionDecision:"deny". See INTERFACE.md § Envelope — advisory.
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": _REASON,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error (INTERFACE.md fidelity rule 6;
        # mirrors the reference hook's `set -uo pipefail` without `-e` — this
        # guard surfaces the offer ONLY on a positive content match, never on
        # an error).
        return None
