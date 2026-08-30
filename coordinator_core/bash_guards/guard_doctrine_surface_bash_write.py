"""coordinator_core.bash_guards.guard_doctrine_surface_bash_write --
PreToolUse(Bash|PowerShell) hard-deny guard closing the Bash escape from the
C7 doctrine admission gate (DoE-claude's ``check-claude-md-size.py``,
registered on ``Write|Edit|MultiEdit`` only).

Port of DoE-claude's ``coordinator/hooks/scripts/guard-doctrine-surface-
bash-write.py`` (1537 lines, 488 executable), one of the four folded
PreToolUse(Bash) guards ``preuse-bash-dispatch.py`` runs in-process ahead of
the engine dispatch (see ``docs/plans/2026-08-28-the-four-folded-bash-
guards-get-registered-not-folded.md`` C4) -- this module ports the LOGIC
into the engine's own registered guard chain (``dispatch.py``) so DoE's
in-process fold can be deleted without the cold path losing coverage. Same
predicate, same SINK-based detection strategy; only the transport and the
governed-identifier SOURCE differ (see "GOVERNED IDENTIFIER SOURCE" below).

THE DEFECT THIS CLOSES: a shell redirect (``cat > file``, ``printf >>
file``, ``tee``), an interpreter payload (``python3 -c "open(p,'w').write(
...)"``, ``sed -i``, ``perl -i``), or a heredoc writes a governed doctrine
surface with the Write|Edit|MultiEdit admission gate never firing at all.
This guard is the mirror-image on the Bash/PowerShell surface, the same
paired-guard idiom already established for ``docs/plans/**``
(``write_guards.block_subagent_plan_body_write`` paired with
``bash_guards.block_subagent_plan_body_bash_write``).

GOVERNED IDENTIFIER SOURCE -- the one deliberate divergence from the DoE
original. DoE's copy imports ``_claude_md_ledger.GOVERNED_AUTHORING_
SURFACES`` at module import time and builds its identifier tables once, at
import, from that fixed tuple. This port CANNOT do that: it has no local
``_claude_md_ledger`` (a non-package module reachable only by
``sys.path.insert``, the wrong seam for a fleet-shared process -- see C3's
own body) and per this plan's own Anti-scope, the governed-authoring-
surfaces manifest must be read PER CALL, never memoized, so a cached
manifest cannot freeze to whichever session booted a resident warm server.
So every identifier-dependent helper below takes the caller's resolved
``identifiers_lower: Tuple[str, ...]`` as an explicit parameter rather than
reading a module-level constant -- see ``check()``, which is the one place
per call that turns the caller-supplied ``governed_surfaces`` list into that
tuple (``_governed_identifiers_lower``) and threads it down. No module-level
mutable cache exists anywhere in this file.

DETECTION STRATEGY -- deny by the WRITE SINK, not by the presence of shell
punctuation anywhere in the command (per this plan's own Anti-scope: "Do not
preserve DoE's cheap-detection variant ... The cheap version denied ordinary
read pipelines"):

  1. GOVERNED-IDENTIFIER MENTION (fast path). If no governed identifier
     (full repo-relative path or bare basename, case-folded) appears
     anywhere in the command text, the command cannot be directed at a
     governed surface by any means this guard can see -> ALLOW immediately.

  2. TOP-LEVEL SEGMENTATION. The command is split at top-level ``;``,
     ``&&``, ``||``, ``|``, and newline -- outside quotes and
     ``(...)``/``$(...)`` grouping -- so a pipe or semicolon embedded in a
     quoted argument does not fracture one logical command. Each segment is
     classified independently: a segment that does not itself mention a
     governed identifier cannot deny the command on its own.

  3. PER-SEGMENT SINK CLASSIFICATION. A segment that DOES mention a
     governed identifier is denied if it also contains, in that SAME
     segment: a write marker (``>``/``>>`` not fd-duplication/``/dev/null``,
     ``tee``, ``sed -i``/``perl -i``, ``cp``/``mv``/``install``/``dd``/
     ``truncate``, a write-mode ``open(...)``/``.write(``/``.write_text(``/
     ``.write_bytes(``, ``ex``/``ed``, ``patch``/``rsync``, ``curl -o``,
     ``wget -O``, ``sed ... w <file>``) OR an indirection marker
     (``python``/``python3``/``perl``/``ruby``/``node``, a ``sh``/``bash``/
     ``zsh -c`` subshell, ``eval``, ``xargs``, a backtick, or ``$(...)``) --
     inside an interpreter payload this guard cannot reliably prove a
     mention is a read, so it fails CLOSED.

  4. CROSS-SEGMENT VARIABLE-ASSIGNMENT INDIRECTION. If a segment is (or
     opens with) ``NAME=...`` whose value mentions a governed identifier,
     the WHOLE command is checked for a write marker (not scoped to that
     segment) -- "command assembly from a variable" stays denied by
     construction.

  5. FAIL-CLOSED POSTURE. Once an interpreter/indirection marker AND a
     governed-identifier mention are BOTH present in the same segment (or
     via point 4), the shape is denied unconditionally -- this guard never
     tries to parse whether an interpreter payload is actually reading or
     writing.

  6. READ-SHAPE CARVE-OUT. A segment mentioning a governed identifier but
     containing no write/indirection marker (``cat``, ``head``, ``grep``,
     ``wc``, ``git show/log/diff/status/cat-file``, ``ls``, ``stat``, ...)
     is a read and ALLOWED, even chained via ``|``/``;``/``&&`` with
     unrelated segments -- a pipeline whose SINK is read-only must not be
     denied merely because an upstream/downstream stage mentions an
     unrelated tool.

  7. GIT CONTENT-SAFE SUBCOMMAND CARVE-OUT (``commit``, ``add``, ``log``,
     ``show``, ``diff``, ``status``, ``cat-file``, ``tag``, ``notes``,
     ``rev-parse``, ``ls-files``, ``blame``) -- an allow-LIST, never a
     deny-list. Includes the ``ceremony.scoped_git_commit`` wrapper family
     (``scoped-git-commit``, ``coordinator-safe-commit``, ``spinoff-
     deliverable-and-commit``, basename-exact, ``.cmd``-suffix and
     case-insensitive on Windows), since a commit-message mentioning a
     governed surface in prose is not a path operand. Heredoc bodies are
     stripped first (data, not live shell); quoted spans are stripped
     before the write-marker re-scan (message prose containing ``>`` stays
     allowed) but code-execution markers are re-scanned on the UNSTRIPPED
     text (``git commit -m "$(python3 -c '...')"`` still denies).

  8. GIT CONTENT-MUTATING SUBCOMMANDS ARE THEMSELVES A WRITE MARKER
     (``checkout``, ``restore``, ``switch``, ``apply``, ``reset``,
     ``clean``, ``rm``, ``mv``, ``stash``, ``revert``, ``cherry-pick``,
     ``merge``, ``rebase``, ``pull``) -- ``git checkout HEAD~5 --
     CLAUDE.md`` overwrites working-tree content with no redirect, ``tee``,
     or interpreter at all.

  9. ``coordinator_core.session.claude_md_grant`` CLI CARVE-OUT -- its
     write target is its own grant record for every subcommand, never the
     governed file, so a ``python3 -m coordinator_core.session.
     claude_md_grant`` invocation is read-shape for the governed-identifier
     mention (a real write marker outside a quoted argument still denies).
     CROSS-REPO INVARIANT: this rests on that module's write target never
     changing -- a new subcommand, force-write flag, or path argument there
     requires co-review here (that module is an engine-plane module this
     port CAN see locally, unlike DoE's original comment which flagged it
     as an unauditable cross-repo dependency).

  10. XARGS PIPE INDIRECTION -- ``echo CLAUDE.md | xargs tee`` substitutes
      the identifier into a DIFFERENT segment at runtime; any segment
      containing bare ``xargs`` in a command mentioning a governed
      identifier ANYWHERE denies, whole-command-scoped like point 4.

  11. INTERPRETER-INVOCATION READ-SHAPE -- a segment whose own command
      token IS an interpreter, no OS-exec escape hatch, no ``eval``/
      ``xargs``, is not a ``-m <module>`` invocation, and whose
      governed-identifier mention does NOT survive quoted-span stripping
      (i.e. it is quoted content, e.g.
      ``python3 -c "print('reads a governed surface')"``) is read-shape.

      NARROWED BY SINK 2026-08-29. This carve-out used to decline on ANY
      write marker in the segment. An interpreter payload is a SINGLE
      segment, so a governed read and an unrelated write shared it and the
      carve-out never applied -- the same defect point 4 carried, one leg
      over. A write marker now declines only when
      ``_interpreter_write_sinks_are_ungoverned`` cannot clear it: every
      write must be an analysable literal-path ``open()`` in a write mode
      landing on a non-governed path. Anything else -- a non-literal path,
      a file object bound to a name, a redirect, ``tee``, ``sed -i`` -- is
      unreadable here and still declines.

NEGATIVE-SPEC (accepted over-denial, preserved from the original): a segment
mentioning a governed surface AND an unrelated write marker in the SAME
segment (``git diff CLAUDE.md > /tmp/out.txt``) still denies -- this guard
does not parse which argument a redirect targets within one segment. A
second same-segment over-denial WAS an interpreter payload writing an
unrelated destination while quoting a governed filename; point 11's
by-sink narrowing (2026-08-29) closes it for the analysable literal-open
shape and leaves it standing everywhere else, by design.

A THIRD, UPSTREAM hole is open and is NOT an over-denial: point 1's
prefilter is plain substring matching, so a payload splitting the name
across a concatenation (``open('CLAU' 'DE.md', 'w')``) never reaches any
leg. Filed with its own measurement and a perf constraint at
``state/bug-backlog/2026-08-29-string-concatenation-defeats-the-governed-
identifier-prefilter.yaml``; pinned by a test that asserts the current
wrong behaviour so the fix cannot land silently.

Contract: ``check(payload, governed_surfaces) -> Optional[Dict[str, Any]]``
(this package's own convention, replacing DoE's stdin/exit-code ``main()``).
``governed_surfaces`` is the per-call list ``dispatch.py``'s own
``resolve_governed_authoring_surfaces()`` resolves (never imported directly
by this module -- see "GOVERNED IDENTIFIER SOURCE"); ``None`` or an empty
list fails OPEN (this guard has nothing to key its detection on, mirroring
that resolver's own fail-open contract on a manifest miss). Fires only on
``tool_name in MATCHERS`` (``"Bash"`` or ``"PowerShell"`` -- both dialects
can redirect; see the plan's own C4 row).

Spec backlink: docs/plans/2026-08-28-the-four-folded-bash-guards-get-
registered-not-folded.md § C4; original: docs/plans/2026-07-30-boot-
doctrine-cut-and-refill-gate.md § C7a (DoE-claude)
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

#: Relocated deny-reason explanation on DoE's own wiki -- kept as a citation
#: even though this repo has no local copy of that page, so a reader with
#: the DoE-claude plugin installed can still resolve it. This module itself
#: carries no wiki-anchor resolution machinery (see ``check()``'s own
#: ``resolve_wiki_citation`` parameter docstring): the CALLER
#: (``dispatch.py``'s ``resolve_doctrine_surface_wiki_citation``) resolves
#: this literal into an absolute path off that call's own ``plugin_root``,
#: on the deny path only, falling back to this bare repo-relative literal
#: unchanged whenever resolution is unavailable or declined -- never a wrong
#: absolute-path guess (state/audits/2026-08-29-unverified-parity-findings-
#: measured.md FINDING B).
_WIKI_ANCHOR = (
    "coordinator/docs/wiki/guard-message-concision.md"
    "#doctrine-surface-bash-write-guard-carve-outs-and-remedies"
)


def _governed_identifiers_lower(surfaces: List[str]) -> Tuple[str, ...]:
    """Case-folded, de-duplicated, longest-first tuple of every full
    repo-relative path AND bare basename from ``surfaces`` -- the per-call
    mirror of DoE's import-time ``_GOVERNED_IDENTIFIERS_LOWER``. Computed
    fresh on every ``check()`` call; never cached at module scope (see
    module docstring "GOVERNED IDENTIFIER SOURCE")."""
    identifiers: "set[str]" = set()
    for surface in surfaces:
        identifiers.add(surface)
        identifiers.add(surface.rsplit("/", 1)[-1])
    return tuple(sorted((i.lower() for i in identifiers), key=len, reverse=True))


def _governed_identifier_patterns(identifiers_lower: Tuple[str, ...]) -> Tuple["re.Pattern[str]", ...]:
    """Path-segment-boundary-anchored compiled patterns mirroring
    ``identifiers_lower`` -- used only by ``_names_governed_identifier``,
    which has no live caller in this module (see that function's own
    docstring); ported for parity with the original's structure."""
    return tuple(
        re.compile(r"(?<![A-Za-z0-9])" + re.escape(identifier) + r"(?![A-Za-z0-9])")
        for identifier in identifiers_lower
    )


