"""coordinator_core.write_guards.nudge_windows_subprocess_popup — advisory guard.

Python engine-ification of coordinator-claude's retired
``coordinator/hooks/scripts/nudge-windows-subprocess-popup.sh`` PreToolUse
(Write|Edit|MultiEdit) hook (deleted 2026-07-16, coordinator-claude ``2f8b8450``), per the
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
That re-dispatch existed so coordinator-claude's bash shim could optionally defer its decision
to a claude-klabauter-side evaluation. Since this module now RUNS INSIDE claude-klabauter's own
engine, re-dispatching to claude-klabauter from here would be a self-call — it is
deliberately NOT ported. The reference hook does not delegate its DECISION
logic to any other pre-existing claude-klabauter engine function (no such function
existed prior to this module); this module IS that decision logic, computed
in-process and returned directly.

CLASS is "advisory" per DR-077 (2026-07-21) — a coordinator-claude-authored ruling that
DELIBERATELY DEPARTS from the retired reference hook (see
``docs/decisions/DR-077-console-popup-authoring-guard-whole-file-context-and-advisory.md``
in the coordinator-claude repo). NOTE: DR-077's scope is the whole-file-context /
advisory-class change ONLY (part 1 and part 2 below) — it contains no
substantive discussion of git or DETACHED_PROCESS semantics and does not
back the (now-removed) git exemption; that exemption's provenance is
tracked separately, see the negative-spec below. The
``cross-repo/archive/2026-07-21-claude-central-em-popup-guard-fragment-scoped-false-denials-dr077.md``
citation this docstring used to carry alongside DR-077 is DANGLING — that
file does not exist in either this repo or coordinator-claude (verified
2026-08-07 during the windows-popup-guard-blind-to-git-and-asyncio
spinoff). Left noted here rather than silently dropped so a future reader
does not re-cite it or burn time hunting for it.
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
  - The pattern lists ``_EXTENSIONS``, ``_PY_CONSOLE_TARGET_RE``, and
    ``_PY_SUBPROCESS_CALL_RE`` (and, for suppression detection,
    ``_PY_SUPPRESSION_RE``) ARE THE GUARD'S ENTIRE FIELD OF VIEW. Widen
    freely; NEVER narrow. A narrowing does not make the guard stricter — it
    silently converts it into a no-op that keeps returning None (ALLOW) on
    files it can no longer see, while looking exactly like a clean file to
    every caller. This is the same failure shape as F1 (a hook probe
    matching only ``.sh`` after the repo went all-Python) and F13 (a
    registration assert masking three real failures) from the
    2026-08-07 install-dogfood audit — see
    state/handoffs/2026-08-07-windows-popup-guard-blind-to-git-and-asyncio.md.
    Each pattern constant above carries its own load-bearing note; this line
    is the summary, not a substitute for reading those.
  - Does NOT replicate the reference hook's claude-klabauter fast-path re-dispatch —
    see the async-seam note above.
  - The git exemption (reference hook code-reviewer F3: "git always spawns
    with DETACHED_PROCESS semantics on Windows and does not call
    AllocConsole()") was REMOVED on 2026-08-07, on measured evidence, per
    spinoff 2026-08-07-windows-popup-guard-blind-to-git-and-asyncio.md.
    MEASURED (not asserted): a console-subsystem ``git.exe`` spawned with no
    creationflags from a console-less parent allocates a **visible**
    ``ConsoleWindowClass`` window in ~50ms; pipe redirection
    (``capture_output=True``) does NOT suppress it. Both positive and
    negative controls passed. See
    ``state/audits/2026-08-07-git-console-allocation-measurement.md`` for
    the full harness and raw findings — the F3 premise this exemption rested
    on was itself never measured, and the audit's own live-site check found
    every miss to be a ``git`` spawn. ``git`` and ``git.exe`` are now both
    policed by ``_PY_CONSOLE_TARGET_RE`` like any other console-subsystem
    target; a genuinely suppressed git spawn still clears
    ``_PY_SUPPRESSION_RE`` as normal.
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
Ruling: DR-077 (coordinator-claude docs/decisions/DR-077-console-popup-authoring-guard-whole-file-context-and-advisory.md)
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

#: Extension gate. LOAD-BEARING (spinoff
#: 2026-08-07-windows-popup-guard-blind-to-git-and-asyncio.md Part 3): a file
#: whose extension is absent from this tuple is invisible to this guard
#: regardless of what it spawns — widen only, never narrow. See the note
#: above ``_PY_SUBPROCESS_CALL_RE`` for the recurrence this guards against.
_EXTENSIONS = (".sh", ".py", ".ps1", ".psm1")

#: Throwaway-path exemption.
_THROWAWAY_MARKERS = ("/tasks/", "/state/scratch/")

# --- .sh / .py detection --------------------
#: LOAD-BEARING (spinoff 2026-08-07-windows-popup-guard-blind-to-git-and-asyncio.md
#: Part 3): this pattern list is the guard's entire field of view for Python
#: spawn call-sites. It may be WIDENED, never narrowed — a narrowing silently
#: converts the guard into a no-op that still returns None (ALLOW) on every
#: file it scans, the exact F1/F13 recurrence this spinoff calls out (a check
#: reporting clean because of what it declines to look at). ``asyncio.``-
#: prefixed AND bare ``create_subprocess_exec``/``create_subprocess_shell(``
#: forms are both matched: every call-site found in this repo at authoring
#: time used the qualified ``asyncio.`` form (e.g.
#: ``ops/fleet/_common.py``, ``ops/distill_apply_disposal.py``), but the
#: bare form (``from asyncio import create_subprocess_exec``) is legal
#: Python and would otherwise be a silent second blind spot identical in
#: kind to the one this widening fixes — there is no live site to regress
#: against it, so this is a preventive widening, not an observed-miss fix.
#: Bare-form false-positive risk is bounded by ``_PY_CONSOLE_TARGET_RE``
#: below still needing to match a literal console-target string in the
#: same content before ``_should_deny_sh_or_py`` fires. The bare
#: alternative carries a ``(?<![\w.])`` left lookbehind so it cannot
#: substring-match inside a longer identifier (e.g.
#: ``my_create_subprocess_exec(``) — the qualified ``asyncio.`` form is
#: also covered by the same lookbehind, applied before the optional
#: ``asyncio.`` prefix.
_PY_SUBPROCESS_CALL_RE = re.compile(
    r"subprocess\.(run|Popen)\(|os\.system\("
    r"|(?<![\w.])(?:asyncio\.)?create_subprocess_(?:exec|shell)\("
)
#: LOAD-BEARING (see note above _PY_SUBPROCESS_CALL_RE) — the console-target
#: allowlist. Widen only; do not narrow. Deliberately NOT a catch-all (see
#: this module's Negative-spec) — a target absent from this list is a
#: silent miss by design until added, not a bug to route around elsewhere.
#: ``git``/``git.exe`` added 2026-08-07 (windows-popup-guard-blind-to-git-
#: and-asyncio spinoff) — the prior exemption's "DETACHED_PROCESS" premise
#: was measured and refuted, see the negative-spec below and
#: state/audits/2026-08-07-git-console-allocation-measurement.md. The bare
#: ``"git"`` list-element form (not just ``"git.exe"``) is matched because
#: both audited miss sites spawn ``["git", "rev-parse", ...]``, not
#: ``["git.exe", ...]``.
_PY_CONSOLE_TARGET_RE = re.compile(
    r'"(powershell\.exe|netstat\.exe|python\.exe|cmd\.exe|git\.exe|git)"'
    r"|sys\.executable"
)
#: LOAD-BEARING (see note above _PY_SUBPROCESS_CALL_RE). Matches BOTH the
#: canonical kwarg form (``creationflags=getattr(subprocess,
#: "CREATE_NO_WINDOW", 0)``) and the dict-literal form used by
#: ``ops/distill_apply_disposal.py::_subprocess_kwargs``
#: (``{"creationflags": 0x08000000}  # CREATE_NO_WINDOW``) — the trailing
#: ``# CREATE_NO_WINDOW`` comment that form relies on is stripped by
#: ``_strip_line_comments`` BEFORE this regex runs (comments are stripped
#: file-wide, not just for the call-site regexes), so without the
#: ``creationflags["']?\s*[:=]`` alternative below, that site's suppression
#: was invisible to this guard even though the site itself is correctly
#: suppressed at runtime. Confirmed by direct regex probe against the
#: post-strip content during this widening (2026-08-07) — this was a real,
#: second blind spot, not a hypothetical one.
_PY_SUPPRESSION_RE = re.compile(
    r"creationflags[\"']?\s*[:=]|CREATE_NO_WINDOW|no_console_creationflags\(\)"
)
#: Triple-quoted string spans (``"""..."""`` / ``'''...'''``), matched
#: non-greedy across newlines. Applied ONLY ahead of ``_PY_SUPPRESSION_RE``
#: (see ``_should_deny_sh_or_py``) — never ahead of ``_PY_SUBPROCESS_CALL_RE``
#: / ``_PY_CONSOLE_TARGET_RE``, whose deliberate prose-firing behavior on a
#: pure-docstring file is pinned by
#: ``TestCaseCDocstringOnlyProse::test_write_pure_prose_file_still_surfaces_but_only_as_advisory``
#: and must not change. Scope: a docstring/string literal that merely
#: *mentions* ``creationflags``/``CREATE_NO_WINDOW`` in prose (not inside a
#: real kwarg/dict-literal suppression) must not clear the suppression check
#: for a genuinely unsuppressed spawn elsewhere in the same file — see the
#: negative-spec on ``_strip_line_comments`` for the false-negative this
#: closes.
_TRIPLE_QUOTED_STRING_RE = re.compile(r'"""(?:.*?)"""|\'\'\'(?:.*?)\'\'\'', re.DOTALL)

#: .sh-only bare shell-token check.
_SH_BARE_TOKEN_RE = re.compile(
    r"(^|[\s;&|`])(python3?(\.exe)?[\t ]+(-c|-m)|powershell\.exe)",
    re.MULTILINE,
)
#: `-WindowStyle Hidden` REMOVED 2026-08-07 (no-window-subprocess-primitive
#: plan, chunk C5, AC7) — create-then-hide, not reliable suppression (see
#: `_REASON`'s note above and state/audits/2026-08-07-git-console-
#: allocation-measurement.md). The other four spellings are untouched and
#: remain sufficient to satisfy this `.sh` bare-token check.
_SH_BARE_TOKEN_SUPPRESSION_RE = re.compile(
    r"creationflags[\"']?\s*[:=]|CREATE_NO_WINDOW|no_console_creationflags\(\)"
    r"|python-quiet\.sh|pythonw",
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
#: 2026-08-07 (no-window-subprocess-primitive plan, chunk C5, AC7): the
#: ENTIRE body of this regex is `-WindowStyle\s+Hidden` — nothing else. Left
#: UNCHANGED deliberately. `-WindowStyle Hidden` is create-then-hide, not
#: reliable suppression (see `_REASON`'s note above), but narrowing/emptying
#: this regex would leave `_should_deny_ps1` permanently unsatisfiable: every
#: `.ps1` naming `powershell.exe`/`pwsh` would deny with no compliant
#: spelling left to offer (there is no Python-side primitive a `.ps1` file
#: can spell). The correct disposition is advisory-with-honest-text: this
#: regex stays the accepted (still-imperfect) marker, and the honesty lives
#: in `_REASON`'s `.ps1` line instead.
_PS1_SUPPRESSION_RE = re.compile(r"-WindowStyle[\t ]+Hidden", re.IGNORECASE)


def _strip_line_comments(content: str) -> str:
    """Strip ``#``-comments from content before detection regexes run.

    Fixes improvement-queue 2026-06-22-subprocess-popup-write-guard-denies-edit.yaml:
    a bare-call token mentioned only in a comment/prose (e.g. an explanatory
    ``# python -c '...' would add a spawn`` bash comment) must NOT trigger the
    deny. Quote-aware per line: a ``#`` inside a single- or double-quoted string
    is not a comment start, so a real call followed by a trailing comment
    (``python3 -c 'x'  # note``) is still detected.

    NOTE: an earlier revision of this docstring claimed a second copy of
    this module at coordinator_core/hooks/nudge_windows_subprocess_popup.py,
    kept in hand-lockstep via a shared parametrized test suite. That file
    does not exist in this repo (verified 2026-08-07 during the
    windows-popup-guard-blind-to-git-and-asyncio spinoff) — there is only
    this one copy, under coordinator_core/write_guards/. The 12-case suite
    at coordinator_core/tests/test_nudge_windows_subprocess_popup_comments.py
    tests this copy only (see its own module docstring, which already notes
    the single-copy placement). If a second copy is reintroduced, restore a
    real lockstep note here instead of the stale one this replaces.

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
      - Does NOT strip triple-quoted (``\"\"\"``/``'''``) string spans —
        that gap is closed separately (and only for the suppression check)
        by ``_strip_triple_quoted_strings``, called from
        ``_should_deny_sh_or_py`` ahead of ``_PY_SUPPRESSION_RE`` only. This
        function's own comment-stripping pass is unaffected: a triple-quoted
        span containing a ``#`` is still (correctly) not treated as a
        comment start by the quote tracking below, since ``"``/``'`` are
        tracked per-character, not per-triple.
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


def _strip_triple_quoted_strings(content: str) -> str:
    """Blank out ``\"\"\"...\"\"\"``/``'''...'''`` spans, newline-preserving.

    Closes the false-negative in ``_PY_SUPPRESSION_RE`` (spinoff
    2026-08-07-windows-popup-guard-blind-to-git-and-asyncio.md): a docstring
    or explanatory string literal that merely *mentions*
    ``creationflags``/``CREATE_NO_WINDOW`` in prose must not clear the
    suppression check for a genuinely unsuppressed spawn elsewhere in the
    same file. Called ONLY ahead of ``_PY_SUPPRESSION_RE`` in
    ``_should_deny_sh_or_py`` — deliberately NOT applied ahead of
    ``_PY_SUBPROCESS_CALL_RE``/``_PY_CONSOLE_TARGET_RE``, whose prose-firing
    behavior on a pure-docstring file is intentional (DR-077 Case C) and
    pinned by an existing test.

    Non-greedy, ``re.DOTALL`` match — an unbalanced/unterminated triple
    quote is a rare authoring error this does not attempt to recover from
    (the guard fails open on any unexpected shape regardless). Replacement
    preserves the span's newline count so any downstream ``re.MULTILINE``
    line-anchored regex still sees consistent line numbers.
    """
    return _TRIPLE_QUOTED_STRING_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"), content
    )

#: Advisory offer text. Originally ported BYTE-FOR-BYTE from the reference
#: hook's REASON heredoc, then reworded to offer-framing per DR-077; as of
#: the 2026-08-02 message-size-discipline plan (chunk C8) it is further
#: compressed to fit the 220-prose-byte cap (coordinator_core/bash_guards/
#: _message_size.py) — the RECOMMENDED FIX/WHY/spec-backlink prose was cut,
#: but the fix (creationflags one-liner) and both allowlist escape markers
#: are preserved as exempt command spans.
#:
#: 2026-08-07 (no-window-subprocess-primitive plan, chunk C5, AC6): the
#: `powershell.exe -WindowStyle Hidden` recommended-fix line is REMOVED, not
#: reworded in place — `verify_no_console_flash.py`'s own docstring and
#: `_PS1_SUPPRESSION_RE`'s note both establish it is create-then-hide, not
#: reliable suppression (state/audits/2026-08-07-git-console-allocation-
#: measurement.md). Recommending it as a fix while `_should_deny_ps1` also
#: still accepts it as the only compliant `.ps1` spelling (AC7 — narrowing
#: that acceptance would make the guard unsatisfiable, see
#: `_PS1_SUPPRESSION_RE`'s own docstring) would have the guard deny an
#: author, offer this exact spelling as the fix, then deny that fix. In its
#: place: `no_console_creationflags()` (the primitive `coordinator_core.
#: win_portability` now ships, landed by peer chunk C2) as the Python-side
#: fix, plus one honest `.ps1` line — still inside the same indented
#: cue-window block so it stays an exempt command span, not authored prose
#: — naming the create-then-hide caveat and pointing back at the primitive
#: as the preferred route.
_REASON = """OFFER: this console-subprocess call pops a window under headless Windows Bash. Use instead:
  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
  no_console_creationflags()
  .ps1: -WindowStyle Hidden is create-then-hide, may still flash; no reliable
  PowerShell suppression exists -- prefer spawning via the primitive above.

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
        # Suppression check only: triple-quoted spans (docstrings, string
        # literals) are blanked out first so a prose *mention* of
        # creationflags/CREATE_NO_WINDOW cannot clear a genuinely
        # unsuppressed spawn. Deliberately not applied above, to the
        # call-site/console-target search -- see _strip_triple_quoted_strings.
        suppression_scope = _strip_triple_quoted_strings(content)
        if not _PY_SUPPRESSION_RE.search(suppression_scope):
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
