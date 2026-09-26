"""coordinator_core.write_guards.nudge_windows_subprocess_popup — advisory guard.

Python engine-ification of DoE's retired
``coordinator/hooks/scripts/nudge-windows-subprocess-popup.sh`` PreToolUse
(Write|Edit|MultiEdit) hook (deleted 2026-07-16, DoE ``2f8b8450``), per the
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
That re-dispatch existed so DoE's bash shim could optionally defer its decision
to a claude-klabauter-side evaluation. Since this module now RUNS INSIDE claude-klabauter's own
engine, re-dispatching to claude-klabauter from here would be a self-call — it is
deliberately NOT ported. The reference hook does not delegate its DECISION
logic to any other pre-existing claude-klabauter engine function (no such function
existed prior to this module); this module IS that decision logic, computed
in-process and returned directly.

CLASS is "advisory" per DR-077 (2026-07-21) — a DoE-authored ruling that
DELIBERATELY DEPARTS from the retired reference hook (see
``docs/decisions/DR-077-console-popup-authoring-guard-whole-file-context-and-advisory.md``
in the DoE-claude repo). NOTE: DR-077's scope is the whole-file-context /
advisory-class change ONLY (part 1 and part 2 below) — it contains no
substantive discussion of git or DETACHED_PROCESS semantics and does not
back the (now-removed) git exemption; that exemption's provenance is
tracked separately, see the negative-spec below. The
``cross-repo/archive/2026-07-21-claude-central-em-popup-guard-fragment-scoped-false-denials-dr077.md``
citation this docstring used to carry alongside DR-077 is DANGLING — that
file does not exist in either this repo or DoE-claude (verified
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
  - DETECTION IS TWO DETECTORS, ORed — the file-wide regex cascade AND the
    per-call-site AST walk (``_analyze_py_call_sites``, added 2026-08-21).
    The AST path exists because whole-file scope answers the wrong question
    in both directions: one ``creationflags=`` anywhere silenced the guard
    for every OTHER spawn in the file (measured: 65 files blind, including
    ``coordinator/bin/publish.py`` at 7 suppressed vs 9 bare), and one
    ``DETACHED_PROCESS`` line symmetrically condemned every spawn in its
    file. The AST path may only ever ADD fires; the single scoped exception
    is the no-op-suppression leg, which defers to the per-call-site verdict
    only when that walk reports ``conclusive``. See
    ``state/audits/2026-08-21-detached-process-console-window-storm.md``.
  - The regex cascade is NOT retired by the AST path and must not be
    deleted to simplify it. An AST-only detector cannot fire on a
    docstring-only file, and
    ``TestCaseCDocstringOnlyProse::test_write_pure_prose_file_still_surfaces_but_only_as_advisory``
    pins (DR-077) that pure prose MUST still surface. The regex path is that
    prose fallback; the AST path is a second, more precise detector layered
    in front of it, never a replacement.
  - ``_AST_CONSOLE_TARGET_NAMES`` and ``_PY_CONSOLE_TARGET_RE`` are one
    target list in two spellings — widen them in lockstep, never one alone,
    or the precise detector silently stops seeing a target the coarse one
    still names. Pinned by
    ``TestAnalyzerContract::test_ast_targets_stay_in_lockstep_with_the_regex_alternation``.
  - KNOWN RESIDUE, per DR-345 Decision 1 (b) (2026-08-21) — after widening
    ``_ast_spawn_kind`` to the shared ``SPAWN_NAMES_BY_MODULE`` universe, two
    conveyance shapes still let a no-op flag reach an unrecognized call while
    the AST view reports ``conclusive``, because the closing sweep in
    ``_analyze_py_call_sites`` matches by literal AST IDENTIFIER
    (``ast.Attribute.attr`` / ``ast.Name.id``), not by resolved value:
      1. **Import alias.** ``from subprocess import DETACHED_PROCESS as DP``
         then ``my_popen(argv, DP)`` — the use-site identifier is ``DP``, not
         a member of ``_AST_NO_OP_FLAG_NAMES``, so the sweep does not see it.
      2. **``getattr``-by-string.** ``x = getattr(subprocess,
         "DETACHED_PROCESS", 0)`` then ``my_popen(argv, x)`` — the flag name
         is an ``ast.Constant`` string the sweep's ``Attribute``/``Name``
         ``isinstance`` branches never inspect, and the use-site identifier
         ``x`` carries no flag-shaped name either.
    Both require the flag to first reach an UNRECOGNIZED callable (a local
    wrapper, not a member of ``SPAWN_NAMES_BY_MODULE``) — resolving either
    would require binding-value tracking (resolving what a `Name` is bound
    to, transitively) that this module's static, per-node AST walk does not
    attempt anywhere else, and widening the spawn-name universe (b) cannot
    close a hole whose defect is upstream of spawn-call recognition entirely.
    Not fixed here — considered and left open on that basis; see DR-345.
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
Ruling: DR-077 (DoE-claude docs/decisions/DR-077-console-popup-authoring-guard-whole-file-context-and-advisory.md)
Grep anchors: WINDOWS-CONSOLE-POPUP DR-077
"""

