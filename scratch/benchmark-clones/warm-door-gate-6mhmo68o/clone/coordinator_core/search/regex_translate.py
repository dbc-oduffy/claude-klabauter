"""coordinator_core.search.regex_translate -- POSIX BRE/ERE/fixed-string
pattern translator, producing a Python `re` SOURCE STRING that is
provably equivalent to the input under the given `grep` dialect, or
`None` when no faithful translation exists.

Why this exists: today the bash-spawn `grep`-via-Bash rewrite
(`coordinator_core.bash_guards.dispatch_checks._bt_grep_translate_pattern`)
only accepts patterns that ALREADY mean the same thing under both the
declared dialect and Python `re` unchanged -- it refuses every pattern
that needs an actual translation, because no faithful translator existed.
Measured against a 62,487-call bash-spawn corpus, that refusal fired on
1,707 real commands -- the single largest blocker to routing `grep` calls
through Python `re` instead of a forked child process. This module is
that missing translator.

`translate(pattern, dialect)` never guesses: `None` always means "refuse
to translate this pattern," never "best-effort, might be wrong." A wrong
translation that LOOKS right is worse than a refusal, because the caller
has no signal telling it to double check.

THE CENTRAL DIFFICULTY -- BRE operator/literal polarity is the exact
INVERSE of Python `re`:

  - In POSIX BRE, `\\| \\+ \\? \\{n,m\\} \\( \\)` are OPERATORS (GNU
    extensions for the first three) and the BARE characters
    `| + ? { } ( )` are ORDINARY LITERALS.
  - In Python `re` (and POSIX ERE), it is the reverse: bare
    `| + ? { } ( )` are operators, and a literal occurrence needs an
    escape.

Silently compiling a BRE pattern as Python `re` unchanged is the exact
mistake this module exists to never make again -- it was hit live:
`grep -n "^| AC-3 \\|^| AC-4 " <file>` (BRE alternation on six lines) was
compiled unchanged and matched every line of a 4,000-line file instead of
six (recorded in `dispatch_checks.py`'s own `_bt_grep_flags_and_operands`
negative-spec, which this module's docstring intentionally echoes rather
than merely cross-references -- the failure mode is the reason this
module exists, not incidental context).

Positional rules this module implements (verified against real `grep` by
this module's own differential test suite, not merely asserted here):

  - `*` is an operator EXCEPT as the first character of the pattern, or
    immediately after `\\(`/`(` (BRE/ERE group-open) or `\\|`/`|`
    (alternation) -- there it is a literal `*`.
  - `^` is an anchor ONLY at pattern start (or immediately after a group-
    open/alternation); a literal character everywhere else.
  - `$` is an anchor ONLY at pattern end (or immediately before a group-
    close/alternation); a literal character everywhere else.
  - Bracket expressions (`[...]`): backslash is an ORDINARY MEMBER, not
    an escape; a `]` as the very first member (after an optional leading
    `^`) is a literal `]`; a leading or trailing `-` is a literal `-`.
    POSIX classes (`[:alpha:]`, `[:digit:]`, ...) ARE translated. POSIX
    collating symbols (`[.x.]`) and equivalence classes (`[=x=]`) are
    NOT translatable -- refused.
  - Backreferences `\\1`-`\\9` (BRE) / `\\1`-`\\9` (ERE, GNU extension) map
    to Python `\\1`-`\\9` unchanged.
  - `fixed` dialect: `re.escape(pattern)`, unconditionally -- there are
    no metacharacters to misinterpret when the whole pattern is literal.

Negative-spec -- what this module deliberately does NOT translate (each
one refused outright, `translate()` returns `None`):
  - GNU word-boundary extensions `\\<` `\\>` `\\b` -- dialect-specific
    and not POSIX; guessing at their exact GNU semantics risks the same
    silent-wrong-answer class this module exists to prevent.
  - Perl/PCRE shorthands `\\d` `\\w` `\\s` `\\D` `\\W` `\\S` -- these ARE
    metacharacters in Python `re` but are NOT POSIX BRE/ERE (GNU grep
    without `-P` treats `\\d`/`\\w` as either literal or undefined,
    never as Python's character classes) -- translating them unchanged
    would silently change what the pattern matches.
  - POSIX collating symbols `[.x.]` and equivalence classes `[=x=]` --
    no Python `re` equivalent exists.
  - A malformed `\\{n,m\\}`/`{n,m}` interval (non-digit bounds, `m < n`,
    unterminated) -- refused rather than guessed at.
  - Any backslash-escape not in this module's own known-safe table for
    the dialect in play -- refuse, don't guess at GNU-specific meaning.
  - A pattern with an unbalanced group -- a `\\)`/`)` with no matching
    `\\(`/`(`, or one left open at pattern end -- refused rather than
    handed to `re.compile` to raise on (real corpus patterns hit this
    from truncated/misquoted shell extraction, not hand-written test
    cases; `translate()` must never depend on its caller having compiled
    the result to discover the pattern was malformed).
  - A quantifier (`+`/`?`/an interval) with no preceding atom -- pattern
    start, or immediately after a group-open/alternation. This is
    DISTINCT from a leading bare `*`, which real grep treats as a
    literal `*` in that position (this module translates it as such).
    `+`/`?`/an interval in that same position is refused instead of
    guessed at -- translating it unchanged either produces an invalid
    Python `re` source (`re.compile` raises "nothing to repeat"), or,
    for the specific ERE shape `(?...`, produces a VALID Python source
    that means something else entirely (Python's own lookahead/
    lookbehind/named-group syntax) -- silently reinterpreting the
    operator rather than erroring on it, exactly the class of mistake
    this whole module exists to never make.
  - A quantifier stacked directly on another quantifier -- e.g. BRE
    `\\+\\?` or ERE `++` -- refused for the same "no preceding atom"
    reason as above (the preceding token is itself an operator, not an
    atom to repeat), and doubly so because translating it unchanged
    risks SILENT REINTERPRETATION rather than a compile error: BRE
    `\\+\\?` emits Python `+?`, which Python parses as the LAZY quantifier
    (a real, different construct) rather than "quantify a quantifier",
    and ERE `++` emits Python `++`, which Python 3.11+ parses as the
    POSSESSIVE quantifier. Both are exactly the silent-reinterpretation
    class this module exists to prevent, so this is refused rather than
    passed through.
  - An unbounded quantifier (`*`, `+`, or an open-ended `{n,}`) applied
    to a GROUP whose own top-level content (not content nested inside a
    further sub-group) already contains an unbounded quantifier -- e.g.
    BRE `\\(a\\+\\)\\+` / ERE `(a+)+`. This is refused not because the
    translation would be wrong (it wouldn't -- Python `re` computes an
    identical MATCH SET for this shape) but because it is refused for
    AVAILABILITY, not fidelity: `(a+)+` is the textbook catastrophic-
    backtracking construction for a naive backtracking engine, and
    Python's `re` is a backtracking engine while POSIX/GNU grep's engine
    is not vulnerable to this shape the same way. This module is invoked
    inside a PreToolUse hook gating every Bash tool call with no
    execution timeout available (Python's `re` has no timeout parameter,
    and the usual POSIX `SIGALRM` backstop is not available on Windows,
    which is a first-class host here) -- so a pathological match here
    would stall every subsequent Bash call in the session, not just
    mis-answer one search. Refusing is always safe: the caller falls
    through to a real, non-vulnerable `grep` spawn.

THE ANCHOR-POSITION SPLIT, AND WHY IT REFUSES RATHER THAN PICKS
---------------------------------------------------------------
GNU grep treats `^`/`$` adjacent to an alternation or group boundary
(`^` right after `\\(`/`\\|`, `$` right before `\\)`/`\\|`) as ANCHORS; POSIX
and BSD grep treat them as LITERALS. The two select different lines from
the same file, and there is no translation that is faithful to both.

An earlier revision of this module resolved that by implementing GNU's
reading, on the theory that GNU-on-Linux is the target. The differential
harness then caught it live on a corpus-derived pattern --
`\\.cmd$\\|\\.md$\\|\\.ps1$\\|__pycache__` -- where real grep matched nothing
and the translation matched a line. That resolution was wrong for this
fleet regardless of which oracle is "correct": macOS and Windows are
first-class hosts here, the engine runs on whichever host the operator is
on, and the host's grep dialect is not knowable without spawning a grep --
which is precisely the cost this package exists to remove.

So this module REFUSES any pattern whose meaning depends on that split,
in both dialects, and translates every unambiguous position normally
(`^` at true pattern start and `$` at true pattern end are anchors
everywhere; `^`/`$` elsewhere are literals everywhere). Coverage lost to
the refusal is small and the loss is bounded; a wrong line selection is
neither.

KNOWN DIVERGENCE, deliberate: real grep rejects a bounded repetition
above 255 (`.\\{0,300\\}`) with `maximum repetition exceeds 255` and exits
2; this translator accepts it and returns matches. That is a capability
gain rather than a wrong answer -- no caller depends on grep's interval
ceiling as a feature -- but it IS an observable behaviour difference, so
it is declared here rather than left for a reader to discover from a
diff. 16 such patterns appear in the measured corpus.

KNOWN DIVERGENCE, deliberate: POSIX character classes (`[:alpha:]`,
`[:upper:]`, `[:lower:]`, `[:alnum:]`, `[:punct:]`, `[:print:]`,
`[:graph:]`) translate to hardcoded ASCII/C-locale ranges (e.g.
`[:alpha:]` -> `a-zA-Z`), never re-derived from the caller's actual
locale. Real GNU/BSD `grep` is locale-aware: under a non-`C`/`POSIX`
locale (`en_US.UTF-8`, the default on essentially every developer
macOS/Linux desktop), `[[:alpha:]]` also matches accented and other
multibyte letters that these hardcoded ASCII ranges will not -- a
genuine silent-divergence case, but only for non-ASCII input under a
non-C locale. This module keeps the ASCII translation deliberately
rather than refusing the locale-sensitive classes outside `C`/`POSIX`:
refusing would disable `[[:alpha:]]` and its siblings on essentially
every developer machine, trading a rare wrong answer (non-ASCII input
only) for a near-total capability loss on a common, everyday construct
-- exactly the "safe but incapable" outcome this package's own threat
model names as the thing to avoid alongside "wrong answer". Declared
here, same treatment as the interval-255 divergence above, so a reader
has the signal rather than discovering it from a diff.

Spec backlink: `coordinator_core/bash_guards/dispatch_checks.py`
`_bt_grep_dialect` / `_bt_grep_pattern_safe_for_dialect` /
`_bt_grep_translate_pattern` (the narrower "already-safe-unchanged"
predicate this module supersedes with an actual translator).
"""

