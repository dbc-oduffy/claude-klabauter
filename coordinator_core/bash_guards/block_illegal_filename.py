"""coordinator_core.bash_guards.block_illegal_filename — advisory guard.

Python engine-ification of the BEST-EFFORT Bash arm ONLY of example-doctrine-repo's
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

This is a faithful engine-ification, not a redesign: it ports the reference
hook's Bash-arm pipeline in order —
  1. backslash-newline continuation join,
  2. heredoc-body strip (state-machine, mirroring the reference hook's awk),
  3. quoted-span strip (double- then single-quoted) to build ``CMD_FOR_SCAN``,
  4. fast-bail if neither ``\bmv\b`` nor ``>`` survives the stripped text,
  5. candidate extraction from THREE sources — ``mv``/``git mv`` 2nd non-flag
     arg, ``>``/``>>`` redirect targets (token-boundary anchored to exclude
     ``->``/``=>`` arrows), ``--out``/``-o`` flag values (scanned from the
     heredoc-stripped-but-quote-INTACT text, not ``CMD_FOR_SCAN`` — a
     deliberate asymmetry the reference hook itself documents),
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
Port of: block-illegal-filename.sh (Bash arm; example-doctrine-repo e91827a7, 2026-07-20)
         ; coordinator-safe-name.sh (csn_check) (example-doctrine-repo 721a71f4, 2026-07-21)
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards._helpers import csn_check as _csn_check
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core._hook_envelope import allow_advisory

CLASS = "advisory"
MATCHERS = ["Bash"]
PRIORITY = 100

#: Escape hatch — shared name with the
#: Write/Edit sibling module by design (both arms of one legacy .sh).
_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_ILLEGAL_FILENAME"

#: Fast-bail probe (reference hook line 352): `grep -qE '\bmv\b|>'`.
_MV_WORD_RE = re.compile(r"\bmv\b")

#: mv/git mv token-scan tokenizer (reference hook lines 422-437's awk split).
_TOKEN_SPLIT_RE = re.compile(r"[ \t\n\r\f\v]+")

#: >/>> redirect-target scan (reference hook lines 458-460): '>' must be
#: preceded by whitespace, a fd-digit, or line-start — excludes '->'/'=>'.
_REDIR_RE = re.compile(r"(?:^|[ \t]|[0-9])>{1,2}[ \t]*[^ \t>|;&]+", re.MULTILINE)
_REDIR_STRIP_RE = re.compile(r"^[ \t0-9]*>*")

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


def _extract_redir_candidates(cmd_for_scan: str) -> List[str]:
    """Port of the reference hook's redirect-target grep+sed+tr pipeline
    (lines 458-460)."""
    redir: List[str] = []
    for line in cmd_for_scan.split("\n"):
        for m in _REDIR_RE.finditer(line):
            frag = _REDIR_STRIP_RE.sub("", m.group(0))
            frag = frag.replace(" ", "").replace("\t", "")
            if frag:
                redir.append(frag)
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


def _make_deny_msg(raw_name: str, illegal_char_hint: str) -> str:
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
        + operator_override_note(_OVERRIDE_ENV)
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
        if tool_name != "Bash":
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

        # Fast bail: no move or redirect-like operator survives at all.
        if not (_MV_WORD_RE.search(cmd_for_scan) or ">" in cmd_for_scan):
            return None

        dest_candidates = _extract_dest_candidates(cmd_for_scan)
        redir_candidates = _extract_redir_candidates(cmd_for_scan)
        out_candidates = _extract_out_candidates(cmd)

        # First-match-wins across DEST -> REDIR -> OUT, mirroring advise()'s
        # exit-on-first-match (never "evaluate all, OR the results").
        for raw_candidate in dest_candidates + redir_candidates + out_candidates:
            result = _check_candidate(raw_candidate)
            if result is None:
                continue
            basename, hint = result
            reason = _make_deny_msg(basename, hint)
            ctx = _advisory_ctx(reason)
            return allow_advisory("PreToolUse", ctx)

        return None
    except Exception:
        # Fail-OPEN on any unexpected error — this arm is advisory-only and
        # best-effort; every extraction failure degrades to "no advisory",
        # never a guard-level crash (mirrors the reference hook's
        # `set -uo pipefail` without `-e`).
        return None
