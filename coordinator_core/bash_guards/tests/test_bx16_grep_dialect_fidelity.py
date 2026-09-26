"""Regression coverage for the BX-16 grep-dialect-conflation defect in
``coordinator_core.bash_guards.dispatch_checks`` (``check_grep_via_bash_rewrite``
/ ``_bt_build_generator_lines``'s ``"grep"`` kind, both fed by the single
choke point ``_bt_grep_flags_and_operands``).

The defect: the rewrite compiled a grep-family PATTERN as Python `re`
unconditionally, regardless of which regex dialect actually produced it --
POSIX BRE (bare `grep`), POSIX ERE (`egrep`/`grep -E`), fixed-string
(`fgrep`/`grep -F`), or Rust regex (`rg`'s own default). Those dialects
disagree with Python `re` (and with each other) on what a bare vs. an
escaped `| + ? { } ( )` means, so passing the pattern through unchanged
produced a SILENTLY WRONG rewrite, not an error -- the worst failure shape
for an auto-rewrite seam, because nothing signals the miscompile.

Hit live: `grep -n "^| AC-3 \\|^| AC-4 \\|^| AC-5 " <file>` -- BRE
alternation (`\\|` is the operator, bare `|` is literal) compiled under
Python `re`'s OPPOSITE convention (bare `|` is the operator) and matched
every line of a 4000-line file instead of six.

Verified here by DIFFERENTIAL EXECUTION: the same pattern is run through a
real dialect binary (skipped if absent from PATH) and through the generated
rewrite's embedded Python source (executed via `exec`, not a subprocess --
mirrors `test_bx16_multiprobe_and_headtail_rewrite.py`'s own `_run_python_c`
isolation rationale), and the two outputs are compared line-for-line.

Spec backlink: docs/plans/2026-07-29-windows-viability-stop-the-spawn-
storms.md row BX-16 (DoE-claude); this file's own coverage is the grep-
dialect-fidelity counterpart to that plan's apostrophe-quote-safety fix
(`test_bx16_apostrophe_quote_safety.py`) -- a different correctness axis on
the same rewrite seam.
"""
from __future__ import annotations

import io
import platform
import re
import shutil
import subprocess
from contextlib import redirect_stdout

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _posix(p) -> str:
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


def _payload_prefix() -> str:
    return dc._bt_python3_invocation() + " -c '"


def _run_rewrite(cmd: str) -> str:
    out = dc.check_grep_via_bash_rewrite(cmd)
    if out is None:
        return None
    command = out["hookSpecificOutput"]["updatedInput"]["command"]
    prefix = _payload_prefix()
    assert command.startswith(prefix) and command.endswith("'")
    script = command[len(prefix) : -1]
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(script, "<bx16-grep-rewrite>", "exec"), {})
    return buf.getvalue()


def _run_real(cmd: str, timeout: float = 10.0) -> str:
    if platform.system() == "Windows":
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("git-bash not found on PATH")
        result = subprocess.run(
            [bash, "-c", cmd], capture_output=True, text=True, timeout=timeout
        , **no_console_creationflags())
    else:
        result = subprocess.run(
            # intermediary that CREATE_NO_WINDOW does not suppress; the
            # STARTUPINFO route is a separate, wider fix (review: code-reviewer).
            cmd, shell=True, capture_output=True, text=True, timeout=timeout,
        )
    return result.stdout


_BARE_LINE_RE = re.compile(r"^(\d+):(.*)$")
_PATH_LINE_RE = re.compile(r"^.*?:(\d+):(.*)$")


def _lines_of(txt: str):
    out = set()
    for line in txt.splitlines():
        if not line:
            continue
        m = _BARE_LINE_RE.match(line) or _PATH_LINE_RE.match(line)
        out.add((m.group(1), m.group(2)) if m else line)
    return out


@pytest.fixture()
def fixture_file(tmp_path):
    p = tmp_path / "corpus.txt"
    p.write_text(
        "| AC-3 pass\n"
        "| AC-4 pass\n"
        "| AC-5 pass\n"
        "| AC-6 nope\n"
        "plain line\n"
        "a+b\n"
        "a?b\n"
        "a{2}b\n"
        "a(b)c\n"
        "[[:alpha:]] class here\n"
        "literal.dot.here\n"
        "literal*star*here\n"
        "END\n"
    )
    return p