#: Redirect targets that are never a write to a governed surface: fd
#: duplication (``2>&1``, ``>&2``) and ``/dev/null``.
_SAFE_REDIRECT_RE = re.compile(r"\d?>&\d|>>?\s*/dev/null")
_BARE_REDIRECT_RE = re.compile(r">>?")


def _has_redirect_marker(text: str) -> bool:
    stripped = _SAFE_REDIRECT_RE.sub("", text)
    return bool(_BARE_REDIRECT_RE.search(stripped))


_TEE_RE = re.compile(r"\btee\b")
_SED_INPLACE_RE = re.compile(r"\bsed\b.{0,120}?(-i\b|--in-place\b)", re.DOTALL)
_PERL_INPLACE_RE = re.compile(r"\bperl\b.{0,120}?-i\b", re.DOTALL)
_CP_MV_RE = re.compile(
    r"(?:^|[|&;(]|\|\||&&)\s*(?:\w+=\S*\s+)*(?:sudo\s+|command\s+|env\s+)*"
    r"\b(cp|mv|install|dd|truncate)\b"
)
_WRITE_MODE_OPEN_RE = re.compile(r"open\([^)]*['\"][wax]['\"]")
_WRITE_METHOD_RE = re.compile(r"\.write(_text|_bytes)?\(")
_EX_ED_RE = re.compile(r"\b(ex|ed)\b")
_PATCH_RSYNC_RE = re.compile(r"\b(patch|rsync)\b")
_CURL_OUTPUT_RE = re.compile(r"\bcurl\b.{0,200}?(-o\b|--output\b)", re.DOTALL)
_WGET_OUTPUT_RE = re.compile(r"\bwget\b.{0,200}?(-O\b|--output-document\b)", re.DOTALL)
_SED_WRITE_SCRIPT_RE = re.compile(r"\bsed\b.{0,200}?\bw\s+\S", re.DOTALL)