from __future__ import annotations

import ast
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.spawn_policy.spawn_names import SPAWN_NAMES_BY_MODULE

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 110

_MAX_WHOLE_FILE_BYTES = 256 * 1024

_ALLOWLIST_MARKERS = (
    "# popup-intentional-last-resort",
    "# popup-safe-env-suppressed",
)

#: Extension gate. LOAD-BEARING (spinoff
#: above ``_PY_SUBPROCESS_CALL_RE`` for the recurrence this guards against.
_EXTENSIONS = (".sh", ".py", ".ps1", ".psm1")

_THROWAWAY_MARKERS = ("/tasks/", "/state/scratch/")

#: LOAD-BEARING (spinoff 2026-08-07-windows-popup-guard-blind-to-git-and-asyncio.md
#: Bare-form false-positive risk is bounded by ``_PY_CONSOLE_TARGET_RE``
_PY_SUBPROCESS_CALL_RE = re.compile(
    r"subprocess\.(run|Popen)\(|os\.system\("
    r"|(?<![\w.])(?:asyncio\.)?create_subprocess_(?:exec|shell)\("
)
#: LOAD-BEARING (see note above _PY_SUBPROCESS_CALL_RE) — the console-target
#: and-asyncio spinoff) — the prior exemption's "DETACHED_PROCESS" premise
_PY_CONSOLE_TARGET_RE = re.compile(
    r"[\"'](powershell\.exe|netstat\.exe|python\.exe|cmd\.exe|git\.exe|git)[\"']"
    r"|sys\.executable"
)
#: LOAD-BEARING (see note above _PY_SUBPROCESS_CALL_RE). Matches BOTH the
#: "CREATE_NO_WINDOW", 0)``) and the dict-literal form used by
#: (``{"creationflags": 0x08000000}  # CREATE_NO_WINDOW``) — the trailing
#: ``# CREATE_NO_WINDOW`` comment that form relies on is stripped by
_PY_SUPPRESSION_RE = re.compile(
    r"creationflags[\"']?\s*[:=]|CREATE_NO_WINDOW|no_console_creationflags\(\)"
)
#: LOAD-BEARING DISQUALIFIER — widen, never narrow.
#: ``_PY_SUPPRESSION_RE`` above accepts the mere PRESENCE of a ``creationflags``
#: measured way: ``creationflags`` carrying ``DETACHED_PROCESS`` (or
#: ``CREATE_NEW_CONSOLE``) does not suppress anything — it AMPLIFIES. Win32
#: documents ``CREATE_NO_WINDOW`` as IGNORED whenever either of those is set,
#: ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`` behaves as
#: bare ``DETACHED_PROCESS``: the child gets NO console, and every
#: This is the same defect class as ``_PS1_SUPPRESSION_RE``'s ``-WindowStyle
#: windows across 3 spawns for both the bare and the ``| CREATE_NO_WINDOW``
#: forms, versus 0 once ``DETACHED_PROCESS`` was dropped.
#: Applied to the same triple-quote-stripped scope as ``_PY_SUPPRESSION_RE``, so
_PY_NO_OP_SUPPRESSION_RE = re.compile(r"DETACHED_PROCESS|CREATE_NEW_CONSOLE")
#: non-greedy across newlines. Applied ONLY ahead of ``_PY_SUPPRESSION_RE``
#: (see ``_should_deny_sh_or_py``) — never ahead of ``_PY_SUBPROCESS_CALL_RE``
#: / ``_PY_CONSOLE_TARGET_RE``, whose deliberate prose-firing behavior on a
#: *mentions* ``creationflags``/``CREATE_NO_WINDOW`` in prose (not inside a
_TRIPLE_QUOTED_STRING_RE = re.compile(r'"""(?:.*?)"""|\'\'\'(?:.*?)\'\'\'', re.DOTALL)