from __future__ import annotations

import re
import string
from typing import Dict, List, Optional, Tuple

#: The three dialects `translate()` accepts. Anything else is a caller bug,
#: not a "refuse" case -- `translate()` raises `ValueError` for an
#: unrecognized dialect rather than silently returning `None` for it (a
#: caller passing a typo'd dialect name should see a loud failure, not a
#: quiet "always refused").
_VALID_DIALECTS = frozenset({"basic", "extended", "fixed"})

#: Python `re` character-class metacharacters that must be escaped
#: whenever they appear as a literal MEMBER inside a translated `[...]`
#: class (POSIX bracket expressions have no escape mechanism of their
#: own -- backslash is an ordinary member -- so every literal member this
#: module emits must be made class-safe for Python's own syntax).
_CLASS_METACHARS = frozenset("\\]^-")


def _escape_class_member(c: str) -> str:
    """Escape one bracket-expression MEMBER for safe embedding in a
    Python `re` character class. Members outside `_CLASS_METACHARS` are
    passed through unchanged -- Python's character-class syntax has no
    other special characters (unlike the top-level pattern grammar)."""
    if c in _CLASS_METACHARS:
        return "\\" + c
    return c


def _build_posix_class_map() -> Dict[str, str]:
    """POSIX `[:name:]` -> Python character-class-body fragment (the
    text that goes INSIDE `[...]`, already escaped for that context).
    Built programmatically from `string.*` rather than hand-typed, so the
    `punct` fragment's escaping can't silently drift from
    `_escape_class_member`'s own rule."""
    punct_frag = "".join(_escape_class_member(c) for c in string.punctuation)
    return {
        "alpha": "a-zA-Z",
        "digit": "0-9",
        "alnum": "a-zA-Z0-9",
        "upper": "A-Z",
        "lower": "a-z",
        "space": " \\t\\n\\r\\f\\v",
        "blank": " \\t",
        "punct": punct_frag,
        "xdigit": "0-9A-Fa-f",
        "cntrl": "\\x00-\\x1f\\x7f",
        "print": "\\x20-\\x7e",
        "graph": "\\x21-\\x7e",
    }


