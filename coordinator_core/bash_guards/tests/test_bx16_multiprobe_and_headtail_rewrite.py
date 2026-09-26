"""Tests for the two BX-16 rewrite checks that close BX-7's and BX-8's
missing-rewrite-target gap (DoE docs/plans/2026-07-29-windows-viability-
stop-the-spawn-storms.md, row BX-16): ``check_multiprobe_banner_rewrite``
(MULTI_PROBE_BANNER, 40.1% of measured forks) and
``check_head_tail_plumbing_rewrite`` (HEAD_TAIL_PLUMBING, 25%).

Both checks share the two-tier BX-16 contract, never deny -- see
``dispatch_checks.py``'s BX-16 module comment. As of C4 (2026-08-01),
``check_multiprobe_banner_rewrite`` emits EITHER a concrete rewrite OR
nothing (``None``) for an unrecognized probe/stage -- the prior
prose-only-advisory fall-through was Axis A's exact failure mode and is
removed; ``check_head_tail_plumbing_rewrite`` still advises (out of this
chunk's scope). This file verifies output-equivalence for every
auto-rewrite, the silent fall-through on an unrecognized probe/stage for
the banner check, that a quoted separator token is never mis-split into a
false segment boundary, that a genuinely composed/piped command passes
through untouched (``None``), and that neither check can ever return a
``deny`` verdict for any input tried here.
"""
from __future__ import annotations

import io
import os
import platform
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards import guard_head_tail_rewrite as ht
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _posix(p) -> str:
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


def _payload_prefix() -> str:
    return dc._bt_python3_invocation() + " -c '"


def _run_python_c(command: str) -> str:
    prefix = _payload_prefix()
    assert command.startswith(prefix) and command.endswith("'")
    script = command[len(prefix) : -1]
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(script, "<bx16-rewrite>", "exec"), {})
    return buf.getvalue()


def _run_shell(command: str) -> str:
    if platform.system() == "Windows":
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("git-bash not found on PATH")
        result = subprocess.run(
            [bash, "-c", command],
            capture_output=True,
            text=True,
            timeout=10,
            **no_console_creationflags(),
        )
    else:
        result = subprocess.run(
            # intermediary that CREATE_NO_WINDOW does not suppress; the
            # STARTUPINFO route is a separate, wider fix (review: code-reviewer).
            command, shell=True, capture_output=True, text=True, timeout=10,
        )
    return result.stdout


_CLOCK_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")


def _mask_clock(text: str) -> str:
    return _CLOCK_RE.sub("HH:MM:SS", text)


#: ORDER and PUNCTUATION it prints them in. See `_date_facts`.
_WEEKDAY_RE = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b")
_MONTH_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b"
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _date_facts(text: str):
    """The calendar facts `date(1)` reports, as an order-free dict.

    WHY THIS EXISTS, and what it deliberately stops claiming. On a POSIX
    host the rewrite reproduces `date`'s output byte-for-byte and the
    callers below assert exactly that. On Windows it cannot, and no amount
    of work on the rewrite would change that: the shell in play is Git
    Bash, whose `date` is MSYS coreutils reading MSYS's own locale data
    (`Tue, Sep  1, 2026 13:01:24`), while the rewrite runs native CPython
    against the Windows CRT, which has no route to that rendering --
    measured 2026-09-01 across `setlocale(LC_TIME, "")`, `%c`, and `%x %X`,
    none of which reproduce it. `uname -a` is the same shape one layer
    down: MSYS reports the MINGW64 kernel string, `platform.uname()`
    reports the Windows host. Both sides are correct; they are rendered by
    different layers.

    So on Windows the assertion drops to the claim the rewrite's own
    docstring actually makes -- "reproducing the SAME facts" -- and this
    helper is what keeps that from becoming a rubber stamp. Weekday, month,
    day-of-month and year are each compared individually, so a rewrite that
    dropped the year or reported a stale day still fails here. The clock is
    reported as PRESENCE only, never as a value -- for the same reason
    `_mask_clock` exists above: the shell run and the rewritten run are two
    subprocesses that cannot be made to straddle the same tick. What is no
    longer asserted on Windows, explicitly: field order, separator
    punctuation, and the timezone rendering.
    """
    day = None
    m_month = _MONTH_RE.search(text)
    if m_month:
        after = text[m_month.end() :]
        m_day = re.match(r"[\s,]*(\d{1,2})\b", after)
        day = m_day.group(1) if m_day else None
    m_week = _WEEKDAY_RE.search(text)
    m_year = _YEAR_RE.search(text)
    m_clock = _CLOCK_RE.search(text)
    return {
        "weekday": m_week.group(1) if m_week else None,
        "month": m_month.group(1) if m_month else None,
        "day": day,
        "year": m_year.group(0) if m_year else None,
        "clock_present": m_clock is not None,
    }