_SH_BARE_TOKEN_RE = re.compile(
    r"(^|[\s;&|`])(python3?(\.exe)?[\t ]+(-c|-m)|powershell\.exe)",
    re.MULTILINE,
)
_SH_BARE_TOKEN_SUPPRESSION_RE = re.compile(
    r"creationflags[\"']?\s*[:=]|CREATE_NO_WINDOW|no_console_creationflags\(\)"
    r"|python-quiet\.sh|pythonw",
    re.MULTILINE | re.IGNORECASE,
)

_PS1_BARE_INVOCATION_RE = re.compile(
    r"&[\t ]+python(\.exe)?|python\.exe[\t ]+-c"
    r"|Invoke-Expression[\t ]+\"python|Invoke-Expression[\t ]+'python",
    re.MULTILINE | re.IGNORECASE,
)
_PS1_BARE_POWERSHELL_RE = re.compile(
    r"(^|[\t ])(powershell\.exe|pwsh)([\t ]|$)",
    re.MULTILINE | re.IGNORECASE,
)
#: UNCHANGED deliberately. `-WindowStyle Hidden` is create-then-hide, not
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
#: _message_size.py) — the RECOMMENDED FIX/WHY/spec-backlink prose was cut,
#: `_PS1_SUPPRESSION_RE`'s note both establish it is create-then-hide, not
#: `_PS1_SUPPRESSION_RE`'s own docstring) would have the guard deny an
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
    if not isinstance(old_string, str) or not old_string:
        return None
    if old_string not in content:
        return None
    new_string = new_string if isinstance(new_string, str) else ""
    if replace_all:
        return content.replace(old_string, new_string)
    return content.replace(old_string, new_string, 1)


