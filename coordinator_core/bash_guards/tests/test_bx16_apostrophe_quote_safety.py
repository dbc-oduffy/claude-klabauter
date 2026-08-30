"""Regression coverage for the BX-16 apostrophe/outer-quote defect across
ALL FOUR `python3 -c` payload-construction sites in
``coordinator_core.bash_guards.dispatch_checks``: ``check_find_exec_rewrite``,
``check_grep_via_bash_rewrite``, ``check_multiprobe_banner_rewrite``, and
``check_head_tail_plumbing_rewrite``.

The defect: a bare apostrophe in a user-supplied grep pattern or filesystem
path breaks the outer shell quote in the generated payload, when that payload
is hand-wrapped as ``"python3 -c '%s'" % body`` -- a raw `'` inside `body`
terminates the outer single-quoted shell argument early and hands back a
truncated, syntactically-broken command as `updatedInput`, which is WORSE
than the original (working) command it replaced. Confirmed pre-existing in
the original five BX-16 shapes and (at introduction) in the two newer ones
(multiprobe banner, head/tail plumbing) -- fixed uniformly across all four
emission sites by replacing the hand-wrap with `shlex.quote(body)` (POSIX
quoting, correct for any embedded `'`), never fixed piecemeal.

Verified here by ACTUALLY EXECUTING the generated payload via a real `bash
-c` subprocess (not just reading/compiling it) -- this is how the defect was
originally caught (fold a real apostrophe in, watch the outer quote break).

Spec backlink: DoE-claude:pln-windows-viability-stop-the-spa-b969d9
row BX-16 (DoE-claude); this file's own coverage supersedes the "compile
under current interpreter" checks in `test_bx16_multiprobe_and_headtail_
rewrite.py`'s `TestGeneratedPayloadShellSafety` for the apostrophe case
specifically -- those pin quote-content ABSENCE, this file pins quote-content
PRESENCE (an apostrophe intentionally included) surviving a REAL shell
round-trip.
"""
from __future__ import annotations

import platform
import shutil
import subprocess

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards import guard_head_tail_rewrite as ht
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


def _run_via_real_shell(command: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Execute `command` through an actual shell (`bash -c` on POSIX, the
    Bash-tool's real execution path on macOS/Linux and -- per this
    dispatch's own Windows-readiness verification -- Git Bash on a
    Windows-with-Git-Bash host) rather than compiling the embedded script
    in-process. This is the only way to prove the OUTER shell quoting
    survives, not just that the inner Python is syntactically valid.

    Explicitly routed through Git Bash on Windows: plain
    `subprocess.run(shell=True)` there launches cmd.exe, a wholly
    different tokenizer that cannot parse the `shlex.quote`-produced
    (POSIX) outer quoting this suite exists to verify, and silently
    reports "the filename... is incorrect" instead of running anything."""
    if platform.system() == "Windows":
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("git-bash not found on PATH")
        return subprocess.run(
            [bash, "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            **no_console_creationflags(),
        )
    return subprocess.run(
        # popup-intentional-last-resort: shell=True spawns a cmd.exe
        # intermediary that CREATE_NO_WINDOW does not suppress; the
        # STARTUPINFO route is a separate, wider fix (review: code-reviewer).
        command, shell=True, capture_output=True, text=True, timeout=timeout,
    )


class TestFindExecRewriteApostropheSafety:
    def test_path_with_apostrophe_does_not_break_outer_quote(self, tmp_path):
        target_dir = tmp_path / "don't-panic"
        target_dir.mkdir()
        (target_dir / "a.tmp").write_text("x")
        cmd = 'find "%s" -name "*.tmp" -exec rm {} \\;' % _posix(target_dir)
        out = dc.check_find_exec_rewrite(cmd)
        assert out is not None
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_via_real_shell(rewrite)
        assert result.returncode == 0, (rewrite, result.stdout, result.stderr)
        assert not (target_dir / "a.tmp").exists()


class TestGrepViaBashRewriteApostropheSafety:
    def test_pattern_with_apostrophe_does_not_break_outer_quote(self, tmp_path):
        (tmp_path / "f.txt").write_text("don't stop\nkeep going\n")
        cmd = "grep -rn \"don't\" %s" % _posix(tmp_path)
        out = dc.check_grep_via_bash_rewrite(cmd)
        assert out is not None
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_via_real_shell(rewrite)
        assert result.returncode == 0, (rewrite, result.stdout, result.stderr)
        assert "don't stop" in result.stdout


class TestMultiprobeBannerRewriteApostropheSafety:
    def test_echo_banner_with_apostrophe_does_not_break_outer_quote(self):
        cmd = 'echo "=== it\'s facts ==="; pwd; whoami'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        assert out is not None
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_via_real_shell(rewrite)
        assert result.returncode == 0, (rewrite, result.stdout, result.stderr)
        assert "it's facts" in result.stdout


class TestHeadTailPlumbingRewriteApostropheSafety:
    def test_grep_upstream_pattern_with_apostrophe_does_not_break_outer_quote(self, tmp_path):
        (tmp_path / "f.txt").write_text("don't stop\nkeep going\n")
        cmd = "grep -rn \"don't\" %s | head -n 1" % _posix(tmp_path)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        assert out is not None
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_via_real_shell(rewrite)
        assert result.returncode == 0, (rewrite, result.stdout, result.stderr)
        assert "don't stop" in result.stdout

    def test_find_upstream_path_with_apostrophe_does_not_break_outer_quote(self, tmp_path):
        target_dir = tmp_path / "don't-panic"
        target_dir.mkdir()
        (target_dir / "a.tmp").write_text("x")
        cmd = 'find "%s" -name "*.tmp" | head -n 5' % _posix(target_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        assert out is not None
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_via_real_shell(rewrite)
        assert result.returncode == 0, (rewrite, result.stdout, result.stderr)
        assert "a.tmp" in result.stdout
