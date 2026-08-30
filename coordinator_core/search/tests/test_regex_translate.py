"""coordinator_core.search.tests.test_regex_translate -- unit tests for the
pure `translate()` rules PLUS a differential harness that compiles every
non-refused translation and checks it agrees with a real `grep`/`egrep`
binary line-for-line over a small fixed text corpus.

The differential class is the module's actual quality gate (see
`regex_translate`'s own module docstring): a passing unit test only shows
the translator does what ITS OWN AUTHOR intended, not that the intended
behavior matches real grep. `TestDifferentialAgainstRealGrep` is skipped
(not failed) when no `grep` binary is on `$PATH`, so this file stays
collectible on a `grep`-less CI runner -- but the skip is loud (a named
`pytest.mark.skipif` reason), never a silent pass.

Corpus note: the case list below is a hand-picked adversarial set,
anchored on the ONE historically-hit real failure (`^| AC-3 \\|^| AC-4 `,
the BRE-alternation-compiled-as-Python-`re`-unchanged incident this whole
module exists to prevent), plus every rule the module docstring claims to
implement. It intentionally does NOT try to re-embed the full
several-hundred-pattern corpus run used to develop this module (real
commands pulled from `bash-corpus.jsonl` via
`_shape_classifier.classify_command` / `_command_tokenizer.
segments_from_tokens_with_pipe_flag`, per the dispatch brief) -- that
corpus is dev-machine-local scratch, not something a checked-in test
should depend on existing. See this module's own run-report sidecar for
the corpus-run numbers (patterns exercised, translate-rate, agreement
rate).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.search.regex_translate import translate
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_HAS_GREP = shutil.which("grep") is not None

#: Small fixed text corpus the differential harness runs every pattern
#: against. Deliberately includes the AC-3/AC-4-shaped alternation table,
#: leading/trailing anchors, bracket expressions, POSIX classes, and
#: interval-quantified runs -- one line built to exercise each rule this
#: module claims to implement.
_CORPUS_TEXT = """\
| AC-3 | first row of a table |
| AC-4 | second row of a table |
| AC-5 | third row, not matched by the AC-3/AC-4 alternation |
plain line with no metacharacters
line ending in dollar sign$
a line with a dollar $ in the middle
^caret-looking text at line start
a line with a caret ^ in the middle
*star-looking text at line start
foo*bar has a star in the middle
a(b)c has parens
a{2,3}c has braces
a+b has a literal plus in some dialects
a?b has a literal question mark in some dialects
pipe|looking|text
backslash \\\\ in the middle
[bracket] expression here
CamelCase Words123 and snake_case_words
UPPERCASE ONLY LINE
lowercase only line
0123456789 digits only
punctuation: !@#$%^&*()_+-=[]{}|;:'",.<>/?`~
back-reference test: abcabc
back-reference test: abcxyz
"""


def _write_corpus(tmp_path: Path) -> Path:
    p = tmp_path / "corpus.txt"
    p.write_text(_CORPUS_TEXT, encoding="utf-8")
    return p


def _real_grep_lines(pattern: str, dialect: str, path: Path) -> set:
    """Run the real grep as the differential oracle.

    The pattern goes in through ``-f <file>``, never as an argv word, and that is
    load-bearing on Windows rather than a style choice. ``subprocess`` has no argv
    array to hand a native process: it joins the list into ONE command line, and
    the MSYS runtime behind Git-for-Windows' ``grep.exe`` re-splits and
    glob/brace-expands it. ``a{2,3}c`` arrived as two words, grep read ``a3c`` as
    a filename, and the oracle failed with ``rc=2`` on the exact metacharacter
    cases this test exists to cover -- so the harness, not the translator, was
    what the assertion was measuring. ``-f`` is byte-exact and expansion-proof:
    the pattern never passes through a command line at all.
    """
    flag = {"basic": [], "extended": ["-E"], "fixed": ["-F"]}[dialect]
    pattern_file = path.parent / "pattern.txt"
    pattern_file.write_bytes(pattern.encode("utf-8") + b"\n")
    proc = subprocess.run(
        ["grep"] + flag + ["-n", "-f", str(pattern_file), "--", str(path)],
        capture_output=True,
        timeout=5,
    **no_console_creationflags(),
)
    assert proc.returncode in (0, 1), (
        "real grep itself errored on %r (dialect=%s): rc=%d stderr=%r -- "
        "this pattern cannot be used as a differential oracle case"
        % (pattern, dialect, proc.returncode, proc.stderr)
    )
    lines = set()
    for ln in proc.stdout.decode("utf-8", errors="replace").splitlines():
        idx = ln.find(":")
        if idx != -1:
            lines.add(int(ln[:idx]))
    return lines


def _python_lines(compiled: "re.Pattern", path: Path) -> set:
    lines = set()
    with path.open(encoding="utf-8") as fh:
        for i, ln in enumerate(fh, 1):
            if compiled.search(ln.rstrip("\n")):
                lines.add(i)
    return lines


#: (pattern, dialect) pairs this module's translator does NOT refuse --
#: every one of these must agree EXACTLY with real grep.
_TRANSLATABLE_CASES = [
    # The historical incident this module exists to prevent: BRE
    # The AC-3/AC-4 incident pattern is deliberately NOT in this table: its second
    # `^` sits in the GNU-vs-BSD ambiguous position, so it is refused rather than
    # translated. See test_ac3_ac4_incident_case_is_never_silently_mistranslated.
    # Bare operators are literal in BRE.
    (r"a+b", "basic"),
    (r"a?b", "basic"),
    (r"a(b)c", "basic"),
    (r"a{2,3}c", "basic"),
    (r"pipe|looking|text", "basic"),
    # Escaped operators are GNU-extension operators in BRE.
    (r"AC-3\|AC-4", "basic"),
    (r"ab\+", "basic"),
    (r"\(AC-3\)", "basic"),
    (r"[0-9]\{2,3\}", "basic"),
    # '*' literal only at expression start.
    (r"^\*star", "basic"),
    (r"foo\*bar", "basic"),
    # '^'/'$' anchor only at true start/end (or group/alt boundary).
    (r"^caret", "basic"),
    (r"a\^b", "basic"),  # literal caret mid-pattern
    (r"dollar sign$", "basic"),
    (r"a\$b", "basic"),  # literal dollar mid-pattern
    # Bracket expressions + POSIX classes.
    (r"[[:upper:]]* ONLY LINE", "basic"),
    (r"[[:digit:]]\{10\}", "basic"),
    (r"[]bracket[]", "basic"),  # ']' as first member is literal
    # Backreferences.
    (r"\(abc\)\1", "basic"),
    # ERE: bare operators are operators, same polarity as Python.
    (r"a(b)c", "extended"),
    (r"a{2,3}c", "extended"),
    (r"AC-3|AC-4", "extended"),
    (r"ab+", "extended"),
    (r"ab?", "extended"),
    (r"^\*star", "extended"),
    (r"foo\*bar", "extended"),
    (r"[[:lower:]]+ only line", "extended"),
    # Fixed strings: every metacharacter is literal.
    (r"a(b)c", "fixed"),
    (r"a.b*c?", "fixed"),
    # ERE: a bare '{' that can never be a valid interval is a literal '{'
    # even at expression start (Finding 5 -- interval-validity checked
    # before the "no preceding atom" refusal).
    (r"{abc}", "extended"),
]

#: (pattern, dialect) pairs this module MUST refuse (`translate()` ->
#: `None`) -- each one exercises a named negative-spec item.
_REFUSED_CASES = [
    (r"\d+", "basic"),  # Perl shorthand, not POSIX
    (r"\w+", "extended"),  # Perl shorthand, not POSIX
    (r"\<word\>", "basic"),  # GNU word-boundary extension
    (r"[[.ch.]]", "basic"),  # collating symbol (needs the OUTER bracket
    # too -- a bare `[.ch.]` alone is just an ordinary bracket
    # expression containing the literal members '.', 'c', 'h', '.', not
    # a collating symbol; POSIX collating-symbol syntax nests inside an
    # enclosing `[...]`)
    (r"[[=a=]]", "basic"),  # equivalence class (same double-bracket note)
    (r"a\{5,2\}", "basic"),  # m < n, malformed interval
    (r"a\{x,y\}", "basic"),  # non-digit interval bounds
    (r"\(unterminated", "basic"),  # unbalanced group
    (r"stray\)", "basic"),  # unbalanced group
    (r"(unterminated", "extended"),  # unbalanced group
    (r"stray)", "extended"),  # unbalanced group
    (r"^\+x", "basic"),  # quantifier with no preceding atom
    (r"(?!lookahead)", "extended"),  # '(' immediately followed by '?'
    ("a\\", "basic"),  # trailing lone backslash (not a raw string --
    # r"a\" is a Python syntax error, the trailing backslash escapes the
    # closing quote)
    # Finding 1: nested-quantifier ReDoS shape -- an unbounded quantifier
    # applied to a group whose own top-level content already has one.
    (r"\(a\+\)\+", "basic"),
    (r"(a+)+", "extended"),
    (r"\(a*\)*", "basic"),
    (r"(a*)*", "extended"),
    (r"\(a\+\)\{2,\}", "basic"),  # open-ended interval as the outer quantifier
    (r"(a+){2,}", "extended"),
    # Finding 2: a quantifier stacked directly on another quantifier --
    # silently reinterpreted (lazy / possessive) rather than refused
    # before this fix.
    (r"a\+\?", "basic"),
    (r"a++", "extended"),
    # Finding 3: backreference to a group number never opened.
    (r"\(a\)\9", "basic"),
    (r"(a)\9", "extended"),
    # Finding 6(d): the remaining Perl/PCRE shorthands -- only \d and \w
    # were exercised before this fix.
    (r"a\bc", "basic"),
    (r"a\sc", "basic"),
    (r"a\Dc", "extended"),
    (r"a\Wc", "extended"),
    (r"a\Sc", "extended"),
]


class TestPureTranslationRules:
    """Unit tests for `translate()` in isolation -- fast, no subprocess."""

    @pytest.mark.parametrize("pattern,dialect", _TRANSLATABLE_CASES)
    def test_translates_and_compiles(self, pattern, dialect):
        result = translate(pattern, dialect)
        assert result is not None, "expected a translation for %r/%s" % (pattern, dialect)
        re.compile(result)  # must always be valid Python re source

    @pytest.mark.parametrize("pattern,dialect", _REFUSED_CASES)
    def test_refuses(self, pattern, dialect):
        assert translate(pattern, dialect) is None

    def test_fixed_dialect_is_always_translatable(self):
        for p in ["a.b*c?", "[not-a-class]", "\\d+", "()|{}"]:
            result = translate(p, "fixed")
            assert result == re.escape(p)

    def test_unrecognized_dialect_raises(self):
        with pytest.raises(ValueError):
            translate("abc", "posix-nonsense")

    def test_bre_leading_star_is_literal(self):
        assert translate(r"*abc", "basic") == r"\*abc"

    def test_bre_star_after_group_open_is_literal(self):
        assert translate(r"\(*abc\)", "basic") == r"(\*abc)"

    def test_bre_star_after_alternation_is_literal(self):
        assert translate(r"x\|*abc", "basic") == r"x|\*abc"

    def test_ere_leading_star_is_literal(self):
        assert translate(r"*abc", "extended") == r"\*abc"

    def test_bre_dollar_before_close_group_is_refused(self):
        """GNU reads this `$` as an anchor, POSIX/BSD as a literal. Not translatable
        faithfully for both, and the host's grep is unknowable without spawning one."""
        assert translate(r"\(abc$\)", "basic") is None

    def test_bre_caret_after_group_open_is_refused(self):
        assert translate(r"\(^abc\)", "basic") is None

    def test_bre_dollar_at_true_end_is_still_an_anchor(self):
        """The refusal must not over-reach: unambiguous anchor positions still work."""
        assert translate(r"abc$", "basic") == r"abc$"

    def test_bre_dollar_mid_pattern_is_a_literal(self):
        assert translate(r"a$b", "basic") == r"a\$b"

    def test_posix_class_translation_inside_bracket(self):
        result = translate(r"[[:alpha:][:digit:]]", "basic")
        assert result is not None
        re.compile(result)
        assert re.compile(result).match("a")
        assert re.compile(result).match("5")
        assert not re.compile(result).match("!")

    def test_bracket_leading_caret_negates(self):
        result = translate(r"[^abc]", "basic")
        assert result == r"[^abc]"
        assert re.compile(result).match("d")
        assert not re.compile(result).match("a")

    def test_bracket_leading_dash_is_literal(self):
        result = translate(r"[-abc]", "basic")
        assert result is not None
        assert re.compile(result).match("-")

    def test_backreference_maps_unchanged(self):
        assert translate(r"\(a\)\1", "basic") == r"(a)\1"

    def test_backreference_within_group_count_is_fine(self):
        assert translate(r"\(a\)\(b\)\2", "basic") == r"(a)(b)\2"

    def test_backreference_exceeding_group_count_is_refused(self):
        """Finding 3: `\\9` with only one group opened -- real Python `re`
        would raise `re.error: invalid group reference 9` at COMPILE
        time; this module refuses at TRANSLATE time instead, per its own
        stated contract of never depending on the caller compiling the
        result to discover malformedness."""
        assert translate(r"\(a\)\9", "basic") is None
        assert translate(r"(a)\9", "extended") is None

    def test_nested_quantifier_redos_shape_is_refused(self):
        """Finding 1: `(a+)+` is the textbook catastrophic-backtracking
        construction for Python's backtracking `re` engine -- refused
        structurally (availability refusal) rather than translated, since
        no execution timeout is portably available (no `re` timeout
        parameter, and `SIGALRM` is POSIX-only -- this fleet treats
        Windows as a first-class host)."""
        assert translate(r"\(a\+\)\+", "basic") is None
        assert translate(r"(a+)+", "extended") is None
        assert translate(r"\(a*\)*", "basic") is None
        assert translate(r"(a*)*", "extended") is None

    def test_nested_quantifier_refusal_does_not_over_reach(self):
        """The refusal is scoped to an UNBOUNDED outer quantifier on a
        group with an unbounded top-level quantifier -- a bounded outer
        quantifier is not a ReDoS shape and must still translate."""
        assert translate(r"\(a\+\)\?", "basic") == r"(a+)?"
        assert translate(r"(a+)?", "extended") == r"(a+)?"

    def test_stacked_quantifier_is_refused_not_reinterpreted(self):
        """Finding 2: BRE `a\\+\\?` would silently emit Python's LAZY
        quantifier (`+?`), and ERE `a++` would silently emit Python
        3.11+'s POSSESSIVE quantifier (`++`) -- both are valid Python `re`
        syntax with a completely different meaning than "quantify a
        quantifier", exactly the silent-reinterpretation class this
        module exists to prevent."""
        assert translate(r"a\+\?", "basic") is None
        assert translate(r"a++", "extended") is None

    def test_ere_bare_brace_that_cannot_be_an_interval_is_literal(self):
        """Finding 5: interval-validity is checked BEFORE the
        "no preceding atom" refusal -- `{abc}` can never be a valid
        interval (non-digit bounds), so it is a literal brace regardless
        of position, matching real ERE."""
        assert translate(r"{abc}", "extended") == r"\{abc\}"

    def test_interval_above_255_is_accepted_not_refused(self):
        """KNOWN DIVERGENCE lock-in (module docstring): real grep rejects
        a bounded repetition above 255 with `maximum repetition exceeds
        255` and exits 2; this translator accepts it -- a capability
        gain, not a wrong answer, so it must never start being refused
        without that being a deliberate, documented change."""
        result = translate(r"a\{0,300\}", "basic")
        assert result == r"a{0,300}"
        re.compile(result)

    def test_posix_class_ascii_divergence_is_locked_in(self):
        """KNOWN DIVERGENCE lock-in (module docstring): POSIX classes
        translate to hardcoded ASCII/C-locale ranges, deliberately not
        locale-aware -- refusing them outside the C locale would disable
        `[[:alpha:]]` on essentially every developer machine (Finding 4).
        This pins the ASCII behavior so the divergence stays a documented
        choice rather than an accident."""
        result = translate(r"[[:alpha:]]+", "extended")
        assert result is not None
        compiled = re.compile(result)
        assert compiled.fullmatch("abcXYZ")
        assert not compiled.fullmatch("café")  # accented -- ASCII-only, by design