class TestRefusesRatherThanGuesses:
    def test_founding_incident_pattern_refused(self, fixture_file):
        cmd = 'grep -n "^| AC-3 \\|^| AC-4 \\|^| AC-5 " %s' % fixture_file
        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_bare_pipe_basic_dialect_refused(self, fixture_file):
        assert dc.check_grep_via_bash_rewrite("grep -n a|b %s" % fixture_file) is None

    def test_escaped_plus_basic_dialect_refused(self, fixture_file):
        cmd = r"grep -n a\+b %s" % fixture_file
        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_bare_plus_basic_dialect_refused(self, fixture_file):
        assert dc.check_grep_via_bash_rewrite("grep -n a+b %s" % fixture_file) is None

    def test_escaped_braces_basic_dialect_refused(self, fixture_file):
        cmd = r"grep -n a\{2\}b %s" % fixture_file
        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_bare_parens_basic_dialect_refused(self, fixture_file):
        assert dc.check_grep_via_bash_rewrite("grep -n a(b)c %s" % fixture_file) is None

    def test_posix_bracket_class_refused_regex_dialects(self, fixture_file):
        for binary in ("grep", "egrep", "rg"):
            cmd = '%s -n "[[:alpha:]]" %s' % (binary, fixture_file)
            assert dc.check_grep_via_bash_rewrite(cmd) is None, binary

    def test_fixed_dialect_bracket_class_text_is_literal_not_refused(self, fixture_file):
        TestDifferentialExecutionMatchesRealBinary._assert_matches(
            "fgrep", "-n", "[[:alpha:]]", fixture_file
        )

    def test_fixed_flag_with_extended_flag_refused(self, fixture_file):
        cmd = "grep -EFn a+b %s" % fixture_file
        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_perl_shorthand_escape_refused_in_every_dialect(self, fixture_file):
        # literal) -- an UNQUOTED `\d` would have its backslash stripped by
        for binary in ("grep", "egrep"):
            cmd = "%s -n '\\d+' %s" % (binary, fixture_file)
            assert dc.check_grep_via_bash_rewrite(cmd) is None, binary

    def test_mid_pattern_bare_caret_basic_dialect_refused(self, fixture_file):
        cmd = "grep -n a^b %s" % fixture_file

        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_mid_pattern_bare_dollar_basic_dialect_refused(self, fixture_file):
        cmd = "grep -n 'a$b' %s" % fixture_file
        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_trailing_dollar_basic_dialect_still_safe(self, fixture_file):
        assert dc.check_grep_via_bash_rewrite("grep -n plain$ %s" % fixture_file) is not None


class TestGrepDashWRefusedRatherThanSilentlyDropped:
    """Finding 4: `-w` (whole-word match) used to sit in the "recognized,
    translate it" flag set even though neither rewrite path ever wraps the
    translated pattern in a word-boundary -- `grep -w foo` would silently
    over-match (`foobar` too). Dropped from `_GREP_SUBSTITUTABLE_SHORT_FLAGS`
    so it now falls through to the advisory/refuse path like any other
    untranslated flag."""

    def test_dash_w_refuses_rewrite(self, fixture_file):
        assert dc.check_grep_via_bash_rewrite("grep -w plain %s" % fixture_file) is None

    def test_dash_w_combined_short_flags_refuses_rewrite(self, fixture_file):
        assert dc.check_grep_via_bash_rewrite("grep -wrn plain %s" % fixture_file) is None


class TestDifferentialExecutionMatchesRealBinary:
    @staticmethod
    def _assert_matches(binary, flags, pattern, fixture_file):
        if shutil.which(binary) is None:
            pytest.skip("%s not on PATH" % binary)
        cmd = "%s %s %s %s" % (binary, flags, pattern, _posix(fixture_file))
        real = _run_real(cmd)
        rewritten = _run_rewrite(cmd)
        assert rewritten is not None, "expected a rewrite for: %s" % cmd
        assert _lines_of(real) == _lines_of(rewritten), (cmd, real, rewritten)

    #: sides was the instrument. See `_PATH_LINE_RE`.
    def test_dot_metachar_basic(self, fixture_file):
        self._assert_matches("grep", "-n", ".", fixture_file)

    def test_escaped_dot_basic(self, fixture_file):
        self._assert_matches("grep", "-n", r"\.", fixture_file)

    def test_extended_braces_quantifier(self, fixture_file):
        self._assert_matches("egrep", "-n", "a{2,3}b", fixture_file)

    def test_extended_escaped_braces_literal(self, fixture_file):
        self._assert_matches("grep", "-En", r"a\{2\}b", fixture_file)

    def test_extended_grouping(self, fixture_file):
        self._assert_matches("egrep", "-n", "a(b)c", fixture_file)

    def test_fixed_dot_is_literal(self, fixture_file):
        self._assert_matches("fgrep", "-n", "literal.dot.here", fixture_file)

    def test_fixed_star_is_literal(self, fixture_file):
        self._assert_matches("fgrep", "-n", "literal*star*here", fixture_file)

    def test_grep_dash_capital_f_fixed(self, fixture_file):
        self._assert_matches("grep", "-Fn", "a+b", fixture_file)

    def test_rg_default_dialect_is_extended_like(self, fixture_file):
        self._assert_matches("rg", "-n", "a+b", fixture_file)

    def test_anchors_basic_dialect_safe(self, fixture_file):
        self._assert_matches("grep", "-n", "^plain", fixture_file)


class TestLowercaseEfNotDialectFlags:
    def test_lowercase_e_pattern_flag_not_recognized(self, fixture_file):
        cmd = "grep -e a+b %s" % fixture_file
        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_lowercase_f_file_flag_not_recognized(self, tmp_path):
        pat_file = tmp_path / "patterns.txt"
        pat_file.write_text("TODO\n")
        cmd = "grep -f %s %s" % (pat_file, tmp_path / "corpus.txt")
        assert dc.check_grep_via_bash_rewrite(cmd) is None