_POSIX_CLASS_MAP = _build_posix_class_map()

#: Matches a POSIX bracket sub-token -- class (`[:name:]`), collating
#: symbol (`[.x.]`), or equivalence class (`[=x=]`) -- at a given offset.
_BRACKET_SUBTOKEN_RE = re.compile(r"\[([:.=])")


def _translate_bracket_expression(pattern: str, start: int) -> Optional[Tuple[str, int]]:
    """Translate one POSIX bracket expression (`pattern[start] == '['`)
    into a Python `re` character class. Returns `(python_class_source,
    index_just_past_the_closing_']')`, or `None` to refuse -- an
    unterminated bracket expression, or one containing a collating
    symbol/equivalence class (no Python `re` equivalent exists for
    either).

    Shared verbatim between BRE and ERE callers -- bracket-expression
    grammar does not differ between the two POSIX regex flavors.
    """
    n = len(pattern)
    j = start + 1
    if j >= n:
        return None
    negate = False
    if pattern[j] == "^":
        negate = True
        j += 1
    body: List[str] = []
    first_member = True
    while True:
        if j >= n:
            return None  # unterminated bracket expression
        c = pattern[j]
        if c == "]" and not first_member:
            j += 1
            break
        m = _BRACKET_SUBTOKEN_RE.match(pattern, j)
        if m:
            kind = m.group(1)
            close = kind + "]"
            end = pattern.find(close, j + 2)
            if end == -1:
                return None
            if kind != ":":
                return None  # collating symbol / equivalence class -- refuse
            name = pattern[j + 2 : end]
            frag = _POSIX_CLASS_MAP.get(name)
            if frag is None:
                return None
            body.append(frag)
            j = end + 2
            first_member = False
            continue
        # A '-' range needs at least one more member char and a
        # non-']' character on the far side to be a range rather than a
        # literal '-' (POSIX: '-' is literal only as first/last member;
        # mid-list it forms a range with its neighbors).
        if j + 2 < n and pattern[j + 1] == "-" and pattern[j + 2] != "]":
            lo, hi = c, pattern[j + 2]
            body.append(_escape_class_member(lo) + "-" + _escape_class_member(hi))
            j += 3
            first_member = False
            continue
        body.append(_escape_class_member(c))
        j += 1
        first_member = False
    return "[" + ("^" if negate else "") + "".join(body) + "]", j