class TestPython3InvocationImportErrorFallback:

    def test_import_error_falls_back_to_python3(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "coordinator_core.pyresolve", None)
        assert dc._bt_python3_invocation() == "python3"
    def test_never_denies_on_recognized_chain(self):
        cmd = 'echo "=== SESSION FACTS ==="; pwd; whoami; date; uname'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        assert "updatedInput" in out["hookSpecificOutput"]

    def test_unrecognized_probe_emits_nothing(self):
        cmd = 'echo "=== FACTS ==="; git log -1; pwd'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        assert out is None

    @pytest.mark.pending_fix
    def test_bare_facts_equivalence(self, tmp_path):
        cmd = 'echo "=== FACTS ==="; pwd; whoami'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]

        original = _run_shell(cmd)
        rewritten = _run_python_c(rewrite)
        assert original == rewritten

    def test_date_and_uname_a_equivalence(self):
        cmd = "date; uname -a"
        out = dc.check_multiprobe_banner_rewrite("echo \"=== X === Y ===\"; " + cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell("echo \"=== X === Y ===\"; " + cmd)
        rewritten = _run_python_c(rewrite)
        if platform.system() != "Windows":
            assert _mask_clock(original) == _mask_clock(rewritten)
            return
        assert _date_facts(original) == _date_facts(rewritten)
        assert _date_facts(rewritten)["year"] is not None
        assert _date_facts(rewritten)["clock_present"]
        node = platform.node()
        assert node and node in original and node in rewritten

    def test_git_facts_batched_into_one_git_invocation(self, tmp_path):
        cmd = (
            'echo "=== SESSION FACTS ==="; git rev-parse --abbrev-ref HEAD; '
            "git status --porcelain; pwd"
        )
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        script = rewrite[len(_payload_prefix()) : -1]
        assert script.count("subprocess.run([\"git\"") == 1

        branch_original = _run_shell("git rev-parse --abbrev-ref HEAD").strip()
        rewritten = _run_python_c(rewrite)
        lines = rewritten.splitlines()
        assert lines[1] == branch_original

    def test_quoted_banner_marker_not_mis_split(self):
        cmd = 'echo "=== a ; b ==="; pwd; whoami'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        assert "a ; b" in rewrite

    def test_piped_stage_inside_banner_emits_nothing(self):
        cmd = 'echo "=== FACTS ==="; pwd | cat; whoami'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        assert out is None

    def test_no_banner_shape_returns_none(self):
        assert dc.check_multiprobe_banner_rewrite("pwd; whoami") is None
        assert dc.check_multiprobe_banner_rewrite("") is None

    def test_override_disables_check(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_ALLOW_MULTIPROBE_BANNER", "1")
        cmd = 'echo "=== FACTS ==="; pwd; whoami; date'
        assert dc.check_multiprobe_banner_rewrite(cmd) is None

    def test_unparseable_command_returns_none(self):
        assert dc.check_multiprobe_banner_rewrite("echo 'unterminated") is None


def _run_python_c_with_faked_git_status(command: str, porcelain_lines) -> str:
    prefix = _payload_prefix()
    assert command.startswith(prefix) and command.endswith("'")
    script = command[len(prefix) : -1]

    class _FakeResult:
        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    class _FakeSubprocess:
        @staticmethod
        def run(*_a, **_k):
            return _FakeResult("\n".join(porcelain_lines))

    real_subprocess = sys.modules.get("subprocess")
    sys.modules["subprocess"] = _FakeSubprocess
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(compile(script, "<bx16-rewrite-faked-git>", "exec"), {})
    finally:
        if real_subprocess is not None:
            sys.modules["subprocess"] = real_subprocess
    return buf.getvalue()


class TestGitFactDetachedHeadAndUnbornBranchFidelity:
    """Finding 2 (P1): `# branch.head`/`# branch.oid`'s porcelain sentinel
    strings (`(detached)`, `(initial)`) used to be printed VERBATIM for
    whichever of `git rev-parse --abbrev-ref HEAD` / `git branch
    --show-current` / `git rev-parse HEAD` was asked -- matching NEITHER
    command's real output in that state. Fixed by tracking which of the two
    branch-probe forms a segment actually was (`_bt_git_probe_kind`'s new
    `form` return) and mapping each sentinel to that form's real behavior."""

    _cmd = (
        'echo "=== FACTS ==="; git rev-parse HEAD; '
        "git rev-parse --abbrev-ref HEAD; git branch --show-current; pwd"
    )

    def test_detached_head_sentinel_mapped_per_form(self):
        out = dc.check_multiprobe_banner_rewrite(self._cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite, ["# branch.oid abc123", "# branch.head (detached)"]
        )
        lines = result.splitlines()
        assert lines[1] == "abc123"
        assert lines[2] == "HEAD"
        assert lines[3] == ""

    def test_unborn_branch_sentinel_mapped_per_form(self):
        out = dc.check_multiprobe_banner_rewrite(self._cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite, ["# branch.oid (initial)", "# branch.head master"]
        )
        lines = result.splitlines()
        assert lines[1] == ""
        assert lines[2] == "master"
        assert lines[3] == "master"


class TestGitStatusPorcelainV2RenameAndSpacePathFidelity:

    def test_rename_record_reconstructed_as_short_style_arrow_line(self):
        cmd = 'echo "=== X ==="; git status --porcelain; pwd'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite,
            [
                "# branch.oid abc",
                "# branch.head main",
                "2 R100 N... 100644 100644 100644 aaa bbb R100 "
                "new name.txt\told name.txt",
            ],
        )
        assert "R100 new name.txt -> old name.txt" in result.splitlines()

    def test_modified_path_containing_space_not_fragmented(self):
        cmd = 'echo "=== X ==="; git status --porcelain; pwd'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite,
            [
                "# branch.oid abc",
                "# branch.head main",
                "1 M. N... 100644 100644 100644 aaa bbb file with space.txt",
            ],
        )
        assert "M. file with space.txt" in result.splitlines()

    def test_untracked_path_containing_space_not_fragmented(self):
        cmd = 'echo "=== X ==="; git status --porcelain; pwd'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite,
            [
                "# branch.oid abc",
                "# branch.head main",
                "? untracked with space.txt",
            ],
        )
        assert "?? untracked with space.txt" in result.splitlines()