@pytest.mark.skipif(not _HAS_GREP, reason="no grep binary on PATH -- differential oracle unavailable")
class TestDifferentialAgainstRealGrep:
    """For every non-refused case, the compiled Python translation must
    agree EXACTLY (same matching line numbers) with real grep over the
    same fixed corpus. Any disagreement is a bug in the translator, per
    this module's own binding verification requirement -- a refusal is
    always acceptable, a disagreement never is."""

    @pytest.mark.parametrize("pattern,dialect", _TRANSLATABLE_CASES)
    def test_agrees_with_real_grep(self, pattern, dialect, tmp_path):
        corpus = _write_corpus(tmp_path)
        py_src = translate(pattern, dialect)
        assert py_src is not None
        compiled = re.compile(py_src)
        grep_lines = _real_grep_lines(pattern, dialect, corpus)
        py_lines = _python_lines(compiled, corpus)
        assert py_lines == grep_lines, (
            "translation of %r (dialect=%s) -> %r disagrees with real grep: "
            "grep matched lines %s, python matched lines %s"
            % (pattern, dialect, py_src, sorted(grep_lines), sorted(py_lines))
        )

    def test_ac3_ac4_incident_case_is_never_silently_mistranslated(self, tmp_path):
        """The historical failure this module exists to prevent:
        `grep -n "^| AC-3 \\|^| AC-4 " <file>` once compiled straight to Python
        `re` and matched every line of a 4,000-line file instead of six.

        Its second `^` sits immediately after `\\|`, which GNU grep reads as an
        anchor and POSIX/BSD grep reads as a literal -- the two select different
        lines, so no single translation is faithful to both. This module therefore
        REFUSES it. Refusal fully discharges the incident's lesson: the command
        falls through and the real grep runs, which is correct by construction.

        What must never happen is a confident wrong answer, so this asserts the
        refusal directly rather than asserting a particular line set -- pinning a
        line set here would re-encode a bet on one grep's dialect, which is the
        mistake that produced the incident in the first place.
        """
        assert translate(r"^| AC-3 \|^| AC-4 ", "basic") is None

    def test_unambiguous_anchors_still_translate(self, tmp_path):
        """The refusal is scoped to the ambiguous positions, not to anchors at large."""
        corpus = _write_corpus(tmp_path)
        py_src = translate(r"^| AC-3 ", "basic")
        assert py_src is not None
        assert _python_lines(re.compile(py_src), corpus) == _real_grep_lines(
            r"^| AC-3 ", "basic", corpus
        )