#: `\{n,m\}` (BRE) / `{n,m}` (ERE, unescaped) interval-bound validator --
#: `{n}`, `{n,}`, `{n,m}`, all digit bounds. Python `re`'s own `{...}`
#: syntax accepts the identical three forms, so a validated interval
#: passes through UNCHANGED (no digit-by-digit re-emission needed).
_INTERVAL_RE = re.compile(r"^\{[0-9]+(,[0-9]*)?\}$")


def _valid_interval(spec: str) -> bool:
    """`spec` is the `{...}` substring INCLUDING braces. `True` only for
    a well-formed `{n}` / `{n,}` / `{n,m}` with `m >= n` when both
    bounds are present -- a bound-order violation is refused rather than
    handed to Python `re`, which would raise `re.error` at compile time
    for some malformed forms but silently accept an `m < n` interval
    that can never match (same "refuse rather than guess" posture as
    every other ambiguous construct in this module)."""
    if not _INTERVAL_RE.match(spec):
        return False
    inner = spec[1:-1]
    if "," in inner:
        lo_s, hi_s = inner.split(",", 1)
        if hi_s == "":
            return True
        return int(hi_s) >= int(lo_s)
    return True


def _interval_is_unbounded(spec: str) -> bool:
    """`spec` is a `{...}` substring already confirmed `_valid_interval`.
    `True` for an open-ended `{n,}` (no upper bound) -- the same
    unbounded-repetition class as bare `*`/`+` for Finding-1 nested-
    quantifier ReDoS-refusal purposes. `{n}` and `{n,m}` are bounded and
    return `False`."""
    inner = spec[1:-1]
    return "," in inner and inner.split(",", 1)[1] == ""


