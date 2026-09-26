
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.search.regex_translate import translate
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_HAS_GREP = shutil.which("grep") is not None

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


_TRANSLATABLE_CASES = [
    (r"a+b", "basic"),
    (r"a?b", "basic"),
    (r"a(b)c", "basic"),
    (r"a{2,3}c", "basic"),
    (r"pipe|looking|text", "basic"),
    (r"AC-3\|AC-4", "basic"),
    (r"ab\+", "basic"),
    (r"\(AC-3\)", "basic"),
    (r"[0-9]\{2,3\}", "basic"),
    (r"^\*star", "basic"),
    (r"foo\*bar", "basic"),
    (r"^caret", "basic"),
    (r"a\^b", "basic"),
    (r"dollar sign$", "basic"),
    (r"a\$b", "basic"),
    (r"[[:upper:]]* ONLY LINE", "basic"),
    (r"[[:digit:]]\{10\}", "basic"),
    (r"[]bracket[]", "basic"),
    (r"\(abc\)\1", "basic"),
    (r"a(b)c", "extended"),
    (r"a{2,3}c", "extended"),
    (r"AC-3|AC-4", "extended"),
    (r"ab+", "extended"),
    (r"ab?", "extended"),
    (r"^\*star", "extended"),
    (r"foo\*bar", "extended"),
    (r"[[:lower:]]+ only line", "extended"),
    (r"a(b)c", "fixed"),
    (r"a.b*c?", "fixed"),
    (r"{abc}", "extended"),
]

_REFUSED_CASES = [
    (r"\d+", "basic"),
    (r"\w+", "extended"),
    (r"\<word\>", "basic"),
    (r"[[.ch.]]", "basic"),
    (r"[[=a=]]", "basic"),
    (r"a\{5,2\}", "basic"),
    (r"a\{x,y\}", "basic"),
    (r"\(unterminated", "basic"),
    (r"stray\)", "basic"),
    (r"(unterminated", "extended"),
    (r"stray)", "extended"),
    (r"^\+x", "basic"),
    (r"(?!lookahead)", "extended"),
    ("a\\", "basic"),
    (r"\(a\+\)\+", "basic"),
    (r"(a+)+", "extended"),
    (r"\(a*\)*", "basic"),
    (r"(a*)*", "extended"),
    (r"\(a\+\)\{2,\}", "basic"),
    (r"(a+){2,}", "extended"),
    (r"a\+\?", "basic"),
    (r"a++", "extended"),
    (r"\(a\)\9", "basic"),
    (r"(a)\9", "extended"),
    (r"a\bc", "basic"),
    (r"a\sc", "basic"),
    (r"a\Dc", "extended"),
    (r"a\Wc", "extended"),
    (r"a\Sc", "extended"),
]


class TestPureTranslationRules:

    @pytest.mark.parametrize("pattern,dialect", _TRANSLATABLE_CASES)
    def test_translates_and_compiles(self, pattern, dialect):
        result = translate(pattern, dialect)
        assert result is not None, "expected a translation for %r/%s" % (pattern, dialect)
        re.compile(result)

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
        assert translate(r"\(abc$\)", "basic") is None

    def test_bre_caret_after_group_open_is_refused(self):
        assert translate(r"\(^abc\)", "basic") is None

    def test_bre_dollar_at_true_end_is_still_an_anchor(self):
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
        assert not compiled.fullmatch("café")


@pytest.mark.skipif(not _HAS_GREP, reason="no grep binary on PATH -- differential oracle unavailable")
class TestDifferentialAgainstRealGrep:

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
        assert translate(r"^| AC-3 \|^| AC-4 ", "basic") is None

    def test_unambiguous_anchors_still_translate(self, tmp_path):
        corpus = _write_corpus(tmp_path)
        py_src = translate(r"^| AC-3 ", "basic")
        assert py_src is not None
        assert _python_lines(re.compile(py_src), corpus) == _real_grep_lines(
            r"^| AC-3 ", "basic", corpus
        )
