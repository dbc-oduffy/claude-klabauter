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
    """POSIX-slash string form of a path for embedding in a bash
    command-line string -- the tokenizer under test parses commands as
    real bash/POSIX-sh syntax (backslash is an escape character), so a
    native Windows ``str(Path)`` (backslash-separated) embedded directly
    into a ``cmd`` string is not a realistic Bash-tool payload and
    silently corrupts the path once tokenized (see bb48ce7's identical
    fixture-realism finding on the write-bump test suite). Accepts a
    ``Path`` or a plain ``str``."""
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


def _payload_prefix() -> str:
    return dc._bt_python3_invocation() + " -c '"


def _run_rewrite(cmd: str) -> str:
    """Compile `cmd` via `check_grep_via_bash_rewrite`, execute the embedded
    script in-process, and return its stdout -- `None` (as a sentinel via
    pytest.skip is wrong here; return the Python `None` object) if no
    rewrite was offered (a dialect-unsafe pattern was correctly refused)."""
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
    """Execute the RAW command string through an actual shell -- must go
    through the shell (not an argv list), so backslash-escaping etc. is
    resolved by the shell exactly as it would be for the original command
    dispatch_checks tokenizes (an argv list bypasses the shell entirely and
    would hand grep a literal backslash the shell would otherwise strip,
    producing a false divergence that is a test-harness bug, not a
    production one).

    Explicitly routed through Git Bash on Windows: `dispatch_checks`
    tokenizes `cmd` as bash/POSIX-sh, where a backslash before an ordinary
    character strips it and passes the character on literally (e.g. `\\.`
    reaches `grep` as a bare `.`). Plain `subprocess.run(shell=True)` on
    Windows launches cmd.exe instead, which does NOT treat backslash as an
    escape character at all -- `\\.` reaches `grep` unchanged, a genuinely
    different argument than what a real Bash-tool invocation would produce,
    and a false divergence against the rewrite (which mirrors the bash
    tokenizer's own stripping)."""
    if platform.system() == "Windows":
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("git-bash not found on PATH")
        result = subprocess.run(
            [bash, "-c", cmd], capture_output=True, text=True, timeout=timeout
        , **no_console_creationflags())
    else:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout,
            **no_console_creationflags(),
        )
    return result.stdout


def _lines_of(txt: str):
    """Normalize `path:lineno:content` / `lineno:content` output (the
    rewrite always includes the path; real `grep` omits it for a
    single-file invocation) down to a comparable ``{(lineno, content)}``
    set."""
    out = set()
    for line in txt.splitlines():
        if not line:
            continue
        m = re.match(r"^(?:[^:]*:)?(\d+):(.*)$", line)
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


# ---------------------------------------------------------------------------
# Patterns that MUST be refused (dialect-ambiguous) -- no rewrite offered,
# never a silently-wrong translation.
# ---------------------------------------------------------------------------


class TestRefusesRatherThanGuesses:
    def test_founding_incident_pattern_refused(self, fixture_file):
        """The EXACT command shape that produced the incident."""
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
        """`[[:alpha:]]` has no Python `re` equivalent as a REGEX
        construct -- refused for every dialect that parses it as a regex
        (basic/extended/rust). `fgrep`/`grep -F` is deliberately excluded:
        under `-F` there IS no regex parsing at all, so the bracket-class
        TEXT is itself just a literal string to search for, and `re.escape`
        translates it faithfully (covered by `test_fixed_dialect_bracket_
        class_text_is_literal_not_refused`, below)."""
        for binary in ("grep", "egrep", "rg"):
            cmd = '%s -n "[[:alpha:]]" %s' % (binary, fixture_file)
            assert dc.check_grep_via_bash_rewrite(cmd) is None, binary

    def test_fixed_dialect_bracket_class_text_is_literal_not_refused(self, fixture_file):
        TestDifferentialExecutionMatchesRealBinary._assert_matches(
            "fgrep", "-n", "[[:alpha:]]", fixture_file
        )

    def test_fixed_flag_with_extended_flag_refused(self, fixture_file):
        """`-E -F` together: real grep lets the LAST one on the command
        line win; this rewrite has no ordering information from an
        unordered flag set, so it refuses rather than guesses which wins."""
        cmd = "grep -EFn a+b %s" % fixture_file
        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_perl_shorthand_escape_refused_in_every_dialect(self, fixture_file):
        # Single-quoted in the command TEXT (not merely an `r"..."` Python
        # literal) -- an UNQUOTED `\d` would have its backslash stripped by
        # the shell-mimicking tokenizer before the pattern operand is even
        # extracted (bare backslash-before-ordinary-char is a no-op escape
        # in POSIX shell quoting), silently turning this into "d+" and
        # testing the wrong thing entirely.
        for binary in ("grep", "egrep"):
            cmd = "%s -n '\\d+' %s" % (binary, fixture_file)
            assert dc.check_grep_via_bash_rewrite(cmd) is None, binary

    def test_mid_pattern_bare_caret_basic_dialect_refused(self, fixture_file):
        """Finding 5: `grep -n 'a^b'` (BRE: literal substring `a^b`) must be
        refused, not silently compiled as a Python `re` mid-string anchor
        that can never match."""
        cmd = "grep -n a^b %s" % fixture_file

        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_mid_pattern_bare_dollar_basic_dialect_refused(self, fixture_file):
        cmd = "grep -n 'a$b' %s" % fixture_file
        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_trailing_dollar_basic_dialect_still_safe(self, fixture_file):
        """`$` AT the pattern's own last character is a real anchor in BOTH
        BRE and Python `re` -- must still be offered, unlike the mid-pattern
        case above."""
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


# ---------------------------------------------------------------------------
# Patterns that MUST be offered, and MUST match the real binary's output
# byte-for-byte (line/content set) -- differential execution, not reading.
# ---------------------------------------------------------------------------


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

    #: state/bash-guards/known-red.json group "dispatch-checks-windows-path"
    #: (check_grep_via_bash_rewrite's os.path.join defect). Owner:
    #: docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md.
    @pytest.mark.pending_fix
    def test_dot_metachar_basic(self, fixture_file):
        self._assert_matches("grep", "-n", ".", fixture_file)

    @pytest.mark.pending_fix
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

    @pytest.mark.pending_fix
    def test_grep_dash_capital_f_fixed(self, fixture_file):
        self._assert_matches("grep", "-Fn", "a+b", fixture_file)

    def test_rg_default_dialect_is_extended_like(self, fixture_file):
        self._assert_matches("rg", "-n", "a+b", fixture_file)

    @pytest.mark.pending_fix
    def test_anchors_basic_dialect_safe(self, fixture_file):
        self._assert_matches("grep", "-n", "^plain", fixture_file)


# ---------------------------------------------------------------------------
# `-e`/`-f` (lowercase) are real grep flags with unrelated argument-taking
# meanings ("-e PATTERN", "-f FILE") -- must never be folded into the
# dialect-flag vocabulary alongside `-E`/`-F`.
# ---------------------------------------------------------------------------


class TestLowercaseEfNotDialectFlags:
    def test_lowercase_e_pattern_flag_not_recognized(self, fixture_file):
        cmd = "grep -e a+b %s" % fixture_file
        assert dc.check_grep_via_bash_rewrite(cmd) is None

    def test_lowercase_f_file_flag_not_recognized(self, tmp_path):
        pat_file = tmp_path / "patterns.txt"
        pat_file.write_text("TODO\n")
        cmd = "grep -f %s %s" % (pat_file, tmp_path / "corpus.txt")
        assert dc.check_grep_via_bash_rewrite(cmd) is None