#: Backslash-escapes BRE recognizes as meaning "the literal character"
#: (never as an operator) -- `\.` `\*` `\^` `\$` `\[` `\]` `\\`.
_BRE_LITERAL_ESCAPES = frozenset(".*^$[]\\")

#: Backslash-escapes ERE recognizes as meaning "the literal character"
#: for its own (bare-operator) metacharacter set, plus the four BRE
#: shares with it (`. * ^ $ [ ] \`) unioned in by the caller.
_ERE_LITERAL_ESCAPES = frozenset(".*^$[]\\(){}|+?")

#: BRE-only escaped operators: `\( \) \| \+ \?` (group/alternation/GNU
#: quantifier extensions) map to their Python `re` bare-operator form.
_BRE_ESCAPED_OPERATORS = {
    "(": "(",
    ")": ")",
    "|": "|",
    "+": "+",
    "?": "?",
}

#: Bare characters that are ORDINARY LITERALS in BRE but OPERATORS in
#: Python `re` -- must be escaped on output. `{`/`}` are handled
#: separately (interval detection), not through this table.
_BRE_BARE_LITERALS = frozenset("+?()|")


def _translate_basic(pattern: str) -> Optional[str]:
    """Translate one POSIX BRE pattern. See module docstring for the
    positional rules this implements (`*`/`^`/`$` context-sensitivity,
    operator/literal polarity inversion), and for the two
    quantifier-stacking / nested-quantifier negative-spec items this
    function enforces via `last_was_quantifier` / `group_unbounded_stack`
    / `pending_group_unbounded` below."""
    out: List[str] = []
    i, n = 0, len(pattern)
    at_expr_start = True  # true at pattern start and right after \( or \|
    paren_depth = 0
    groups_opened = 0  # total groups opened so far -- backreference range check
    #: `group_unbounded_stack[-1]` is `True` once the CURRENTLY OPEN
    #: group's own top-level content has emitted an unbounded quantifier
    #: (bare `*`-as-operator, `\+`, or an open-ended `\{n,\}`). Consulted
    #: when that group closes, to detect `\(a\+\)\+`-shaped ReDoS
    #: constructs (module docstring negative-spec).
    group_unbounded_stack: List[bool] = []
    #: `True` immediately after emitting a group-close whose own
    #: top-level content was unbounded -- valid only for the single next
    #: token (any other atom/operator resets it to `False`).
    pending_group_unbounded = False
    #: `True` immediately after emitting a quantifier operator (`*`, `+`,
    #: `?`, or a closed `{n,m}` interval) -- used to refuse a quantifier
    #: stacked directly on another quantifier (module docstring
    #: negative-spec).
    last_was_quantifier = False
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                return None  # trailing lone backslash -- malformed
            nc = pattern[i + 1]
            if nc in ("+", "?") and (at_expr_start or last_was_quantifier):
                # A quantifier operator with no preceding atom to repeat
                # (pattern start, right after \(/\|, or -- stacked
                # directly on another quantifier, e.g. `a\+\?`) --
                # translating it unchanged produces either an invalid
                # Python `re` source ("nothing to repeat") or, for the
                # stacked case, a VALID Python source that means
                # something else entirely (`+?` is Python's LAZY
                # quantifier, not "quantify a quantifier") -- exactly the
                # silent-reinterpretation class this module exists to
                # prevent. Refusing outright is the safe choice either
                # way -- it never risks emitting a Python source that
                # compiles to something OTHER than what the operator
                # meant.
                return None
            if nc in _BRE_ESCAPED_OPERATORS:
                if nc == ")":
                    if paren_depth == 0:
                        return None  # unbalanced -- close with no open
                    paren_depth -= 1
                    had_unbounded = group_unbounded_stack.pop() if group_unbounded_stack else False
                    out.append(_BRE_ESCAPED_OPERATORS[nc])
                    at_expr_start = False
                    pending_group_unbounded = had_unbounded
                    last_was_quantifier = False
                    i += 2
                    continue
                if nc == "(":
                    paren_depth += 1
                    groups_opened += 1
                    group_unbounded_stack.append(False)
                    out.append(_BRE_ESCAPED_OPERATORS[nc])
                    at_expr_start = True
                    pending_group_unbounded = False
                    last_was_quantifier = False
                    i += 2
                    continue
                if nc == "|":
                    out.append(_BRE_ESCAPED_OPERATORS[nc])
                    at_expr_start = True
                    pending_group_unbounded = False
                    last_was_quantifier = False
                    i += 2
                    continue
                if nc == "+":
                    # Unbounded quantifier operator. Finding 1: refuse if
                    # it is directly quantifying a just-closed group whose
                    # own top-level content already had an unbounded
                    # quantifier -- the `(a+)+` ReDoS shape (see module
                    # docstring negative-spec).
                    if pending_group_unbounded:
                        return None
                    if group_unbounded_stack:
                        group_unbounded_stack[-1] = True
                    out.append(_BRE_ESCAPED_OPERATORS[nc])
                    at_expr_start = False
                    pending_group_unbounded = False
                    last_was_quantifier = True
                    i += 2
                    continue
                # nc == "?" -- bounded (0-or-1) quantifier; Finding 1 does
                # not apply (only an UNBOUNDED outer quantifier is a
                # ReDoS risk), but it still consumes the "preceding atom"
                # slot and counts as a quantifier for stacking purposes.
                out.append(_BRE_ESCAPED_OPERATORS[nc])
                at_expr_start = False
                pending_group_unbounded = False
                last_was_quantifier = True
                i += 2
                continue
            if nc == "{":
                if at_expr_start or last_was_quantifier:
                    return None  # interval quantifier with no atom / stacked quantifier
                close = pattern.find("\\}", i + 2)
                if close == -1:
                    return None
                spec = "{" + pattern[i + 2 : close] + "}"
                if not _valid_interval(spec):
                    return None
                unbounded = _interval_is_unbounded(spec)
                if unbounded and pending_group_unbounded:
                    return None  # Finding 1: `\(a\+\)\{2,\}`-shaped ReDoS
                if unbounded and group_unbounded_stack:
                    group_unbounded_stack[-1] = True
                out.append(spec)
                i = close + 2
                at_expr_start = False
                pending_group_unbounded = False
                last_was_quantifier = True
                continue
            if nc == "}":
                return None  # stray \} with no matching \{ -- malformed
            if nc in "123456789":
                if int(nc) > groups_opened:
                    return None  # Finding 3: backreference to a group never opened
                out.append("\\" + nc)
                i += 2
                at_expr_start = False
                pending_group_unbounded = False
                last_was_quantifier = False
                continue
            if nc in _BRE_LITERAL_ESCAPES:
                out.append(re.escape(nc))
                i += 2
                at_expr_start = False
                pending_group_unbounded = False
                last_was_quantifier = False
                continue
            return None  # unknown escape (\d \w \< \> \b ...) -- refuse
        if c == "[":
            translated = _translate_bracket_expression(pattern, i)
            if translated is None:
                return None
            frag, i = translated
            out.append(frag)
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            continue
        if c == "*":
            if at_expr_start:
                out.append("\\*")
                at_expr_start = False
                pending_group_unbounded = False
                last_was_quantifier = False
                i += 1
                continue
            # Operator use -- Finding 2 (stacked on a preceding quantifier)
            # and Finding 1 (unbounded-quantifying a just-closed group
            # whose own top-level content was already unbounded).
            if last_was_quantifier:
                return None
            if pending_group_unbounded:
                return None
            if group_unbounded_stack:
                group_unbounded_stack[-1] = True
            out.append("*")
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = True
            i += 1
            continue
        if c == "^":
            if at_expr_start and i != 0:
                # Anchor-vs-literal here is NOT portable: GNU grep treats `^` right
                # after `\(`/`\|` as an anchor, POSIX and BSD grep treat it as a
                # literal. We cannot be faithful to both from one translation, and the
                # host's grep is not knowable without spawning one -- which is the cost
                # this package exists to remove. Refuse instead of picking a dialect.
                return None
            if at_expr_start:
                out.append("^")
                at_expr_start = True  # GNU: '*' right after an anchoring
                # leading '^' is STILL literal -- a leading anchor does
                # not consume the "start of expression" position for
                # asterisk purposes (verified by this module's
                # differential suite: `grep '^*x'` matches literal
                # `*x` at line start, not "zero-or-more of nothing").
            else:
                out.append("\\^")
                at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        if c == "$":
            if i != n - 1 and pattern[i + 1 : i + 3] in ("\\)", "\\|"):
                # Same portability split as `^` above, and the one this module's own
                # differential harness caught live on
                # `\.cmd$\|\.md$\|\.ps1$\|__pycache__`: GNU reads each `$` as an anchor,
                # BSD as a literal, and the two select different lines. Refuse.
                return None
            out.append("$" if i == n - 1 else "\\$")
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        if c == ".":
            out.append(".")
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        if c in _BRE_BARE_LITERALS or c in "{}":
            out.append("\\" + c)
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        out.append(re.escape(c))
        at_expr_start = False
        pending_group_unbounded = False
        last_was_quantifier = False
        i += 1
    if paren_depth != 0:
        return None  # unbalanced -- open group(s) never closed
    return "".join(out)


