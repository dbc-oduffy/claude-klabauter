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

Spec backlink: docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md
row BX-16 (example-doctrine-repo); this file's own coverage supersedes the "compile
under current interpreter" checks in `test_bx16_multiprobe_and_headtail_
rewrite.py`'s `TestGeneratedPayloadShellSafety` for the apostrophe case
specifically -- those pin quote-content ABSENCE, this file pins quote-content
PRESENCE (an apostrophe intentionally included) surviving a REAL shell
round-trip.
"""
from __future__ import annotations

import subprocess

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards import guard_head_tail_rewrite as ht


def _run_via_real_shell(command: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Execute `command` through an actual shell (`bash -c` on POSIX, the
    Bash-tool's real execution path on macOS/Linux and -- per this
    dispatch's own Windows-readiness verification -- Git Bash on a
    Windows-with-Git-Bash host) rather than compiling the embedded script
    in-process. This is the only way to prove the OUTER shell quoting
    survives, not just that the inner Python is syntactically valid."""
    return subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=timeout
    )


class TestFindExecRewriteApostropheSafety:
    def test_path_with_apostrophe_does_not_break_outer_quote(self, tmp_path):
        target_dir = tmp_path / "don't-panic"
        target_dir.mkdir()
        (target_dir / "a.tmp").write_text("x")
        cmd = 'find "%s" -name "*.tmp" -exec rm {} \\;' % target_dir
        out = dc.check_find_exec_rewrite(cmd)
        assert out is not None
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_via_real_shell(rewrite)
        assert result.returncode == 0, (rewrite, result.stdout, result.stderr)
        assert not (target_dir / "a.tmp").exists()


class TestGrepViaBashRewriteApostropheSafety:
    def test_pattern_with_apostrophe_does_not_break_outer_quote(self, tmp_path):
        (tmp_path / "f.txt").write_text("don't stop\nkeep going\n")
        cmd = "grep -rn \"don't\" %s" % tmp_path
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
        cmd = "grep -rn \"don't\" %s | head -n 1" % tmp_path
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
        cmd = 'find "%s" -name "*.tmp" | head -n 5' % target_dir
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        assert out is not None
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_via_real_shell(rewrite)
        assert result.returncode == 0, (rewrite, result.stdout, result.stderr)
        assert "a.tmp" in result.stdout