class TestDatePortableAcrossPlatforms:
    """Finding 3 (P1): `%e` (space-padded day) is a glibc/BSD `strftime`
    EXTENSION -- absent from Windows CRT, raising `ValueError` there. Fixed
    by computing the space-padded day with plain Python string formatting
    instead of asking `strftime` for it, so no platform branch is needed and
    the real `date` command's exact output is still reproduced."""

    def test_date_rewrite_never_raises_and_matches_real_date(self):
        cmd = 'echo "=== FACTS ==="; pwd; date'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell(cmd)
        rewritten = _run_python_c(rewrite)
        if platform.system() != "Windows":
            assert original == rewritten
            return
        orig_lines = original.splitlines()
        rw_lines = rewritten.splitlines()
        assert orig_lines[:2] == rw_lines[:2]
        assert _date_facts(orig_lines[2]) == _date_facts(rw_lines[2])

    def test_date_rewrite_uses_no_percent_e_directive(self):
        cmd = 'echo "=== FACTS ==="; pwd; date'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        script = rewrite[len(_payload_prefix()) : -1]
        assert "%e" not in script


@pytest.fixture()
def census_dir(tmp_path):
    (tmp_path / "a.txt").write_text("hello TODO world\n")
    (tmp_path / "b.txt").write_text("nothing here\n")
    (tmp_path / "c.log").write_text("another TODO line\n")
    return tmp_path


class TestHeadTailPlumbingRewriteGeneratorPathJoin:

    def test_find_generator_joins_with_posixpath_not_os_path(self):
        parsed = ht._bt_parse_find_census_segment(["find", "/tmp/census", "-type", "f"])
        script = "\n".join(ht._bt_build_generator_lines("find", parsed))
        assert "posixpath.join(root, fn)" in script
        assert "os.path.join(root, fn)" not in script

    def test_grep_generator_joins_with_posixpath_not_os_path(self):
        parsed = dc._bt_grep_flags_and_operands(["grep", "-rn", "TODO", "/tmp/census"])
        script = "\n".join(ht._bt_build_generator_lines("grep", parsed))
        assert "posixpath.join(root, fn)" in script
        assert "os.path.join(root, fn)" not in script


class TestHeadTailPlumbingRewrite:
    def test_never_denies(self, census_dir):
        for cmd in [
            "ls %s | head -n 2" % _posix(census_dir),
            "find %s -type f | head" % _posix(census_dir),
            "grep -rn TODO %s | tail -n 1" % _posix(census_dir),
            "cat file | head",
            "a | b | c | head",
        ]:
            out = ht.check_head_tail_plumbing_rewrite(cmd)
            assert out is None or out["hookSpecificOutput"]["permissionDecision"] != "deny"

    def test_ls_head_equivalence(self, census_dir):
        cmd = "ls %s | head -n 2" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell(cmd)
        rewritten = _run_python_c(rewrite)
        assert original == rewritten

    def test_find_head_equivalence_sorted(self, census_dir):
        cmd = "find %s -type f -name '*.txt' | head -n 5" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell(
            "find %s -type f -name '*.txt' | sort | head -n 5" % _posix(census_dir)
        )
        rewritten = _run_python_c(rewrite)
        assert original == rewritten

    def test_grep_tail_equivalence_sorted(self, census_dir):
        cmd = "grep -rn TODO %s | tail -n 1" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell("grep -rn TODO %s | sort | tail -n 1" % _posix(census_dir))
        rewritten = _run_python_c(rewrite)
        assert original == rewritten

    def test_tail_zero_does_not_return_everything(self, census_dir):
        cmd = "ls %s | tail -n 0" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        assert _run_python_c(rewrite) == ""

    def test_unrecognized_upstream_silent_not_advised(self):
        assert ht.check_head_tail_plumbing_rewrite("sort weird.txt | head -n 3") is None

    def test_grep_dash_w_upstream_silent_not_advised(self, census_dir):
        """Finding 4: `check_head_tail_plumbing_rewrite`'s grep-upstream
        translation feeds the same `_bt_grep_flags_and_operands` choke
        point as `check_grep_via_bash_rewrite` -- `-w` must be refused here
        too, not just at the standalone-grep entry point, since dropping
        `-w` from `_GREP_SUBSTITUTABLE_SHORT_FLAGS` closes both callers at
        once. No rewrite means no advisory either (silenced branch)."""
        out = ht.check_head_tail_plumbing_rewrite(
            "grep -wrn TODO %s | head -n 5" % _posix(census_dir)
        )
        assert out is None

    def test_unrecognized_count_form_advises(self, census_dir):
        out = ht.check_head_tail_plumbing_rewrite("ls %s | head -c 10" % _posix(census_dir))
        assert out is not None
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_composed_three_stage_pipeline_silent_not_advised(self, census_dir):
        out = ht.check_head_tail_plumbing_rewrite(
            "ls %s | sort | head -n 2" % _posix(census_dir)
        )
        assert out is None

    def test_quoted_pipe_character_not_mis_split(self, census_dir):
        (census_dir / "pipe.txt").write_text("a|b marker\n")
        cmd = "grep -Frn 'a|b' %s | head -n 1" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell(cmd)
        rewritten = _run_python_c(rewrite)
        assert original == rewritten
        assert rewritten

    def test_quoted_pipe_bare_bre_pattern_refuses_not_mistranslates(self, census_dir):
        (census_dir / "pipe.txt").write_text("a|b marker\n")
        cmd = "grep -rn 'a|b' %s | head -n 1" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        assert out is None

    def test_bare_pipeline_without_head_tail_returns_none(self, census_dir):
        assert ht.check_head_tail_plumbing_rewrite("ls %s" % _posix(census_dir)) is None
        assert ht.check_head_tail_plumbing_rewrite("") is None

    def test_override_disables_check(self, monkeypatch, census_dir):
        monkeypatch.setenv("COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING", "1")
        cmd = "ls %s | head -n 2" % _posix(census_dir)
        assert ht.check_head_tail_plumbing_rewrite(cmd) is None

    def test_bare_head_no_pipe_returns_none(self, census_dir):
        assert ht.check_head_tail_plumbing_rewrite("head %s/a.txt" % _posix(census_dir)) is None