def _redirect_target_token(segment: str) -> Optional[str]:
    """The token immediately following the last real (non-fd-duplication,
    non-``/dev/null``) bare ``>``/``>>`` in ``segment``. ``None`` when
    ``segment`` carries no such redirect, or it is trailing with nothing
    after it.

    Literal joins are folded first (``> 'CLAU''DE.md'``). This token is read
    for its IDENTITY, never its offsets, so rewriting the segment here is
    safe -- and an unfolded read stopped at the first closing quote, returning
    a truncated name that matched no governed identifier. That is how a split
    name reached a real redirect target and was allowed."""
    segment = _fold_literal_joins(segment)
    masked = _SAFE_REDIRECT_RE.sub(lambda m: " " * len(m.group(0)), segment)
    last = None
    for match in _BARE_REDIRECT_RE.finditer(masked):
        last = match
    if last is None:
        return None
    rest = segment[last.end():].lstrip()
    if not rest:
        return None
    if rest[0] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        return rest[1:end] if end != -1 else rest[1:]
    end = 0
    while end < len(rest) and not rest[end].isspace():
        end += 1
    return rest[:end]


def _has_write_marker(text: str) -> bool:
    if _has_redirect_marker(text):
        return True
    # Marker patterns are scanned on the literal-join fold as well as the raw
    # text: `'t''ee' <governed>` is `tee` to the shell, but `\btee\b` matched
    # neither, so a split write verb carried no marker and the segment read as
    # a plain mention (measured 2026-08-29). Widening only -- a marker found
    # only after folding still has to co-occur with a governed mention in the
    # same segment before anything denies.
    folded = _fold_literal_joins(text)
    for pattern in (
        _TEE_RE,
        _SED_INPLACE_RE,
        _PERL_INPLACE_RE,
        _CP_MV_RE,
        _WRITE_MODE_OPEN_RE,
        _WRITE_METHOD_RE,
        _EX_ED_RE,
        _PATCH_RSYNC_RE,
        _CURL_OUTPUT_RE,
        _WGET_OUTPUT_RE,
        _SED_WRITE_SCRIPT_RE,
    ):
        if pattern.search(text) or pattern.search(folded):
            return True
    return False


_INTERPRETER_RE = re.compile(r"\b(python3?|perl|ruby|node)\b")
_SHELL_DASH_C_RE = re.compile(r"\b(sh|bash|zsh)\b.{0,40}?-c\b", re.DOTALL)
_EVAL_RE = re.compile(r"\beval\b")
_XARGS_RE = re.compile(r"\bxargs\b")
_CMD_SUBST_RE = re.compile(r"\$\(|`")

_INDIRECTION_PATTERNS = (
    _INTERPRETER_RE,
    _SHELL_DASH_C_RE,
    _EVAL_RE,
    _XARGS_RE,
    _CMD_SUBST_RE,
)


def _has_indirection_marker(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INDIRECTION_PATTERNS)


#: A ZERO-WIDTH literal join: two quote characters with nothing between them
#: but an optional ``+``, each optionally backslash-escaped. Covers shell
#: adjacency (``'CLAU''DE.md'``), Python implicit concatenation (the same
#: bytes), Python explicit concatenation (``'CLAU' + 'DE.md'``), either quote
#: style, mixed between them, and the escaped form a payload nested inside a
#: double-quoted shell word must use (``'CLAU'+\"DE.md\"``).
#: WHITESPACE-SEPARATED words are deliberately NOT joined: ``'a' 'b'`` is one
#: string in Python but two arguments in shell, and folding it would invent
#: governed mentions in ordinary commands. That is why a gap requires a ``+``.
_LITERAL_JOIN_RE = re.compile(r"\\?['\"]\\?['\"]|\\?['\"]\s*\+\s*\\?['\"]")

#: One pass collapses every non-overlapping join; a second catches joins the
#: first pass created by removing the quotes between them (``'a''b''c'``).
#: Three is slack, not a measurement -- the loop exits on the first no-op pass.
_LITERAL_JOIN_FOLD_PASSES = 3