#: ERE bare operators that map straight through to Python `re` unchanged
#: -- ERE and Python agree on bare-operator polarity (unlike BRE), so no
#: inversion is needed for this set.
_ERE_BARE_OPERATORS = frozenset("()|+?")


def _translate_extended(pattern: str) -> Optional[str]:
    """Translate one POSIX ERE pattern. ERE's bare-operator polarity
    already agrees with Python `re` for `( ) | + ?` -- the translation
    work here is narrower than BRE: interval detection for `{...}`,
    bracket-expression translation, and the same `*`/`^`/`$` positional
    rules BRE has (GNU ERE keeps them, it does not make `^`/`$`
    unconditional anchors -- verified by this module's differential
    suite, not merely assumed from POSIX text). See `_translate_basic`'s
    docstring for what `last_was_quantifier` / `group_unbounded_stack` /
    `pending_group_unbounded` guard against -- identical shape here."""
    out: List[str] = []
    i, n = 0, len(pattern)
    at_expr_start = True
    paren_depth = 0
    groups_opened = 0  # total groups opened so far -- backreference range check
    group_unbounded_stack: List[bool] = []
    pending_group_unbounded = False
    last_was_quantifier = False
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                return None
            nc = pattern[i + 1]
            if nc in _ERE_LITERAL_ESCAPES:
                out.append(re.escape(nc))
                i += 2
                at_expr_start = False
                pending_group_unbounded = False
                last_was_quantifier = False
                continue
            if nc in "123456789":
                if int(nc) > groups_opened:
                    return None  # Finding 3: backreference to a group never opened
                out.append("\\" + nc)  # GNU ERE backreference extension
                i += 2
                at_expr_start = False
                pending_group_unbounded = False
                last_was_quantifier = False
                continue
            return None  # \d \w \< \> \b and any other GNU/Perl shorthand
        if c == "[":
            translated = _translate_bracket_expression(pattern, i)
            if translated is None:
                return None
            frag, i = translated
            out.append(frag)
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            continue
        if c == "*":
            if at_expr_start:
                out.append("\\*")
                at_expr_start = False
                pending_group_unbounded = False
                last_was_quantifier = False
                i += 1
                continue
            # Operator use -- Finding 2 (stacked on a preceding quantifier)
            # and Finding 1 (unbounded-quantifying a just-closed group
            # whose own top-level content was already unbounded).
            if last_was_quantifier:
                return None
            if pending_group_unbounded:
                return None
            if group_unbounded_stack:
                group_unbounded_stack[-1] = True
            out.append("*")
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = True
            i += 1
            continue
        if c == "^":
            # Same non-portable anchor position as the BRE path -- refuse rather than
            # pick GNU's or BSD's reading. See the BRE `^`/`$` branches for the full
            # reasoning and the pattern that caught it.
            if at_expr_start and i != 0:
                return None
            if at_expr_start:
                out.append("^")
                at_expr_start = True
            else:
                out.append("\\^")
                at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        if c == "$":
            if i != n - 1 and pattern[i + 1] in ")|":
                return None
            out.append("$" if i == n - 1 else "\\$")
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        if c == ".":
            out.append(".")
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        if c in "+?":
            if at_expr_start or last_was_quantifier:
                # No preceding atom to quantify (pattern start, or right
                # after '(' / '|'), OR stacked directly on another
                # quantifier (e.g. `a++`, which Python 3.11+ parses as
                # the POSSESSIVE quantifier -- a silent reinterpretation,
                # not a compile error). Observed live on a real corpus
                # pattern, `state/subagent-share/(?!93c086f0)`: the local
                # `grep -E` (BSD/macOS) rejects it ("repetition-operator
                # operand invalid") rather than treating it as literal.
                # Translating it unchanged would hand Python `re` the
                # substring `(?!...)`, which Python parses as a NEGATIVE
                # LOOKAHEAD -- valid syntax with a completely different
                # meaning than the operator ever had. That reinterpretation
                # risk, not just the one observed grep disagreement, is
                # why this refuses unconditionally rather than trying to
                # special-case just the lookahead spelling.
                return None
            if c == "+" and pending_group_unbounded:
                # Finding 1: unbounded-quantifying a just-closed group
                # whose own top-level content was already unbounded --
                # the `(a+)+` ReDoS shape.
                return None
            if c == "+" and group_unbounded_stack:
                group_unbounded_stack[-1] = True
            out.append(c)
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = True
            i += 1
            continue
        if c == "(":
            paren_depth += 1
            groups_opened += 1
            group_unbounded_stack.append(False)
            out.append(c)
            at_expr_start = True
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        if c == ")":
            if paren_depth == 0:
                return None  # unbalanced -- close with no open
            paren_depth -= 1
            had_unbounded = group_unbounded_stack.pop() if group_unbounded_stack else False
            out.append(c)
            at_expr_start = False
            pending_group_unbounded = had_unbounded
            last_was_quantifier = False
            i += 1
            continue
        if c == "|":
            out.append(c)
            at_expr_start = True
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        if c == "{":
            close = pattern.find("}", i + 1)
            valid = close != -1 and _valid_interval(pattern[i : close + 1])
            if not valid:
                # Not a well-formed interval -- POSIX/GNU ERE treats a
                # bare '{' that doesn't open a valid interval as a
                # literal '{' REGARDLESS of position (Finding 5: this
                # must be checked before any at-expr-start refusal, since
                # a `{` that can never be an interval was never a
                # quantifier candidate in the first place).
                out.append("\\{")
                at_expr_start = False
                pending_group_unbounded = False
                last_was_quantifier = False
                i += 1
                continue
            if at_expr_start or last_was_quantifier:
                return None  # interval quantifier with no atom / stacked quantifier
            spec = pattern[i : close + 1]
            unbounded = _interval_is_unbounded(spec)
            if unbounded and pending_group_unbounded:
                return None  # Finding 1: `(a+){2,}`-shaped ReDoS
            if unbounded and group_unbounded_stack:
                group_unbounded_stack[-1] = True
            out.append(spec)
            i = close + 1
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = True
            continue
        if c == "}":
            out.append("\\}")
            at_expr_start = False
            pending_group_unbounded = False
            last_was_quantifier = False
            i += 1
            continue
        out.append(re.escape(c))
        at_expr_start = False
        pending_group_unbounded = False
        last_was_quantifier = False
        i += 1
    if paren_depth != 0:
        return None  # unbalanced -- open group(s) never closed
    return "".join(out)


def translate(pattern: str, dialect: str) -> Optional[str]:
    """Return a Python `re` SOURCE STRING equivalent to `pattern` under
    `dialect` (`'basic'` | `'extended'` | `'fixed'`), or `None` if no
    faithful translation exists. See the module docstring for the full
    rule set and negative-spec (what is deliberately never translated).

    Raises `ValueError` for a `dialect` outside the three recognized
    names -- a caller passing an unrecognized dialect has a bug of its
    own; that is not the same class of "no faithful translation" this
    function's `None` return communicates.
    """
    if dialect not in _VALID_DIALECTS:
        raise ValueError("unrecognized dialect: %r" % (dialect,))
    if dialect == "fixed":
        return re.escape(pattern)
    if dialect == "basic":
        return _translate_basic(pattern)
    return _translate_extended(pattern)