class TestRedirectionDeclinesRewriteNotRewritesWrong(object):
    """Task 4 (2026-07-29, live incident): a shell redirection operator
    (`2>`, `>`, `>>`, `<`) among the upstream segment's own tokens is NOT a
    path/pattern operand. Before this fix, `_bt_parse_ls_segment` took the
    LAST non-flag token as the directory to list, so
    `ls DIR 2>/dev/null | head -40` silently rewrote to
    `os.listdir("2>/dev/null")`, which raised `FileNotFoundError` at
    runtime -- an AUTO-APPLIED `updatedInput` rewrite, not an advisory, so
    the caller's command was silently replaced with one that meant
    something different before it failed. Every case here must decline to
    rewrite (advisory, `updatedInput` absent) rather than emit a wrong one --
    "prefer not rewriting over rewriting approximately".
    """

    def _declines_rewrite(self, cmd: str) -> None:
        assert ht.check_head_tail_plumbing_rewrite(cmd) is None

    def test_exact_live_repro_command(self, census_dir):
        self._declines_rewrite("ls %s 2>/dev/null | head -40" % _posix(census_dir))

    def test_ls_stderr_redirect_glued(self, census_dir):
        self._declines_rewrite("ls %s 2>/dev/null | head -n 2" % _posix(census_dir))

    def test_ls_stderr_redirect_separate_token(self, census_dir):
        self._declines_rewrite("ls %s 2> /dev/null | head -n 2" % _posix(census_dir))

    def test_ls_stdout_redirect(self, census_dir):
        self._declines_rewrite("ls %s >out.txt | head -n 2" % _posix(census_dir))

    def test_ls_append_redirect(self, census_dir):
        self._declines_rewrite("ls %s >>out.txt | head -n 2" % _posix(census_dir))

    def test_ls_input_redirect(self, census_dir):
        self._declines_rewrite("ls %s <in.txt | head -n 2" % _posix(census_dir))

    def test_find_stderr_redirect(self, census_dir):
        self._declines_rewrite(
            "find %s -type f 2>/dev/null | head -n 5" % _posix(census_dir)
        )

    def test_find_bare_redirect_as_first_token(self, tmp_path):
        self._declines_rewrite("find %s 2>/dev/null | head -n 5" % _posix(tmp_path))

    def test_grep_stderr_redirect(self, census_dir):
        self._declines_rewrite("grep -rn TODO %s 2>/dev/null | head -n 5" % _posix(census_dir))

    def test_standalone_grep_rewrite_also_declines(self, census_dir):
        out = dc.check_grep_via_bash_rewrite(
            "grep -rn TODO %s 2>/dev/null" % _posix(census_dir)
        )
        if out is not None:
            assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
            assert "updatedInput" not in out["hookSpecificOutput"]

    def test_unredirected_control_still_rewrites(self, census_dir):
        out = ht.check_head_tail_plumbing_rewrite("ls %s | head -n 2" % _posix(census_dir))
        assert out is not None
        assert "updatedInput" in out["hookSpecificOutput"]