def _fold_literal_joins(text: str) -> str:
    """``text`` with zero-width literal joins collapsed, so a governed name
    split across a concatenation reads as contiguous text.

    WHY THIS EXISTS: the prefilter below is plain substring matching, so
    ``open('CLAU' 'DE.md', 'w')`` named no governed surface anywhere in the
    raw command and was allowed at the fast path -- never reaching any sink
    leg (measured 2026-08-29; the defect predated both by-sink narrowings).
    Folding is applied IN ADDITION to the raw text, never instead of it, so
    this can only ever admit more commands to the sink legs, never fewer.

    Bounded: each pass strictly shortens the string, and the loop stops when
    a pass changes nothing, so a pathological payload cannot spin here.

    The quote-character test first is a HOT-PATH guard, not tidiness: this
    runs on every Bash tool call in the fleet, and a command carrying no
    quote at all cannot contain a join, so it must not pay for a regex scan
    to find that out."""
    if "'" not in text and '"' not in text:
        return text
    for _ in range(_LITERAL_JOIN_FOLD_PASSES):
        folded = _LITERAL_JOIN_RE.sub("", text)
        if folded == text:
            return text
        text = folded
    return text


def _mentions_governed_identifier(text: str, identifiers_lower: Tuple[str, ...]) -> bool:
    """Whole-command PREFILTER: does ``text`` mention a governed surface at
    all? Deliberately UNBOUNDED (plain substring) -- see module docstring
    point 1 and the original's own rationale for why an anchored prefilter
    would miss a variable-name-shaped smuggled write.

    Checked against the raw text AND its literal-join fold, so splitting the
    name across a concatenation does not evade the guard. The raw check runs
    first and short-circuits, so the ordinary case -- a command that names a
    governed surface plainly, or one that mentions none -- pays for the fold
    only when the raw text has already missed."""
    lowered = text.lower()
    if any(identifier in lowered for identifier in identifiers_lower):
        return True
    folded = _fold_literal_joins(lowered)
    if folded == lowered:
        return False
    return any(identifier in folded for identifier in identifiers_lower)


def _names_governed_identifier(text: str, patterns: Tuple["re.Pattern[str]", ...]) -> bool:
    """Path-segment-BOUNDARY-anchored precision test -- NEVER a prefilter.
    Ported for structural parity with the original; has no live caller in
    this module today (same as upstream), kept for a future diagnostic
    consumer."""
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in patterns)


def _has_write_marker_for_point3(
    segment: str, identifiers_lower: Tuple[str, ...]
) -> bool:
    """Point 3's write-marker check, narrowed for the BARE-REDIRECT shape
    only -- a redirect counts as evidence of a governed write iff EITHER
    the identifier mention survives quoted-span stripping (a bare operand)
    OR the redirect's own target token names a governed identifier. When
    neither holds, every OTHER write marker is still checked at its
    existing broad, fail-closed scope."""
    if not _has_redirect_marker(segment):
        return _has_write_marker(segment)
    target = _redirect_target_token(segment)
    target_mentions = bool(target) and _mentions_governed_identifier(target, identifiers_lower)
    bare_mention = _mentions_governed_identifier(_strip_quoted_spans(segment), identifiers_lower)
    if target_mentions or bare_mention:
        return True
    without_redirect = _BARE_REDIRECT_RE.sub(" ", _SAFE_REDIRECT_RE.sub(" ", segment))
    return _has_write_marker(without_redirect)


_CODE_EXECUTION_PATTERNS = (
    _INTERPRETER_RE,
    _SHELL_DASH_C_RE,
    _EVAL_RE,
    _XARGS_RE,
)


def _has_code_execution_marker(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CODE_EXECUTION_PATTERNS)


_HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredoc_bodies(text: str) -> str:
    """``text`` with the BODY of every heredoc removed, keeping the command
    line that introduces it. Falls back to locating an undelimited
    (single-line) terminator as a whitespace-bounded token when no line
    matches it exactly, so an agent-emitted command collapsed to one line
    does not have its heredoc body misread as live shell."""
    lines = text.split("\n")
    out: "list[str]" = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = _HEREDOC_START_RE.search(line)
        idx += 1
        if not match:
            out.append(line)
            continue
        terminator = match.group(2)
        scan = idx
        while scan < len(lines) and lines[scan].strip() != terminator:
            scan += 1
        if scan < len(lines):
            out.append(line)
            out.append(lines[scan])
            idx = scan + 1
            continue
        term_token_re = re.compile(r"(?<!\S)" + re.escape(terminator) + r"(?!\S)")
        term_match = term_token_re.search(line, match.end())
        if term_match is None:
            out.append(line)
            continue
        out.append(line[: match.end()])
        out.append(line[term_match.start():])
    return "\n".join(out)


#: A heredoc whose delimiter is QUOTED (``<<'PY'``, ``<<"PY"``). The quoting is
#: the whole point: the shell performs NO expansion inside such a body -- no
#: parameter expansion, no command substitution -- so a ``$(`` or a backtick
#: there is inert text by the shell's own contract, not a substitution the
#: guard is declining to analyse. `_HEREDOC_START_RE`'s own group 1 is
#: OPTIONAL and matches the unquoted form too; this pattern requires it.
_HEREDOC_QUOTED_START_RE = re.compile(r"<<-?\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\1")


def _quoted_heredoc_bodies(text: str) -> "list[str]":
    """The BODY of every QUOTED-delimiter heredoc in ``text``.

    Consumed by point 3's indirection leg alone (see
    `_lies_in_a_quoted_heredoc_body`), never by any write-marker leg: a body
    that genuinely writes a governed surface still denies through
    `_has_write_marker_for_point3` and `_has_stdin_program_var_write`, both
    of which run over these same bytes untouched.

    Deliberately NOT keyed on `_STDIN_PROGRAM_RE`: the quoting alone is what
    makes shell indirection markers inert, whether the body is a program on
    stdin (``python - <<'PY'``) or data (``cat <<'EOF' > out``). An UNQUOTED
    heredoc is excluded exactly because the shell DOES expand its body.
    """
    bodies: "list[str]" = []
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = _HEREDOC_QUOTED_START_RE.search(line)
        idx += 1
        if not match:
            continue
        terminator = match.group(2)
        scan = idx
        while scan < len(lines) and lines[scan].strip() != terminator:
            scan += 1
        if scan < len(lines):
            body = "\n".join(lines[idx:scan])
            idx = scan + 1
            if body:
                bodies.append(body)
    return bodies


def _lies_in_a_quoted_heredoc_body(segment: str, bodies: "Sequence[str]") -> bool:
    """Is this whole segment nothing but text from inside a quoted heredoc?

    Containment, not overlap: a segment straddling the heredoc's introducing
    line and its body is not contained in any body, so the introducing line's
    own markers keep their full force. An empty/whitespace segment is never
    treated as inert -- it carries no mention either way, and answering True
    for it would be a claim about nothing.
    """
    stripped = segment.strip()
    if not stripped:
        return False
    return any(stripped in body for body in bodies)