def _extract_content_ex(
    tool_name: str, tool_input: Dict[str, Any], file_path: str
) -> tuple[str, bool]:
    if tool_name == "Write":
        return tool_input.get("content") or "", False

    fragment = _extract_fragment(tool_name, tool_input)

    if tool_name == "Edit":
        current = _read_file_safely(file_path)
        if current is None:
            return fragment, True
        result = _apply_one_edit(
            current,
            tool_input.get("old_string"),
            tool_input.get("new_string"),
            tool_input.get("replace_all"),
        )
        return (result, False) if result is not None else (fragment, True)

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return fragment, True
        current = _read_file_safely(file_path)
        if current is None:
            return fragment, True
        for edit in edits:
            if not isinstance(edit, dict):
                return fragment, True
            result = _apply_one_edit(
                current,
                edit.get("old_string"),
                edit.get("new_string"),
                edit.get("replace_all"),
            )
            if result is None:
                return fragment, True
            current = result
        return current, False

    return "", False


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

    Thin wrapper over ``_extract_content_ex`` — kept as a separate function
    (rather than changing every caller to unpack a pair) since this content
    string alone is all every OTHER caller in this repo needs.
    """
    content, _used_fallback = _extract_content_ex(tool_name, tool_input, file_path)
    return content


#: ``_PY_CONSOLE_TARGET_RE``'s alternation, kept as a set because the AST path
_AST_CONSOLE_TARGET_NAMES = frozenset(
    {"powershell.exe", "netstat.exe", "python.exe", "cmd.exe", "git.exe", "git"}
)

#: The no-op creationflags spellings, as AST IDENTIFIERS rather than as text —
#: the identifier twin of ``_PY_NO_OP_SUPPRESSION_RE``'s alternation. Keep the
_AST_NO_OP_FLAG_NAMES = frozenset({"DETACHED_PROCESS", "CREATE_NEW_CONSOLE"})

_AST_FIRE = "fire"
_AST_CLEAN = "clean"
_AST_UNKNOWN = "unknown"


def _ast_argv0_names(node: ast.AST) -> Tuple[Optional[List[str]], bool]:
    """Resolve a spawn call's argv0 to candidate names.

    Returns ``(names, resolved)``. ``names`` is the set of spellings to test
    against ``_AST_CONSOLE_TARGET_NAMES`` (the literal token plus its
    basename, so a fully-pathed ``.../Git/cmd/git.exe`` resolves like
    ``git.exe``).
    ``resolved`` is False whenever argv0 is not a static string — a variable,
    an f-string, a call — in which case the caller must NOT conclude the site
    is clean; it degrades to ``_AST_UNKNOWN`` and the file-wide regex path
    keeps its say. Static-only resolution is the whole reason this detector
    can be precise; pretending to resolve a dynamic argv0 would trade the
    guard's coarse-but-honest verdict for a confident wrong one.
    """
    if isinstance(node, ast.Attribute) and node.attr == "executable":
        if isinstance(node.value, ast.Name) and node.value.id == "sys":
            return ["sys.executable"], True
        return None, False

    first: Optional[ast.AST] = node
    if isinstance(node, (ast.List, ast.Tuple)):
        if not node.elts:
            return None, False
        first = node.elts[0]
        if isinstance(first, ast.Attribute):
            return _ast_argv0_names(first)

    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None, False

    stripped = first.value.strip()
    if not stripped:
        return None, False
    token = stripped.split()[0].strip("\"'")
    if not token:
        return None, False
    normalized = token.replace("\\", "/")
    return [normalized, normalized.rsplit("/", 1)[-1]], True


def _ast_spawn_kind(func: ast.AST) -> Optional[str]:
    """Name the spawn form a call node uses, or None if it is not one.

    Recognition is WIDER than ``_PY_SUBPROCESS_CALL_RE`` by design, per
    DR-345 Decision 1 (b): this function positively recognizes the full
    qualified-attribute (``module.func(``) spawn-name universe the standing
    bare-hot-path-spawn gate polices (``coordinator_core.spawn_policy.
    spawn_names.SPAWN_NAMES_BY_MODULE`` — the SAME table, imported, not
    re-derived), not only the two ``subprocess`` forms and the one ``os``
    form the regex matches. Before this widening, ``subprocess.call(...)``,
    ``subprocess.check_call(...)``, ``subprocess.check_output(...)``, and
    ``os.popen(...)`` were invisible to BOTH detectors at once — outside the
    regex alternation AND outside this function's old recognized set — so an
    unsuppressed console-target call through any of them evaded the guard
    entirely, not merely evaded the precise leg while the coarse leg kept
    its say. A form the regex sees and this does not remains survivable (the
    coarse leg still fires); a form this sees and the regex does not is a
    pure widening — see the module's own negative-spec.

    Deliberately NOT resolving import/module ALIASES (``import subprocess as
    sp`` then ``sp.Popen(...)``, or ``from subprocess import Popen`` then a
    bare ``Popen(...)``): that is a materially bigger feature (the standing
    gate's own ``_SubprocessImportResolver``) than "recognize the full
    spawn-NAME universe," and a bare-name widening for common generic names
    like ``run``/``call`` risks misclassifying an unrelated same-named
    function repo-wide — a cost the qualified-attribute widening does not
    carry. Named as residue in the module's negative-spec, not silently
    dropped.

    Recognizing FEWER than the regex here AND trusting an AST "clean" verdict
    is the combination that would re-open the blind spot this detector exists
    to close, which is why ``_analyze_py_call_sites`` reports ``conclusive``
    only when every recognized call resolved.
    """
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            module = func.value.id
            names = SPAWN_NAMES_BY_MODULE.get(module)
            if names is not None and func.attr in names:
                return module
        return None
    if isinstance(func, ast.Name) and func.id in SPAWN_NAMES_BY_MODULE["asyncio"]:
        return "asyncio"
    return None


def _ast_creationflags_opaque(expr: ast.AST) -> bool:
    """True when a ``creationflags=`` value cannot be read from its own source.

    ``creationflags=flags`` says nothing about whether ``flags`` carries
    ``DETACHED_PROCESS``. Treating such a value as suppression-and-not-a-no-op
    would let the per-call-site path call a file conclusive and thereby retire
    the file-wide ``DETACHED_PROCESS`` leg on a file where that name is merely
    bound to a variable first — exactly the amplifying spelling the leg exists
    to catch. So any free ``Name`` other than a known module reference makes
    the expression opaque, and one opaque value makes the whole file
    non-conclusive.
    """
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Name) and sub.id not in ("subprocess", "os"):
            return True
    return False


def _ast_parse_whole_or_fragment(content: str) -> Optional[ast.AST]:
    """Parse whole-file content, falling back to an indented-fragment retry.

    The fragment retry is not defensive tidiness — it is the only way the
    per-call-site detector reaches a file over ``_MAX_WHOLE_FILE_BYTES``.
    ``_read_file_safely`` refuses to read such a file, so an ``Edit`` to it
    arrives here as the raw edit fragment (see ``_extract_content_ex``), which
    is typically an indented block body and raises ``IndentationError`` from a
    bare ``ast.parse``. ``coordinator/bin/publish.py`` — the measured worst
    case at ~558KB, 7 suppressed spawns standing in front of 9 bare ones — is
    exactly this shape, so without the retry the file named in the audit as
    the defect's headline example would stay blind to the fix for it.

    Wrapping in ``if True:`` and re-indenting preserves uniform relative
    indentation, so any block body that was legal in its original position
    parses here. A fragment that still will not parse returns None, and the
    caller degrades to ``_AST_UNKNOWN`` — the file-wide regex path keeps its
    say, never silence.
    """
    try:
        return ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        pass
    try:
        indented = "".join("    " + line for line in content.splitlines(keepends=True))
        return ast.parse("if True:\n" + indented + "\n")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _analyze_py_call_sites(content: str) -> Tuple[str, bool]:
    """Decide per SPAWN CALL SITE whether a console spawn is unsuppressed.

    Returns ``(verdict, conclusive)``.

    This is a SECOND detector layered in front of the file-wide regex
    cascade, never a replacement for it — see this module's negative-spec and
    ``_should_deny_sh_or_py``. The regex path answers a whole-file question
    ("does this file contain a spawn, a console target, and a suppression?"),
    which is the wrong question in two measured directions:

      - FALSE NEGATIVE, the defect this closes: one ``creationflags=``
        anywhere silences the guard for every OTHER spawn in the same file.
        Measured 2026-08-21 across this repo: 65 files blind, among them live
        CLIs such as ``coordinator/bin/publish.py`` (7 suppressed spawns
        standing in front of 9 bare ones). Reproduced order-independently.
      - FALSE POSITIVE, its mirror: one ``DETACHED_PROCESS`` line condemns
        every spawn in its file via ``_PY_NO_OP_SUPPRESSION_RE``, which shares
        the same file-wide scope.

    ``conclusive`` is True only when the AST view is complete enough to
    REPLACE the file-wide no-op-suppression leg: the file parsed, at least one
    recognized spawn call was found, every one of them resolved its own argv0,
    none carried a ``**`` splat that could smuggle a kwarg past this walk, and
    every ``creationflags=`` value was readable from its own source. Anything
    less and the coarse leg keeps its say — a detector that cannot see a
    construct must not be the reason a guard goes quiet about it.

    The verdict never suppresses a regex fire (the caller ORs them), so this
    path can only make the guard louder, in line with the module-level
    widen-never-narrow rule — with the single, deliberately-scoped exception
    of the ``conclusive`` no-op leg above.

    Measurement and remit: `state/audits/2026-08-21-detached-process-console-
    window-storm.md`.
    """
    tree = _ast_parse_whole_or_fragment(content)
    if tree is None:
        return _AST_UNKNOWN, False

    fires = False
    saw_call = False
    complete = True
    accounted: set = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _ast_spawn_kind(node.func)
        if kind is None:
            # An UNRECOGNIZED call carrying ``creationflags=`` is a spawn this
            # while the leg it retires (``_PY_NO_OP_SUPPRESSION_RE``) is a
            #     Popen(["python.exe", "s.py"], creationflags=subprocess.DETACHED_PROCESS)
            # ``_PY_SUBPROCESS_CALL_RE``, so per-call parity was never
            # ``conclusive`` and silenced the DETACHED_PROCESS the second call
            # is what makes this check COMPLETE rather than merely
            #     subprocess.run(["notepad.exe"], creationflags=...CREATE_NO_WINDOW...)
            #     flags = {"creationflags": subprocess.DETACHED_PROCESS}
            # through the RECOGNIZED branch below and is read directly rather
            if any(kw.arg in ("creationflags", None) for kw in node.keywords):
                complete = False
            continue
        saw_call = True

        if not node.args:
            complete = False
            continue
        names, resolved = _ast_argv0_names(node.args[0])
        if not resolved or names is None:
            complete = False
            continue
        if not any(
            name in _AST_CONSOLE_TARGET_NAMES or name == "sys.executable"
            for name in names
        ):
            continue

        if kind == "os":
            fires = True
            continue

        creationflags: Optional[ast.AST] = None
        has_splat = False
        for kw in node.keywords:
            if kw.arg is None:
                has_splat = True
            elif kw.arg == "creationflags":
                creationflags = kw.value

        if creationflags is None:
            if has_splat:
                complete = False
                continue
            fires = True
            continue

        accounted.update(id(sub) for sub in ast.walk(creationflags))
        if _ast_creationflags_opaque(creationflags):
            complete = False
            continue
        if _PY_NO_OP_SUPPRESSION_RE.search(_ast_expr_source(creationflags)):
            fires = True

    # A no-op flag REFERENCE the walk never inspected forfeits conclusiveness.
    #     subprocess.run(["notepad.exe"], creationflags=...CREATE_NO_WINDOW...)
    #     my_popen(["cmd.exe", "/c", "dir"], subprocess.DETACHED_PROCESS)
    # Keyed on the AST IDENTIFIER (`ast.Attribute.attr` / `ast.Name.id`), never
    # prose mention — `FLAG_DOC = "DETACHED_PROCESS is a no-op"` — is an
    if complete:
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Attribute):
                referenced = sub.attr in _AST_NO_OP_FLAG_NAMES
            elif isinstance(sub, ast.Name):
                referenced = sub.id in _AST_NO_OP_FLAG_NAMES
            else:
                continue
            if referenced and id(sub) not in accounted:
                complete = False
                break

    if fires:
        return _AST_FIRE, complete and saw_call
    if not saw_call:
        return _AST_UNKNOWN, False
    return _AST_CLEAN, complete


def _ast_expr_source(expr: ast.AST) -> str:
    """Best-effort source text for an expression node.

    ``ast.unparse`` is 3.9+; the ``ast.dump`` fallback is not decorative —
    it still contains every ``DETACHED_PROCESS``/``CREATE_NEW_CONSOLE``
    identifier as an ``attr=``/``id=`` field, so the no-op-suppression regex
    reads the same verdict off either form. Never let this return "" on the
    failure path: an empty string reads as a clean creationflags value.
    """
    try:
        return ast.unparse(expr)
    except Exception:
        return ast.dump(expr)


def _should_deny_sh_or_py(
    file_path: str, content: str, ast_content: Optional[str] = None
) -> bool:
    """Advisory verdict for ``.sh``/``.py`` content — two detectors, not one.

    ``content`` is comment-stripped and drives the file-wide regex cascade.
    ``ast_content`` is the ORIGINAL, unstripped text and drives the per-call-
    site AST cascade (``_analyze_py_call_sites``); it is optional so the regex
    path stays independently exercisable, and its absence degrades this to the
    pre-2026-08-21 whole-file behaviour rather than to silence. The two are
    ORed: the AST path exists to make the guard fire on spawns the file-wide
    suppression check hid, never to talk it out of a fire it already has.

    The one place the AST path SUBTRACTS is the no-op-suppression leg
    (``_PY_NO_OP_SUPPRESSION_RE``), whose file-wide scope means a single
    ``DETACHED_PROCESS`` line condemns every spawn in its file. That leg
    defers to the per-call-site verdict only when the AST view is
    ``conclusive`` — see ``_analyze_py_call_sites`` for what that word is
    allowed to mean.

    Negative-spec: the regex path is NOT retired by the AST path. An AST-only
    detector cannot fire on a docstring-only file, and
    ``TestCaseCDocstringOnlyProse::test_write_pure_prose_file_still_surfaces_but_only_as_advisory``
    pins (DR-077, whole-file context + advisory) that pure prose MUST still
    surface. The regex cascade is that prose fallback; deleting it to make an
    AST rewrite pass would trade a pinned behaviour for a tidier shape.
    """
    ast_verdict, ast_conclusive = _AST_UNKNOWN, False
    if (
        ast_content is not None
        and file_path.endswith(".py")
        and len(ast_content) <= _MAX_WHOLE_FILE_BYTES
    ):
        ast_verdict, ast_conclusive = _analyze_py_call_sites(ast_content)

    if _PY_SUBPROCESS_CALL_RE.search(content) and _PY_CONSOLE_TARGET_RE.search(content):
        # creationflags/CREATE_NO_WINDOW cannot clear a genuinely
        suppression_scope = _strip_triple_quoted_strings(content)
        if not _PY_SUPPRESSION_RE.search(suppression_scope):
            return True
        # A creationflags value carrying DETACHED_PROCESS/CREATE_NEW_CONSOLE is
        # not suppression at all -- Win32 ignores CREATE_NO_WINDOW alongside
        # allocating windowed consoles. See _PY_NO_OP_SUPPRESSION_RE.
        if _PY_NO_OP_SUPPRESSION_RE.search(suppression_scope):
            if not ast_conclusive:
                return True

    if ast_verdict == _AST_FIRE:
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
        if any(marker in content for marker in _ALLOWLIST_MARKERS):
            return None

        stripped_content = _strip_line_comments(content)

        if file_path.endswith((".ps1", ".psm1")):
            should_advise = _should_deny_ps1(stripped_content)
        else:
            should_advise = _should_deny_sh_or_py(
                file_path, stripped_content, ast_content=content
            )

        if not should_advise:
            return None

        # permissionDecision:"deny". See INTERFACE.md § Envelope — advisory.
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": _REASON,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error (INTERFACE.md fidelity rule 6;
        return None