class TestGeneratedPayloadShellSafety:
    def test_banner_rewrite_has_no_raw_single_quote_in_payload(self):
        cmd = 'echo "=== FACTS ==="; git rev-parse --abbrev-ref HEAD; git status; pwd; whoami'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        command = out["hookSpecificOutput"]["updatedInput"]["command"]
        prefix = _payload_prefix()
        assert command.startswith(prefix) and command.endswith("'")
        inner = command[len(prefix) : -1]
        assert "'" not in inner

    def test_head_tail_rewrite_has_no_raw_single_quote_in_payload(self, census_dir):
        prefix = _payload_prefix()
        for cmd in [
            "ls %s | head -n 2" % _posix(census_dir),
            "find %s -type f | tail -n 1" % _posix(census_dir),
            "grep -rn TODO %s | head -n 1 -l" % _posix(census_dir),
        ]:
            out = ht.check_head_tail_plumbing_rewrite(cmd)
            if out is None or "updatedInput" not in out["hookSpecificOutput"]:
                continue
            command = out["hookSpecificOutput"]["updatedInput"]["command"]
            inner = command[len(prefix) : -1]
            assert "'" not in inner, command

    def test_banner_rewrite_parses_under_current_interpreter(self):
        cmd = 'echo "=== FACTS ==="; pwd; whoami; date; uname'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        command = out["hookSpecificOutput"]["updatedInput"]["command"]
        inner = command[len(_payload_prefix()) : -1]
        compile(inner, "<test>", "exec")

    def test_head_tail_rewrite_parses_under_current_interpreter(self, census_dir):
        cmd = "grep -rn TODO %s | tail -n 1" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        command = out["hookSpecificOutput"]["updatedInput"]["command"]
        inner = command[len(_payload_prefix()) : -1]
        compile(inner, "<test>", "exec")


def _exec_script_text(script: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(script, "<old-style-bx16-script>", "exec"), {})
    return buf.getvalue()


def _old_style_head_tail_script(kind: str, parsed: dict, is_head: bool, n: int) -> str:
    gen_lines = ht._bt_build_generator_lines(kind, parsed)
    if is_head:
        slice_expr = "_out[:%d]" % n
    elif n > 0:
        slice_expr = "_out[-%d:]" % n
    else:
        slice_expr = "[]"
    return "\n".join(gen_lines + ["for _l in %s:" % slice_expr, "    print(_l)"])


