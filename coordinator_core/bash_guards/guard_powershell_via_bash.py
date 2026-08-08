"""coordinator_core.bash_guards.guard_powershell_via_bash -- PreToolUse(Bash)
advisory-only guard for the POWERSHELL-VIA-BASH-DOUBLE-QUOTE shape: the Bash
tool invoking ``powershell.exe``/``powershell``/``pwsh`` with an inline
``-Command``/``-c`` script argument that is DOUBLE-quoted and contains a
literal ``$`` -- the exact combination under which bash expands ``$var``/
``$(...)`` inside that argument BEFORE PowerShell ever sees it, silently
altering the script rather than failing loudly.

THE INCIDENT THIS GUARD EXISTS FOR
-----------------------------------
Observed live this session:

    powershell.exe -NoProfile -Command "$p=Get-Process -Id 44448 -EA
    SilentlyContinue; if($p){\"ALIVE $($p.ProcessName)\"}else{'DEAD'}; ..."

reached PowerShell as ``=Get-Process ...; if(){...}`` -- the Bash tool's
own shell had already expanded ``$p`` (an unset bash variable, so it
vanished) and ``$($p.ProcessName)`` (bash command substitution) before the
double-quoted argument ever left bash's hands. PowerShell then died with
``IfStatementMissingCondition``. The damaging case is not the loud syntax
error: it was a wait-for-process poll loop, so the mangled script exited
~1s in and reported success -- a SILENT-WRONG-RESULT failure, not a loud
one, that read as "the thing I was waiting on finished."

This host carries a first-class ``PowerShell`` tool that takes the script
verbatim -- no bash seam, no double-expansion. That is the correct path
every time; this guard exists to make it the CHEAP path too (design-as-
offers, ``~/.claude/CLAUDE.md``: "lead with the alternative").

WHY DOUBLE-QUOTED-WITH-`$`-ONLY
--------------------------------
Bash does not expand ``$``/``$(...)`` inside a SINGLE-quoted argument at
all -- a single-quoted PowerShell script body reaches PowerShell byte-for-
byte as written, so it is not this guard's concern (``_check_powershell_
invocation``'s own quote-kind test declines it). ``-EncodedCommand``/``-e``
carries an already-base64-encoded payload -- there is no unencoded ``$`` for
bash to have a chance at, so it is exempted by flag identity before quote
kind is even inspected. Only a DOUBLE-quoted inline script body containing
an unescaped ``$`` is the live hazard: bash performs parameter/command
substitution inside double quotes (never inside single quotes), so exactly
this combination is where the silent alteration in the incident above
occurs.

DETECTION SHAPE -- RAW-TEXT SCAN, NOT THE SHARED SHLEX TOKENIZER
------------------------------------------------------------------
``_command_tokenizer.tokenize_full_command`` (the shared ``shlex(posix=
True)`` tokenizer every other guard in this package reuses) STRIPS quote
characters as part of producing each token -- by the time a caller sees the
decoded argument text, single- and double-quoted spellings of the identical
character sequence are indistinguishable. That erasure is exactly the
distinction this guard exists to recover, so this module does its own
narrow raw-text regex scan instead of reusing that tokenizer (mirrors
``guard_grep_via_bash.py``'s own declining-to-guess posture: prefer a
narrow, correct, purpose-built check over stretching a shared primitive
past what it can honestly answer). ``_PS_INVOCATION_RE`` locates the FIRST
``-c``/``-Command``/``-e``/``-EncodedCommand`` flag following a
``powershell``/``pwsh`` binary token, keyed on quote-balanced backslash-
escape scanning for the flag's own argument (handles a bash-escaped
``\\"`` inside the double-quoted span, as the incident's own command
carries) -- not a full bash grammar, and not trying to be one: this is an
ADVISORY, never a deny (see "Never denies" below), so a narrow miss costs
nothing but a skipped nudge, while a false positive costs one ignorable
line of context.

NEVER DENIES
------------
This guard's own module docstring commits to the same posture
``guard_grep_via_bash.py`` documents for itself: no branch in this module
ever returns a deny envelope, on any host. The failure mode this guard
warns about is a SILENTLY altered script, not a security boundary --
denying the command outright would be a worse outcome than letting it run
with a warning attached, since the caller may already know the script has
no bash-meaningful ``$`` expansion despite the syntactic double-quote
(a false positive the caller can simply ignore).

Override
--------
``COORDINATOR_OVERRIDE_POWERSHELL_VIA_BASH_GUARD=1`` bypasses this guard
entirely (read inline, at call time, per this package's ``_override``
convention -- never hoisted to module scope).

Spec backlink: cross-repo/inbox/ dispatch, "Guard powershell-via-bash
mangling" (2026-08-08) -- coordinator_core/bash_guards/guard_grep_via_bash.py
is this module's structural template (registration shape, override-note
convention, "declines rather than guesses" posture).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import allow_advisory
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

#: Review: code-reviewer -- vestigial attributes kept for readability
#: consistency with every sibling guard in this package (see
#: ``guard_grep_via_bash.py``'s own identical comment on why these govern
#: nothing here); ``dispatch.py`` hardcodes registration/ordering in its own
#: ``guard_chain`` literal.
MATCHERS = ("Bash",)
PRIORITY = 43

#: This guard's OWN escape hatch -- read inline at call time, never
#: hoisted (this package's established ``_override`` convention).
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_POWERSHELL_VIA_BASH_GUARD"

#: Matches a ``powershell``/``powershell.exe``/``pwsh``/``pwsh.exe`` binary
#: token at a word boundary, optionally preceded by a path prefix. Anchored
#: with lookaround rather than requiring leading whitespace so a
#: segment-initial invocation (start of string) also matches.
_PS_BINARY = (
    r"(?<![\w.\\/-])(?:[\w./\\:-]*[\\/])?"
    r"(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)(?![\w.-])"
)

#: Everything between the binary token and the first recognized flag,
#: consumed one character at a time (e.g. ``-NoProfile``) so the flag
#: alternation below is forced to match the FIRST ``-c``/``-e``-shaped flag
#: actually present, not merely the first occurrence anywhere in the
#: command. Non-greedy and excludes `;`/`&`/`|`/newline so an unrelated
#: LATER shell segment's own flags are never pulled into this scan.
_PS_BETWEEN = (
    r"(?:(?!-(?:e(?:ncodedcommand)?|c(?:ommand)?)\b)[^;&|\n])*?"
)

#: The flag itself -- ``-c``/``-Command`` (script body) or
#: ``-e``/``-EncodedCommand`` (already-encoded, exempt -- see module
#: docstring). Exact spellings only, not every PowerShell-legal unambiguous
#: abbreviation (``-Com``, ``-Comm``, ...) -- a narrower match here only
#: ever means a missed advisory, never a false one, and PowerShell's own
#: abbreviation surface is open-ended enough that chasing it would trade a
#: simple, auditable regex for a guess.
_PS_FLAG = r"-(?P<flag>e(?:ncodedcommand)?|c(?:ommand)?)\b"

#: The flag's own argument: a single quoted span (double or single),
#: immediately following the flag modulo horizontal whitespace. Body is
#: consumed as escaped-pair-or-plain-char so an escaped instance of the
#: SAME quote character (``\"`` inside a double-quoted span -- exactly the
#: incident's own shape) is never mistaken for the closing quote. Bash
#: gives single quotes no escaping meaning at all, and this pattern does
#: not need any -- the loop still terminates correctly at the first bare
#: ``'`` for a single-quoted body, since bash quotes single-quoted text
#: literally with no escape processing to consume prematurely.
_PS_QUOTED_ARG = r'[ \t]*(?P<quote>["\'])(?P<body>(?:\\.|[^\\])*?)(?P=quote)'

_PS_INVOCATION_RE = re.compile(
    "(?i)" + _PS_BINARY + _PS_BETWEEN + _PS_FLAG + _PS_QUOTED_ARG
)

#: A `$` not immediately preceded by a backslash -- bash treats `\$` inside
#: a double-quoted string as a literal dollar sign, not an expansion
#: trigger, so an escaped one must not count toward "this argument WILL be
#: expanded."
_UNESCAPED_DOLLAR_RE = re.compile(r"(?<!\\)\$")


def _extract_command(payload: Dict[str, Any]) -> Optional[str]:
    """Return the CRLF-normalized ``command`` string for a Bash PreToolUse
    payload, or ``None`` if this payload is not a non-empty Bash-family
    call (per ``MATCHERS``)."""
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    return cmd.replace("\r", "")


def _advisory() -> Dict[str, Any]:
    """Advisory-only envelope (never a deny -- see module docstring "Never
    denies"). Leads with the alternative per design-as-offers: the
    PowerShell tool takes the script verbatim, so naming it first is the
    actionable thing a reader does with this message; the mechanism
    (bash's own double-quote expansion) is named second, as the reason the
    alternative matters, not the headline.

    Deliberately does NOT echo the triggering command back: that text is
    FOUND DATA the guard would be quoting, not an offered alternative, so
    ``_message_size``'s own exemption machinery (backtick/cue-window spans)
    does not exempt it -- see that module's own "FOUND DATA vs. AUTHORED
    PROSE" docstring section. The caller already has the command in front
    of them; repeating it back here would only spend prose-cap bytes on
    something the reader does not need restated.
    """
    return allow_advisory(
        "PreToolUse",
        "powershell-via-bash: double-quoted inline script contains `$` -- "
        "bash will silently expand it first. Use the `PowerShell` tool "
        "instead (verbatim, no bash seam), or single-quote the script. %s"
        % operator_override_note(_OVERRIDE_ENV_VAR),
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the powershell-via-bash guard against a PreToolUse(Bash)
    payload.

    Returns ``None`` for every case with nothing actionable to say: no
    ``powershell``/``pwsh`` invocation at all; a matched invocation whose
    flag is ``-EncodedCommand``/``-e`` (already encoded, exempt); a matched
    invocation whose script argument is single-quoted (bash performs no
    expansion inside single quotes, so nothing is at risk); or a
    double-quoted argument carrying no unescaped ``$`` (nothing for bash to
    expand). Returns an advisory-only envelope (every host, never a deny)
    for the one case that matches the incident this guard exists for: a
    double-quoted inline script argument containing an unescaped ``$``.
    """
    cmd = _extract_command(payload)
    if not cmd:
        return None
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    match = _PS_INVOCATION_RE.search(cmd)
    if not match:
        return None
    if match.group("flag").lower().startswith("e"):
        return None  # -EncodedCommand/-e -- already encoded, nothing to expand
    if match.group("quote") != '"':
        return None  # single-quoted -- bash performs no expansion inside it
    if not _UNESCAPED_DOLLAR_RE.search(match.group("body")):
        return None  # double-quoted, but nothing bash would expand

    return _advisory()