_STDIN_PROGRAM_RE = re.compile(
    r"(?:^|[;&|]|\s)(?:python3?|perl|ruby|node)\s+-(?=\s|$)"
    r"|(?:^|[;&|]|\s)(?:bash|sh)\s+-s(?=\s|$)"
    r"|(?:^|[;&|]|\s)(?:bash|sh)\s*(?=<<)"
)


def _stdin_program_heredoc_bodies(text: str) -> "list[str]":
    """The BODY of every heredoc whose introducing line makes stdin the
    program (``_STDIN_PROGRAM_RE``) -- for these the body IS executed code,
    unlike every other heredoc ``_strip_heredoc_bodies`` treats as inert
    stdin data."""
    bodies: "list[str]" = []
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = _HEREDOC_START_RE.search(line)
        idx += 1
        if not match:
            continue
        terminator = match.group(2)
        scan = idx
        while scan < len(lines) and lines[scan].strip() != terminator:
            scan += 1
        body = "\n".join(lines[idx:scan]) if scan < len(lines) else ""
        if scan < len(lines):
            idx = scan + 1
        if body and _STDIN_PROGRAM_RE.search(line[: match.start()] + " "):
            bodies.append(body)
    return bodies


_PAYLOAD_ASSIGN_RE = re.compile(
    r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?:('''|\"\"\")([\s\S]*?)\2"
    r"|(['\"])([^'\"]*)\4|([^\s;&|()]+))"
)


def _has_stdin_program_var_write(cmd: str, identifiers_lower: Tuple[str, ...]) -> bool:
    """True iff a stdin-as-program heredoc body binds a governed doctrine
    surface to a name and then writes THROUGH that name -- closes the seam
    between point 3 (same-line identifier+marker) and point 4 (assign-then-
    dereference, but scanned on heredoc-stripped text)."""
    for body in _stdin_program_heredoc_bodies(cmd):
        for match in _PAYLOAD_ASSIGN_RE.finditer(body):
            name = match.group(1)
            value = (
                match.group(3) or match.group(5) or match.group(6) or ""
            ).strip().replace("\\", "/")
            if " " in value:
                continue
            if not _mentions_governed_identifier(value.rsplit("/", 1)[-1], identifiers_lower):
                continue
            deref = re.compile(
                r"open\s*\(\s*%s\s*,\s*['\"][wax]"
                r"|%s\s*,\s*['\"][wax]"
                r"|%s\s*\)\s*\.\s*write"
                r"|>\s*\$?\{?%s\}?\b" % ((re.escape(name),) * 4)
            )
            if deref.search(body):
                return True
    return False


def _copy_command_substitution(text: str, start: int) -> "tuple[str, int]":
    n = len(text)
    if text[start] == "`":
        end = start + 1
        while end < n and text[end] != "`":
            end += 1
        end = min(end + 1, n)
        return text[start:end], end - start
    depth = 0
    i = start
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    return text[start:i], i - start


def _strip_quoted_spans(text: str) -> str:
    """``text`` with the contents of single- and double-quoted spans
    removed (quote characters kept), EXCEPT a command-substitution span
    (``$(...)`` or a backtick pair) inside the quote, which is copied
    through verbatim -- bash evaluates substitution inside a quoted
    argument regardless of the surrounding quotes."""
    out: "list[str]" = []
    quote: Optional[str] = None
    prev = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote and prev != "\\":
                quote = None
                out.append(ch)
                prev = ch
                i += 1
                continue
            if ch == "`" or text[i: i + 2] == "$(":
                span, consumed = _copy_command_substitution(text, i)
                out.append(span)
                prev = span[-1] if span else prev
                i += consumed
                continue
            prev = ch
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev = ch
            i += 1
            continue
        out.append(ch)
        prev = ch
        i += 1
    return "".join(out)


_GIT_CONTENT_SAFE_SUBCOMMANDS = frozenset(
    {
        "commit", "add", "log", "show", "diff", "status", "cat-file",
        "tag", "notes", "rev-parse", "ls-files", "blame",
    }
)

_GIT_CONTENT_MUTATING_SUBCOMMANDS = frozenset(
    {
        "checkout", "restore", "apply", "reset", "clean", "rm", "mv",
        "stash", "switch", "revert", "cherry-pick", "merge", "rebase", "pull",
    }
)

_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})

_ASSIGN_RE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=")


def _git_subcommand(segment: str) -> Optional[str]:
    # Folded before tokenizing: `'g''it' checkout HEAD~5 -- <governed>` is a
    # git content mutation to the shell, but the raw first token was never the
    # literal `git`, so point 8 never classified it and the segment fell
    # through to the read-shape carve-out (measured 2026-08-29). This reads
    # the segment for verb IDENTITY only, never to reconstruct arguments --
    # which is also why the quote characters come off each token: folding
    # `'g''it'` leaves `'git'`, still not the literal `git` the walk below
    # compares against.
    tokens = [
        token.replace("'", "").replace('"', "")
        for token in _fold_literal_joins(segment).split()
    ]
    idx = 0
    while idx < len(tokens) and tokens[idx] != "git":
        if not _ASSIGN_RE.match(tokens[idx]):
            return None
        idx += 1
    if idx >= len(tokens):
        return None
    idx += 1
    while idx < len(tokens):
        token = tokens[idx]
        if not token.startswith("-"):
            return token
        if token in _GIT_VALUE_OPTS:
            idx += 2
            continue
        idx += 1
    return None


_COMMIT_WRAPPER_BASENAMES = frozenset(
    {"scoped-git-commit", "coordinator-safe-commit", "spinoff-deliverable-and-commit"}
)

_CMD_SUFFIX_RE = re.compile(r"\.cmd$", re.IGNORECASE)


def _segment_command_token(segment: str) -> Optional[str]:
    tokens = segment.split()
    for token in tokens:
        if _ASSIGN_RE.match(token):
            continue
        return token
    return None


def _command_token_basename(token: str) -> str:
    stripped = token.strip("'\"")
    return stripped.rsplit("/", 1)[-1]


def _is_commit_wrapper_command(segment: str) -> bool:
    token = _segment_command_token(segment)
    if token is None:
        return False
    basename = _command_token_basename(token)
    basename = _CMD_SUFFIX_RE.sub("", basename)
    return basename.lower() in _COMMIT_WRAPPER_BASENAMES


def _is_commit_wrapper_read_shape(segment: str) -> bool:
    if not _is_commit_wrapper_command(segment):
        return False
    without_heredocs = _strip_heredoc_bodies(segment)
    if _has_write_marker(_strip_quoted_spans(without_heredocs)):
        return False
    return not _has_code_execution_marker(without_heredocs)


def _is_git_content_mutation(segment: str) -> bool:
    return _git_subcommand(segment) in _GIT_CONTENT_MUTATING_SUBCOMMANDS


def _is_git_read_shape(segment: str) -> bool:
    if _git_subcommand(segment) not in _GIT_CONTENT_SAFE_SUBCOMMANDS:
        return False
    without_heredocs = _strip_heredoc_bodies(segment)
    if _has_write_marker(_strip_quoted_spans(without_heredocs)):
        return False
    return not _has_code_execution_marker(without_heredocs)


#: The known-safe grant-CLI module -- see module docstring point 9. Unlike
#: DoE's original comment (which flagged this as an unauditable cross-repo
#: dependency), this module IS engine-plane code this repo hosts directly.
_CLAUDE_MD_GRANT_MODULE = "coordinator_core.session.claude_md_grant"

_PYTHON_BASENAMES = frozenset({"python", "python3"})


def _python_dash_m_module(segment: str) -> Optional[str]:
    tokens = segment.split()
    idx = 0
    while idx < len(tokens) and _ASSIGN_RE.match(tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return None
    if _command_token_basename(tokens[idx]) not in _PYTHON_BASENAMES:
        return None
    idx += 1
    while idx < len(tokens):
        if tokens[idx] == "-m" and idx + 1 < len(tokens):
            return tokens[idx + 1].strip("'\"")
        idx += 1
    return None


def _is_claude_md_grant_invocation(segment: str) -> bool:
    return _python_dash_m_module(segment) == _CLAUDE_MD_GRANT_MODULE


def _is_claude_md_grant_read_shape(segment: str) -> bool:
    if not _is_claude_md_grant_invocation(segment):
        return False
    without_heredocs = _strip_heredoc_bodies(segment)
    return not _has_write_marker(_strip_quoted_spans(without_heredocs))


def _split_top_level_segments(cmd: str) -> "list[str]":
    """Split ``cmd`` at top-level ``;``, ``&&``, ``||``, ``|``, and
    newline -- outside quotes and ``(...)``/``$(...)`` grouping."""
    segments: "list[str]" = []
    current: "list[str]" = []
    quote: Optional[str] = None
    paren_depth = 0
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            current.append(ch)
            if ch == quote and cmd[i - 1] != "\\":
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "(":
            paren_depth += 1
            current.append(ch)
            i += 1
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            current.append(ch)
            i += 1
            continue
        if paren_depth == 0:
            if cmd[i: i + 2] in ("&&", "||"):
                segments.append("".join(current))
                current = []
                i += 2
                continue
            if ch in (";", "|", "\n"):
                segments.append("".join(current))
                current = []
                i += 1
                continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments


def _has_var_assignment_indirection(
    segments: "list[str]", identifiers_lower: Tuple[str, ...]
) -> bool:
    for segment in segments:
        if _ASSIGN_RE.match(segment) and _mentions_governed_identifier(segment, identifiers_lower):
            return True
    return False


_ASSIGN_NAME_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=")
_VAR_DEREF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _governed_bound_variables(
    segments: "list[str]", identifiers_lower: Tuple[str, ...]
) -> "set[str]":
    """Variable names bound to a governed path, following aliases.

    ``p=<governed>`` binds ``p`` directly; ``q=$p`` then binds ``q`` too. The
    fixed-point loop is bounded (an alias chain longer than the segment count
    cannot exist) so a pathological command cannot spin here."""
    bound: "set[str]" = set()
    for _ in range(len(segments) + 1):
        changed = False
        for segment in segments:
            match = _ASSIGN_NAME_RE.match(segment)
            if not match:
                continue
            name = match.group(1)
            if name in bound:
                continue
            value = segment[match.end():]
            if _mentions_governed_identifier(segment, identifiers_lower) or any(
                deref in bound for deref in _VAR_DEREF_RE.findall(value)
            ):
                bound.add(name)
                changed = True
        if not changed:
            break
    return bound


def _assignment_indirection_reaches_a_write(
    segments: "list[str]", identifiers_lower: Tuple[str, ...]
) -> bool:
    """Point 4's by-SINK narrowing -- MEASURED FALSE POSITIVE FIX.

    Point 4 denied on "some segment assigns a governed path to a variable"
    AND "a write marker exists ANYWHERE in the whole command". The second
    conjunct never asked what the write actually TARGETS, so a command that
    merely READS the governed file through the variable and writes somewhere
    unrelated was denied. Reproduced independently by three sessions in one
    day (2026-08-28), minimal pair, the only difference being the assignment:

        p=<governed> ; cat $p ; echo x > /tmp/probe.txt   -> denied (wrong)
        cat <governed> ;        echo x > /tmp/probe.txt   -> allowed

    This mirrors, for point 4, exactly the narrowing
    ``_has_write_marker_for_point3`` already applies to point 3: a REDIRECT
    counts as evidence of a governed write only when its own target names one
    -- here, when the target dereferences a variable bound to a governed path
    (``> $p``, ``> "${p}"``) or names a governed identifier outright.

    FAIL-CLOSED EVERYWHERE ELSE, deliberately. Only the plain-redirect shape
    is analysable by target token. A segment carrying any OTHER write marker
    (``tee``, ``cp``/``mv``, ``sed -i``, an interpreter payload, ``xargs``)
    keeps point 4's original broad behaviour, because this guard cannot
    cheaply tell ``tee $p`` from ``tee /tmp/x``. Narrowing those needs
    argument parsing per marker family; it is not attempted here, and a
    future narrowing must add them one measured family at a time.

    Ported from DoE-claude ``9d1404fa6``; the only divergence is that
    ``identifiers_lower`` is threaded in per call rather than read off an
    import-time constant (module docstring, "GOVERNED IDENTIFIER SOURCE")."""
    bound = _governed_bound_variables(segments, identifiers_lower)
    for segment in segments:
        if not _has_write_marker(segment):
            continue
        if not _has_redirect_marker(segment):
            return True  # unanalysable marker family -- fail closed
        without_redirect = _BARE_REDIRECT_RE.sub(
            " ", _SAFE_REDIRECT_RE.sub(" ", segment)
        )
        if _has_write_marker(without_redirect):
            return True  # a second, unanalysable marker rides along
        target = _redirect_target_token(segment)
        if not target:
            return True  # cannot resolve the destination -- fail closed
        if _mentions_governed_identifier(target, identifiers_lower):
            return True
        if any(deref in bound for deref in _VAR_DEREF_RE.findall(target)):
            return True
    return False


def _has_xargs_pipe_indirection(
    segments: "list[str]", identifiers_lower: Tuple[str, ...]
) -> bool:
    if not any(_mentions_governed_identifier(segment, identifiers_lower) for segment in segments):
        return False
    return any(_XARGS_RE.search(segment) for segment in segments)


_OS_EXEC_RE = re.compile(
    r"\bos\.system\(|\bsubprocess\.(run|call|Popen|check_call|check_output)\("
    r"|shell\s*=\s*True|\beval\(|\bexec\("
)


def _has_os_exec_marker(text: str) -> bool:
    return bool(_OS_EXEC_RE.search(text))


_OPEN_CALL_RE = re.compile(r"\bopen\s*\(")
_WRITE_MODE_RE = re.compile(r"['\"][^'\"]*[wax][^'\"]*['\"]")
_LITERAL_FIRST_ARG_RE = re.compile(r"\A\s*(['\"])(?P<path>[^'\"]*)\1\s*(?:,|\Z)")
_TRAILING_WRITE_CALL_RE = re.compile(r"\A\s*\.\s*write(?:_text|_bytes)?\s*\(")


def _open_call_spans(segment: str) -> "list[tuple[int, int, str]]":
    """Every ``open(`` call in ``segment`` as ``(start, end, args)``, where
    ``end`` is one past the call's matching close paren and ``args`` is the
    raw argument text. Depth-counted rather than regex-matched: an argument
    list can itself contain parens (``open(str(p), 'w')``), and a regex that
    stops at the first ``)`` would truncate the mode and read a write as a
    read. A call whose paren never closes is skipped, so a truncated payload
    contributes no analysable span and the caller falls back to closed."""
    spans: "list[tuple[int, int, str]]" = []
    for match in _OPEN_CALL_RE.finditer(segment):
        depth = 0
        i = match.end() - 1
        while i < len(segment):
            if segment[i] == "(":
                depth += 1
            elif segment[i] == ")":
                depth -= 1
                if depth == 0:
                    spans.append((match.start(), i + 1, segment[match.end():i]))
                    break
            i += 1
    return spans


def _interpreter_write_sinks_are_ungoverned(
    segment: str, identifiers_lower: Tuple[str, ...]
) -> bool:
    """Point 3's by-SINK narrowing for the INTERPRETER shape -- MEASURED
    FALSE POSITIVE FIX, the companion leg to
    ``_assignment_indirection_reaches_a_write``.

    ``_is_interpreter_read_shape`` declines the moment the segment carries
    ANY write marker, regardless of what that write targets. A ``python3 -c``
    payload is a SINGLE segment, so a governed READ and an unrelated write
    share it and the carve-out never applies:

        python3 -c "print(open('<governed>').read()); open('/tmp/x','w').write('y')"

    ...is denied even though the only write lands in scratch. Same defect as
    point 4's, one leg over: the marker was never related to its sink.

    True only when EVERY write in the payload is an analysable literal-path
    ``open()`` in a write mode and none of those paths names a governed
    surface. A write-mode ``open()`` whose path is not a string literal
    (``open(p, 'w')``) is unresolvable and returns False, as does any write
    marker left over once the analysable opens are blanked out -- a
    redirect, ``tee``, ``sed -i``, ``.write(`` on a name bound earlier
    (``f = open(...)`` then ``f.write(...)``), or anything else.

    FAIL-CLOSED EVERYWHERE ELSE, deliberately, and narrower than point 4's
    equivalent: only the adjacent ``open(<literal>, <write mode>)`` shape --
    optionally chained into ``.write()``/``.write_text()``/``.write_bytes()``
    -- is analysed. Widening this to a bound file object needs dataflow
    inside the payload, which this guard does not have and must not fake.
    Pinned by a test so a later widening is a deliberate act.

    READS ARE NOT SINKS. A read-mode ``open('<governed>')`` is left standing
    on purpose: reading a governed surface is exactly the shape this
    narrowing exists to stop denying."""
    blanked = list(segment)
    analysable = False
    for start, end, args in _open_call_spans(segment):
        if not _WRITE_MODE_RE.search(args[args.find(",") + 1:]) or "," not in args:
            continue  # read-mode open -- not a sink, leave it standing
        literal = _LITERAL_FIRST_ARG_RE.match(args)
        if literal is None:
            return False  # write target is not a literal -- unresolvable
        if _mentions_governed_identifier(literal.group("path"), identifiers_lower):
            return False  # the write names a governed surface
        analysable = True
        stop = end
        chained = _TRAILING_WRITE_CALL_RE.match(segment[end:])
        if chained is not None:
            stop = end + chained.end()
        for i in range(start, stop):
            blanked[i] = " "
    if not analysable:
        return False
    return not _has_write_marker("".join(blanked))


def _is_interpreter_read_shape(segment: str, identifiers_lower: Tuple[str, ...]) -> bool:
    """See module docstring point 11 for the full rationale."""
    token = _segment_command_token(segment)
    if token is None:
        return False
    if _command_token_basename(token) not in _PYTHON_BASENAMES and _command_token_basename(
        token
    ) not in ("perl", "ruby", "node"):
        return False
    if _python_dash_m_module(segment) is not None:
        return False
    if _has_write_marker(segment) and not _interpreter_write_sinks_are_ungoverned(
        segment, identifiers_lower
    ):
        return False
    if _has_os_exec_marker(segment):
        return False
    if _EVAL_RE.search(segment) or _XARGS_RE.search(segment):
        return False
    return not _mentions_governed_identifier(_strip_quoted_spans(segment), identifiers_lower)


def is_denied_bash_write(cmd: str, identifiers_lower: Tuple[str, ...]) -> bool:
    """The whole predicate, isolated from payload plumbing so it is directly
    unit-testable. ``identifiers_lower`` is the per-call resolved governed-
    identifier tuple (see module docstring "GOVERNED IDENTIFIER SOURCE").
    Returns True (deny) iff:

      (a) some top-level segment mentions a governed identifier AND that
          same segment contains a write or indirection marker (point 3), or
      (b) some segment is a variable assignment whose value mentions a
          governed identifier, AND some segment's write actually REACHES a
          governed sink -- a redirect whose own target names a governed
          identifier or dereferences a variable bound to one, or any write
          marker family too coarse to analyse by target (point 4, narrowed
          by ``_assignment_indirection_reaches_a_write``).

    ...EXCEPT a segment satisfying the point-7 git carve-out, the point-7
    wrapper-family mirror, the point-9 grant-CLI carve-out, or the point-11
    interpreter-read-shape carve-out."""
    if not identifiers_lower or not _mentions_governed_identifier(cmd, identifiers_lower):
        return False

    segments = _split_top_level_segments(cmd)
    quoted_heredoc_bodies = _quoted_heredoc_bodies(cmd)

    stripped_cmd = _strip_heredoc_bodies(cmd)
    stripped_segments = _split_top_level_segments(stripped_cmd)
    if _has_var_assignment_indirection(
        stripped_segments, identifiers_lower
    ) and _assignment_indirection_reaches_a_write(stripped_segments, identifiers_lower):
        return True

    if _has_stdin_program_var_write(cmd, identifiers_lower):
        return True

    if _has_xargs_pipe_indirection(segments, identifiers_lower):
        return True

    for segment in segments:
        if not _mentions_governed_identifier(segment, identifiers_lower):
            continue
        if _is_git_content_mutation(segment):
            return True
        if (
            _is_git_read_shape(segment)
            or _is_commit_wrapper_read_shape(segment)
            or _is_claude_md_grant_read_shape(segment)
            or _is_interpreter_read_shape(segment, identifiers_lower)
        ):
            continue
        if _has_write_marker_for_point3(segment, identifiers_lower):
            return True
        if not _lies_in_a_quoted_heredoc_body(
            segment, quoted_heredoc_bodies
        ) and _has_indirection_marker(segment):
            return True

    return False


def _looks_commit_shaped(cmd: str) -> bool:
    for segment in _split_top_level_segments(cmd):
        if _git_subcommand(segment) in ("commit", "add"):
            return True
        if _is_commit_wrapper_command(segment):
            return True
    return False


def _looks_quoted_content_shaped(cmd: str, identifiers_lower: Tuple[str, ...]) -> bool:
    mentioning_segments = [
        segment
        for segment in _split_top_level_segments(cmd)
        if _mentions_governed_identifier(segment, identifiers_lower)
    ]
    if not mentioning_segments:
        return False
    for segment in mentioning_segments:
        target = _redirect_target_token(segment)
        if target and _mentions_governed_identifier(target, identifiers_lower):
            return False
    return all(
        not _mentions_governed_identifier(_strip_quoted_spans(segment), identifiers_lower)
        for segment in mentioning_segments
    )


def _compose_deny_message(
    *,
    commit_shaped: bool = False,
    quoted_content_shaped: bool = False,
    resolve_wiki_citation: Optional[Callable[[str], str]] = None,
) -> str:
    """Construction-verified equivalent of DoE's ``_compose_deny_message`` +
    ``render()`` pipeline: same three prose shapes, a trailing wiki pointer.
    See module docstring "GOVERNED IDENTIFIER SOURCE" note on ``_WIKI_ANCHOR``
    for why this module carries no resolution machinery of its own.

    ``resolve_wiki_citation``, when given, is called on ``_WIKI_ANCHOR`` HERE
    -- on the deny path only, since this function is only ever reached once
    ``check()`` has already decided to deny -- never on the allow path. The
    caller (``dispatch.py``'s ``resolve_doctrine_surface_wiki_citation``,
    threaded down from ``check()``'s own ``resolve_wiki_citation`` parameter)
    owns the resolution logic and its own plugin-root-derived fail-open
    behaviour; this module only invokes what it is handed, mirroring the
    ``governed_surfaces`` parameter's own caller-resolves shape. ``None``
    (the default -- no resolver supplied, or the caller's own resolution
    missed) leaves ``_WIKI_ANCHOR`` untouched, the pre-existing behaviour."""
    citation = resolve_wiki_citation(_WIKI_ANCHOR) if resolve_wiki_citation else _WIKI_ANCHOR
    if commit_shaped:
        prose = (
            "BLOCKED: this looks commit-shaped, but a write marker sits "
            "outside the message. Use the pathspec form, not Write/Edit."
        )
    elif quoted_content_shaped:
        prose = (
            "BLOCKED: the governed name is quoted content, not a write "
            "target. Edit the real destination; see "
            "findings-self-persist-sentinel.md."
        )
    else:
        prose = (
            "BLOCKED: this Bash command writes a governed doctrine "
            "surface. If the real target is one of the four files, use "
            "Write or Edit."
        )
    return f"{prose}\n\nSee {citation}."


def check(
    payload: Dict[str, Any],
    governed_surfaces: Optional[List[str]],
    resolve_wiki_citation: Optional[Callable[[str], str]] = None,
) -> Optional[Dict[str, Any]]:
    """Evaluate the doctrine-surface Bash/PowerShell write gate against a
    PreToolUse payload. Returns ``None`` (allow) or the nested hard-deny
    envelope. Never identity-gated -- fires for every caller.

    ``governed_surfaces`` is resolved by the CALLER (``dispatch.py``'s own
    ``resolve_governed_authoring_surfaces``/``resolve_plugin_root_loud``),
    never imported here -- this module has no dependency on ``dispatch.py``
    or any plugin-root resolution machinery, avoiding a circular import.
    ``None`` or an empty list fails OPEN: this guard has nothing to key its
    detection on, mirroring the resolver's own fail-open contract on a
    manifest miss.

    ``resolve_wiki_citation``, same caller-resolves shape as
    ``governed_surfaces``: an optional ``str -> str`` callable
    ``dispatch.py`` supplies (``resolve_doctrine_surface_wiki_citation``,
    bound to that call's own resolved ``plugin_root``), threaded down to
    ``_compose_deny_message`` and invoked ONLY there -- i.e. only once this
    function has already decided to deny, never on the allow path. ``None``
    (no resolver, or the caller's own resolution missed) leaves the deny
    message's trailing citation as the bare literal, unchanged from before
    this parameter existed.

    Deliberately no try/except here -- fail-CLOSED-on-exception is the
    dispatcher's own job for hard-deny guards.
    """
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    if not governed_surfaces:
        return None

    identifiers_lower = _governed_identifiers_lower(governed_surfaces)

    if not is_denied_bash_write(cmd, identifiers_lower):
        return None

    message = _compose_deny_message(
        commit_shaped=_looks_commit_shaped(cmd),
        quoted_content_shaped=_looks_quoted_content_shaped(cmd, identifiers_lower),
        resolve_wiki_citation=resolve_wiki_citation,
    )

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }
