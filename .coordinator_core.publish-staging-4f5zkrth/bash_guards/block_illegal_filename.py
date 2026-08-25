"""coordinator_core.bash_guards.block_illegal_filename — advisory guard.

Python engine-ification of the BEST-EFFORT Bash arm ONLY of DoE's
``coordinator/hooks/scripts/block-illegal-filename.sh`` PreToolUse hook, per
the W3a/W3b naked-Python hook migration recipe
(scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md
§(b) item 5). The RELIABLE arm (Write|Edit|NotebookEdit) is a SEPARATE module,
``coordinator_core.write_guards.block_illegal_filename`` — the two arms are
deliberately independent module files with two different OUTPUT wrappers
around the one shared ``csn_check`` predicate; collapsing them into one
shared decision shape would be a regression (recipe §(b) item 5, "this
asymmetry is load-bearing and must NOT collapse in the port").

Posture: ADVISORY-ONLY — this arm NEVER blocks. It scans the Bash command
string for ``mv``/``git mv`` destination targets, shell-redirection targets,
and ``--out``/``-o`` flag values, and on a match returns an
``allow`` + ``additionalContext`` envelope (never ``deny``). This is
explicitly a best-effort static scan, NOT the enforcement net — the
commit/merge backstop is. Four false-positive incidents motivated the
advisory-only posture on this arm specifically.

This is a faithful engine-ification of the reference hook's pipeline, with
one deliberate departure (C3,
``docs/plans/2026-08-07-deny-legs-reachable-and-quoted-redirects-visible.md``):
the reference hook's redirect-target scan was quote-BLIND (it ran over the
same quoted-span-stripped text as the mv/dest scan), which made every
quoted redirect target — spaced or unspaced — invisible. This port's
redirect extraction is quote-AWARE instead; see below and
``_extract_redir_candidates``'s own docstring for the mechanism.

Pipeline, in order —
  1. backslash-newline continuation join,
  2. heredoc-body strip (state-machine, mirroring the reference hook's awk),
  3. quoted-span strip (double- then single-quoted) to build ``CMD_FOR_SCAN``
     — used by the mv/dest scan only (step 5's dest source),
  4. fast-bail if neither ``\bmv\b`` survives ``CMD_FOR_SCAN`` nor ``>``
     survives the quote-INTACT heredoc-stripped text (the text the redirect
     extractor actually reads — bailing on ``CMD_FOR_SCAN`` here would miss
     a command whose only ``>`` sits inside a quoted target),
  5. candidate extraction from THREE sources — ``mv``/``git mv`` 2nd non-flag
     arg (from ``CMD_FOR_SCAN``), ``>``/``>>`` redirect targets (quote-AWARE
     scan over the quote-INTACT text: a '>' only counts as an operator at
     quote-depth 0 with an allowed preceding character, which is what
     excludes ``->``/``=>`` arrows AND a quoted '>' that is merely argument
     content; the captured target may itself be quoted and may contain
     spaces), ``--out``/``-o`` flag values (scanned from the
     heredoc-stripped-but-quote-INTACT text — the same asymmetry the
     reference hook itself documents, now shared with the redirect scan),
  6. per-candidate basename extraction + ``csn_check`` — first illegal
     candidate wins (mirrors the reference hook's ``advise()`` helper, which
     emits its envelope and ``exit 0``s immediately on the FIRST match across
     all three candidate lists, evaluated in DEST → REDIR → OUT order).

Net simplification (recipe §(b) item 5, explicitly called out as bash-only
baggage the Python port sheds for free): the reference hook's quoted-span
strip uses a perl-vs-sed fallback ladder because bash's sed is line-oriented
and cannot span a multi-line quoted string in one pass; Python's
``re.sub(r'"[^"]*"', ...)`` already spans newlines with a plain character
class (no ``DOTALL``/multi-line mode needed), so there is no fallback ladder
to port. Likewise, the reference hook's candidate-cleanup step routes a
``<<``/``<(`` strip through ``sed`` rather than bash parameter expansion
specifically because bash's own lexer treats those as live heredoc/process-sub
START tokens even inside a glob pattern — that hazard does not exist in
Python at all.

Negative-spec:
  - Does NOT deny/block under any circumstance — CLASS is ``"advisory"``, not
    ``"hard-deny"``. A caller that only wires hard-deny guards from this
    package will simply never see this module fire; that is correct, not a
    gap (see the reference hook's own "TWO ARMS" header: the reliable arm is
    the only blocking surface).
  - Does NOT port the RELIABLE arm's Write/Edit/NotebookEdit logic — that is
    ``coordinator_core.write_guards.block_illegal_filename``.
  - Does NOT re-derive ``csn_check`` — imports the ONE shared port from
    ``coordinator_core.bash_guards._helpers`` (recipe §(a) "Summary" item 2:
    "one shared helper, not two").
  - Does NOT parse ``csn_check``'s bash stderr message to recover the
    "char hint" (the reference hook's ``sed -n`` alternation over
    ``CHECK_MSG``) — the Python ``csn_check`` already RETURNS the hint token
    directly (``"trailing dot"``, the illegal char itself, or
    ``"control character"``), making that indirection moot.
  - Does NOT evaluate all candidates and OR the results (contrast
    ``nudge-subagent-scoped-commit.sh``'s fold-candidate #4, which does) —
    this guard mirrors ``advise()``'s exit-on-first-match behavior exactly:
    first illegal candidate across DEST → REDIR → OUT wins, remaining
    candidates are never checked.
  - Never raises: any unexpected input shape returns ``None`` (ALLOW/no-op) —
    matches the reference hook's ``set -uo pipefail`` (no ``-e``) discipline;
    every extraction failure degrades to "skip this candidate," never a
    guard-level crash.

Escape hatch: ``COORDINATOR_OVERRIDE_ILLEGAL_FILENAME=1`` — the SAME env var
name as the reliable-arm sibling module (one escape hatch covered BOTH arms
of the single ``.sh`` file this ported from). Read inline at ``check()`` call
time, never hoisted to module scope (F2 discipline — recipe §(e), "every
``COORDINATOR_OVERRIDE_*`` read MUST be an inline ``os.environ.get(...)``
call inside the check function body").

Spec backlink: docs/plans/2026-06-30-cross-platform-file-naming-helper.md § D1
Tripwire entry: docs/wiki/coordinator-tripwires.md § BLOCK-ILLEGAL-FILENAME
Port of: block-illegal-filename.sh (Bash arm; DoE e91827a7, 2026-07-20)
         ; coordinator-safe-name.sh (csn_check) (DoE 721a71f4, 2026-07-21)
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._helpers import csn_check as _csn_check
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core._hook_envelope import allow_advisory

CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 100

#: Escape hatch — shared name with the
#: Write/Edit sibling module by design (both arms of one legacy .sh).
_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_ILLEGAL_FILENAME"

#: Fast-bail probe (reference hook line 352): `grep -qE '\bmv\b|>'`.
_MV_WORD_RE = re.compile(r"\bmv\b")

#: mv/git mv token-scan tokenizer (reference hook lines 422-437's awk split).
_TOKEN_SPLIT_RE = re.compile(r"[ \t\n\r\f\v]+")

#: Preceding-char whitelist for a '>' to count as a redirect OPERATOR
#: (mirrors the retired ``_REDIR_RE``'s ``(?:^|[ \t]|[0-9])`` prefix class):
#: start-of-command, start-of-line (after '\n'), whitespace, or an fd digit.
#: '-' and '=' are deliberately excluded, which is what makes '->'/'=>'
#: arrows fall through untouched — no separate arrow check needed. '&' is
#: included (review: MINOR-2) so ``&>`` (combined stdout+stderr redirect) is
#: recognised as an operator instead of being silently missed.
_REDIR_PRECEDING_OK = set(" \t\n0123456789&")

#: Characters that end an unquoted redirect-target word (mirrors the retired
#: ``_REDIR_RE``'s target class ``[^ \t>|;&]+`` upper bound). ``\n``/``\r``
#: are load-bearing (review: BLOCKER-1) -- without them an unquoted target
#: runs past its own line and swallows the next command as part of the
#: "filename". ``(``/``)`` (review: NIT-1) keep a process-substitution body
#: (``tee >(grep foo) < in``) from becoming a candidate at all.
_REDIR_TARGET_STOP = set(" \t\n\r>|;&()")

#: --out/-o flag-value scan (reference hook lines 463-465).
_OUT_RE = re.compile(r"(?:--out|-o)[ \t]+(\"[^\"]*\"|'[^']*'|[^ \t]+)", re.MULTILINE)

#: Glued-on heredoc/process-sub opener strip (reference hook lines 374-392).
_HEREDOC_GLUE_RE = re.compile(r"<<.*$")
_PROCSUB_GLUE_RE = re.compile(r"<\(.*$")


def _strip_heredocs(cmd: str) -> str:
    """Port of the reference hook's awk heredoc-body-strip state machine
    (lines 300-321). Handles ``<<DELIM``, ``<<-DELIM``, ``<<'DELIM'``,
    ``<<"DELIM"`` open/close, tab-stripping for the ``-`` form. Best-effort:
    does not handle nested heredocs (same v1 limitation as the reference).
    """
    out: List[str] = []
    in_hd = False
    delim = ""
    strip_tabs = False
    for line in cmd.split("\n"):
        if in_hd:
            check = line
            if strip_tabs:
                check = check.lstrip("\t")
            if check == delim:
                in_hd = False
                strip_tabs = False
            continue  # heredoc-body lines (incl. the delimiter line) are dropped
        if "<<" in line:
            # awk's `sub(/.*<</, "", rest)` — greedy .* anchors at the LAST
            # occurrence of "<<" in the line, leaving the text after it.
            idx = line.rfind("<<")
            rest = line[idx + 2 :]
            strip_tabs = rest.startswith("-")
            if strip_tabs:
                rest = rest[1:]
            rest = rest.lstrip()
            if rest.startswith("'") or rest.startswith('"'):
                rest = rest[1:]
            m = re.search(r"['\"\t ]", rest)
            if m:
                rest = rest[: m.start()]
            if rest != "":
                delim = rest
                in_hd = True
        out.append(line)
    return "\n".join(out)


def _extract_dest_candidates(cmd_for_scan: str) -> List[str]:
    """Port of the reference hook's mv/git-mv 2nd-non-flag-arg awk scan
    (lines 422-437). State (``in_mv``/``arg_count``) persists across the
    whole scan, not per-line, matching the awk `BEGIN`-once semantics.
    """
    dest: List[str] = []
    in_mv = False
    arg_count = 0
    for t in _TOKEN_SPLIT_RE.split(cmd_for_scan):
        if t == "" or t == "\\":
            continue
        if in_mv:
            if t.startswith("-"):
                continue
            arg_count += 1
            if arg_count == 2:
                dest.append(t)
                in_mv = False
                arg_count = 0
            continue
        if t == "mv":
            in_mv = True
            arg_count = 0
    return dest


def _extract_redir_candidates(cmd: str) -> List[str]:
    """Quote-AWARE redirect-target scan (C3,
    ``docs/plans/2026-08-07-deny-legs-reachable-and-quoted-redirects-visible.md``),
    superseding the retired quote-blind ``_REDIR_RE`` regex-over-``cmd_for_scan``
    approach. Scans the heredoc-stripped, quote-INTACT ``cmd`` directly (same
    input the OUT extractor already reads) so a quoted target is captured as
    ONE unit including any embedded spaces, instead of being erased by the
    upstream quoted-span strip or truncated at the first space.

    A '>' only counts as a redirect OPERATOR when it occurs at quote-depth 0
    AND its preceding character is start-of-command, start-of-line, whitespace,
    or an fd digit (``_REDIR_PRECEDING_OK`` — same whitelist the retired regex
    encoded via ``(?:^|[ \\t]|[0-9])``). This is what excludes both '->'/'=>'
    arrows (preceding char '-'/'=' is not in the whitelist) AND a '>' that is
    merely quoted ARGUMENT content, e.g. ``git commit -m "see foo > bar?"``
    (that '>' sits at quote-depth 1, inside the ``-m`` string, never reached
    as an operator candidate at all — see AC9).

    Once a real operator '>' or '>>' is found, the target word is captured by
    continuing to consume characters — toggling quote state on unescaped
    quote characters so a quoted span is swallowed whole — until an unquoted
    whitespace/operator character (``_REDIR_TARGET_STOP``) or end of string.
    The captured target keeps its quote characters intact; ``_check_candidate``
    already strips them via its existing ``.replace('"', "").replace("'", "")``
    pass, so no new unquoting path is needed here.

    Backslash escaping (C3,
    ``docs/plans/2026-08-07-deny-legs-reachable-and-quoted-redirects-visible.md``,
    regression fix, tightened during C3 rework): on encountering a backslash,
    the scanner unconditionally consumes the backslash AND the single
    character following it, then resumes scanning from the character after
    that — it does NOT special-case "only if the next char is a quote". That
    uniform rule is required for ``\\\\`` (a literal escaped backslash) to
    parse correctly: the first backslash escapes the SECOND backslash, so a
    ``"`` immediately following the pair is a REAL delimiter that DOES
    close/open a quoted span, not an escaped one. An "only skip if next is a
    quote" rule misreads that ``"`` as escaped, desyncs quote depth, and
    swallows the rest of the line (including a genuine redirect after it) —
    a false negative against HEAD's behavior. The consume-two rule correctly
    handles ``\\"``, ``\\'``, ``\\\\``, and every other escape the same way
    at quote-depth 0 and inside ``"..."`` — but NOT inside ``'...'`` (review:
    MAJOR-3): bash never honours backslash escapes inside a single-quoted
    span, where ``\\`` is a literal character and the very next ``'`` always
    closes the span. Both loops below gate the escape branch on
    ``in_quote != "'"`` / ``target_quote != "'"`` accordingly. This applies
    at BOTH the top-level depth-tracking loop and the per-candidate
    target-capture loop below — same hazard, two call sites, and the two
    MUST agree or they desync on the same input.

    Unterminated quote (requirement 3): if a quoted span is opened and never
    closed before end-of-string, the scan is unparseable from that point on.
    This is DELIBERATE SILENCE, not an oversight — an advisory-only guard
    must never guess past a malformed/unterminated command, so the scanner
    simply stops emitting candidates once state desyncs this way (mirrors the
    existing fail-open posture: an extraction failure degrades to "no
    advisory," never a crash or a guess).
    """
    redir: List[str] = []
    n = len(cmd)
    i = 0
    in_quote: Optional[str] = None
    while i < n:
        ch = cmd[i]
        if ch == "\\" and i + 1 < n and in_quote != "'":
            # Backslash unconditionally escapes the NEXT character, whatever
            # it is -- consume both and advance. Special-casing "only if the
            # next char is a quote" desyncs quote-depth tracking on `\\`
            # (the first backslash escapes the SECOND backslash, so a `"`
            # immediately following is a REAL delimiter, not an escaped one).
            # This rule applies at depth 0 and inside "..." only (review:
            # MAJOR-3) -- bash never processes backslash escapes inside
            # '...': there `\` is a literal character and the very next `'`
            # always closes the span, escaped-looking or not.
            i += 2
            continue
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_quote = ch
            i += 1
            continue
        if ch != ">":
            i += 1
            continue
        prev = cmd[i - 1] if i > 0 else "\n"
        if prev not in _REDIR_PRECEDING_OK:
            i += 1
            continue
        # Two-char operator forms: '>>' (append) and '>|' (noclobber
        # override, review: MINOR-2) -- both must consume the second
        # character before target capture starts, or the '|' of '>|' is
        # misread as the target and immediately terminates it empty.
        op_len = 2 if (i + 1 < n and cmd[i + 1] in (">", "|")) else 1
        j = i + op_len
        while j < n and cmd[j] in (" ", "\t"):
            j += 1
        start = j
        target_quote: Optional[str] = None
        while j < n:
            tch = cmd[j]
            if tch == "\\" and j + 1 < n and target_quote != "'":
                # Same escape rule as the top-level scan above -- consume
                # backslash + next char, EXCEPT inside a single-quoted
                # target, where bash treats '\' as a literal (review:
                # MAJOR-3).
                j += 2
                continue
            if target_quote is not None:
                if tch == target_quote:
                    target_quote = None
                j += 1
                continue
            if tch in ("'", '"'):
                target_quote = tch
                j += 1
                continue
            if tch in _REDIR_TARGET_STOP:
                break
            j += 1
        if target_quote is not None:
            # Ran off the end of the string still inside a quoted target —
            # unterminated quote. Deliberate silence: emit nothing and stop
            # scanning entirely, since everything past this point is inside
            # the unterminated span and unparseable.
            break
        target = cmd[start:j]
        if target:
            redir.append(target)
        i = j
    return redir


def _extract_out_candidates(cmd: str) -> List[str]:
    """Port of the reference hook's ``--out``/``-o`` grep+sed pipeline
    (lines 463-465). Scans ``cmd`` (heredoc-stripped, quote-INTACT) — a
    deliberate asymmetry vs. the other two extractors, which scan
    ``cmd_for_scan`` (quote-stripped)."""
    out: List[str] = []
    for line in cmd.split("\n"):
        for m in _OUT_RE.finditer(line):
            out.append(m.group(1))
    return out


def _check_candidate(raw_candidate: str) -> Optional[Tuple[str, str]]:
    """Port of the reference hook's ``check_candidate`` (lines 369-417) minus
    the deny/advise emission — returns ``(basename, char_hint)`` on an
    illegal basename, else ``None`` (safe or un-extractable candidate).
    """
    candidate = raw_candidate.replace('"', "").replace("'", "")
    candidate = _HEREDOC_GLUE_RE.sub("", candidate)
    candidate = _PROCSUB_GLUE_RE.sub("", candidate)
    if candidate == "":
        return None
    if candidate.startswith("$"):
        return None
    if candidate.startswith("-"):
        return None
    idx = max(candidate.rfind("/"), candidate.rfind("\\"))
    basename = candidate[idx + 1 :] if idx >= 0 else candidate
    if basename in ("", ".", ".."):
        return None
    hint = _csn_check(basename)
    if hint is None:
        return None
    return (basename, hint)


def _safe_suggestion(raw_name: str) -> str:
    """Port of ``make_deny_msg``'s safe-suggestion pipeline (see module
    Port-of backlink):
    ``tr ':?*<>|"\\/' '-' | tr -s '-' | sed 's/^-//; s/-$//' | sed 's/[. ]*$//'``
    (duplicated from the write_guards sibling's own local copy — both are
    independent module files per the "one guard = one module file" rule;
    only ``csn_check``/``is_confined_findings_agent`` are the designated
    cross-module shared helpers)."""
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
    """Compressed reason text (message-size discipline, plan chunk C8):
    names the offending char, the Windows-breakage consequence, and a
    ready-to-run rename -- no restated Reference/backstop boilerplate,
    which duplicated what ``_advisory_ctx`` already said. The override
    sentence still routes through ``operator_override_note`` (M17,
    2026-07-30), never hand-written -- see that helper's own docstring."""
    safe_suggestion = _safe_suggestion(raw_name)
    return (
        f"'{raw_name}' has '{illegal_char_hint}' -- illegal on Windows, blocks "
        f"`git checkout`. Use instead: rename to '{safe_suggestion}', or run "
        "`coordinator-safe-name timestamp`. "
        + operator_override_note(_OVERRIDE_ENV, payload=payload)
    )


def _advisory_ctx(reason: str) -> str:
    """Non-blocking prefix over ``_make_deny_msg``'s reason (message-size
    discipline, plan chunk C8) -- the hook-name/backstop-citation tail was
    cut as decoration once the reason itself states what/why/alternative."""
    return f"ADVISORY (non-blocking): {reason}"


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        dialect = dialect_from_tool_name(tool_name)
        if dialect is Dialect.POWERSHELL:
            # C5 (row 20, `docs/reference/guard-dialect-coverage.md`): this
            # guard's own heredoc stripper, glued-heredoc/process-substitution
            # strip, and fd-prefixed redirect scan are all POSIX shell
            # syntax with no PowerShell equivalent parsed here -- re-using
            # them against PowerShell input would be a guess, not a
            # verdict, on a shell this guard's own text scanning was never
            # built to read. Declares SILENT rather than clean, per the
            # plan's "prefer SILENT to a guess" mandate.
            record_silent(
                "block_illegal_filename",
                "PowerShell dialect: heredoc/process-substitution/redirect "
                "scanning here is POSIX-only shell text syntax",
            )
            return None
        if dialect is not Dialect.BASH:
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        cmd = tool_input.get("command") or ""
        if not cmd:
            return None

        # CRLF strip — redundant-but-safe insurance at THIS check function's
        # own entry, independent of any dispatcher-level strip (recipe §(c):
        # "the Python port should keep that same double-strip discipline PER
        # CHECK FUNCTION").
        cmd = cmd.replace("\r", "")

        # Join backslash-newline continuations so a split op is one segment
        # (reference hook lines 289-291).
        cmd = cmd.replace("\\\n", " ")

        # Strip heredoc bodies before scanning for mv/redirect targets.
        cmd = _strip_heredocs(cmd)

        # Strip double- then single-quoted spans to build CMD_FOR_SCAN. Order
        # matters: double-quoted spans first, so an apostrophe inside a
        # double-quoted body is gone before the single-quote pass.
        cmd_for_scan = re.sub(r'"[^"]*"', "", cmd)
        cmd_for_scan = re.sub(r"'[^']*'", "", cmd_for_scan)

        # Fast bail: no move or redirect-like operator survives in the text
        # the extractors actually read. ``mv`` is scanned from the
        # quote-stripped ``cmd_for_scan`` (dest extraction); ``>`` must be
        # checked against the quote-INTACT ``cmd`` (C3 quote-aware redirect
        # extraction now reads ``cmd`` directly, not ``cmd_for_scan``) —
        # bailing on ``cmd_for_scan`` here would under-cover a command whose
        # only '>' lives inside a quoted redirect target.
        if not (_MV_WORD_RE.search(cmd_for_scan) or ">" in cmd):
            return None

        dest_candidates = _extract_dest_candidates(cmd_for_scan)
        redir_candidates = _extract_redir_candidates(cmd)
        out_candidates = _extract_out_candidates(cmd)

        # First-match-wins across DEST -> REDIR -> OUT, mirroring advise()'s
        # exit-on-first-match (never "evaluate all, OR the results").
        for raw_candidate in dest_candidates + redir_candidates + out_candidates:
            result = _check_candidate(raw_candidate)
            if result is None:
                continue
            basename, hint = result
            reason = _make_deny_msg(basename, hint, payload=payload)
            ctx = _advisory_ctx(reason)
            return allow_advisory("PreToolUse", ctx)

        return None
    except Exception:
        # Fail-OPEN on any unexpected error — this arm is advisory-only and
        # best-effort; every extraction failure degrades to "no advisory",
        # never a guard-level crash (mirrors the reference hook's
        # `set -uo pipefail` without `-e`).
        return None