class TestHeadTailRewriteDifferentialEquivalence:

    @pytest.fixture()
    def big_tree(self, tmp_path):
        for i in range(30):
            (tmp_path / ("f%03d.txt" % i)).write_text("TODO line %d\n" % i)
        (tmp_path / "skip.log").write_text("not a match\n")
        return tmp_path

    @pytest.mark.parametrize("n", [0, 1, 3, 10, 30, 999])
    def test_find_head(self, big_tree, n):
        up_tokens = ["find", _posix(big_tree), "-type", "f", "-name", "*.txt"]
        parsed = ht._bt_parse_find_census_segment(up_tokens)
        old = _exec_script_text(_old_style_head_tail_script("find", parsed, True, n))
        cmd = "find %s -type f -name '*.txt' | head -n %d" % (_posix(big_tree), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
        assert old == new

    @pytest.mark.parametrize("n", [0, 1, 3, 10, 30, 999])
    def test_find_tail(self, big_tree, n):
        up_tokens = ["find", _posix(big_tree), "-type", "f", "-name", "*.txt"]
        parsed = ht._bt_parse_find_census_segment(up_tokens)
        old = _exec_script_text(_old_style_head_tail_script("find", parsed, False, n))
        cmd = "find %s -type f -name '*.txt' | tail -n %d" % (_posix(big_tree), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
        assert old == new

    @pytest.mark.parametrize("n", [0, 1, 3, 30, 999])
    def test_grep_head(self, big_tree, n):
        up_tokens = ["grep", "-rn", "TODO", _posix(big_tree)]
        parsed = dc._bt_grep_flags_and_operands(up_tokens)
        old = _exec_script_text(_old_style_head_tail_script("grep", parsed, True, n))
        cmd = "grep -rn TODO %s | head -n %d" % (_posix(big_tree), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
        assert old == new

    @pytest.mark.parametrize("n", [0, 1, 3, 30, 999])
    def test_grep_tail(self, big_tree, n):
        up_tokens = ["grep", "-rn", "TODO", _posix(big_tree)]
        parsed = dc._bt_grep_flags_and_operands(up_tokens)
        old = _exec_script_text(_old_style_head_tail_script("grep", parsed, False, n))
        cmd = "grep -rn TODO %s | tail -n %d" % (_posix(big_tree), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
        assert old == new

    @pytest.mark.parametrize("n", [0, 1, 3, 30, 999])
    def test_ls_head_and_tail(self, big_tree, n):
        up_tokens = ["ls", _posix(big_tree)]
        parsed = ht._bt_parse_ls_segment(up_tokens)
        for is_head in (True, False):
            old = _exec_script_text(
                _old_style_head_tail_script("ls", parsed, is_head, n)
            )
            verb = "head" if is_head else "tail"
            cmd = "ls %s | %s -n %d" % (_posix(big_tree), verb, n)
            out = ht.check_head_tail_plumbing_rewrite(cmd)
            new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
            assert old == new, (verb, n)


class TestHeadShortCircuitStopsEarly:

    def test_find_head_walk_is_bounded(self, monkeypatch, tmp_path):
        total_dirs = 50
        for i in range(total_dirs):
            d = tmp_path / ("d%03d" % i)
            d.mkdir()
            (d / "match.txt").write_text("hit\n")

        real_walk = os.walk
        visited = {"count": 0}

        def counting_walk(*args, **kwargs):
            for item in real_walk(*args, **kwargs):
                visited["count"] += 1
                yield item

        monkeypatch.setattr(os, "walk", counting_walk)

        n = 3
        cmd = "find %s -type f -name 'match.txt' | head -n %d" % (_posix(tmp_path), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        result = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])

        assert len(result.strip().splitlines()) == n
        assert visited["count"] == 1 + n
        assert visited["count"] < total_dirs

    def test_grep_head_walk_is_bounded(self, monkeypatch, tmp_path):
        total_dirs = 50
        for i in range(total_dirs):
            d = tmp_path / ("d%03d" % i)
            d.mkdir()
            (d / "match.txt").write_text("TODO hit\n")

        real_walk = os.walk
        visited = {"count": 0}

        def counting_walk(*args, **kwargs):
            for item in real_walk(*args, **kwargs):
                visited["count"] += 1
                yield item

        monkeypatch.setattr(os, "walk", counting_walk)

        n = 4
        cmd = "grep -rn TODO %s | head -n %d" % (_posix(tmp_path), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        result = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])

        assert len(result.strip().splitlines()) == n
        assert visited["count"] == 1 + n
        assert visited["count"] < total_dirs

    def test_tail_still_observes_whole_stream(self, monkeypatch, tmp_path):
        total_dirs = 12
        for i in range(total_dirs):
            d = tmp_path / ("d%03d" % i)
            d.mkdir()
            (d / "match.txt").write_text("hit\n")

        real_walk = os.walk
        visited = {"count": 0}

        def counting_walk(*args, **kwargs):
            for item in real_walk(*args, **kwargs):
                visited["count"] += 1
                yield item

        monkeypatch.setattr(os, "walk", counting_walk)

        cmd = "find %s -type f -name 'match.txt' | tail -n 3" % _posix(tmp_path)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        result = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])

        assert len(result.strip().splitlines()) == 3
        assert visited["count"] == 1 + total_dirs
