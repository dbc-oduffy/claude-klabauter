"""Every `git notes add` remedy the commit guard prescribes carries the correction-note sentinel."""

from __future__ import annotations

import shlex
import subprocess

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.git.correction_note import SENTINEL, parse_note
from coordinator_core.win_portability import no_console_passthrough_kwargs


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, **no_console_passthrough_kwargs())


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "mine.txt").write_text("x")
    _git(repo, "add", "mine.txt")
    _git(repo, "commit", "-q", "-m", "peer subject line")
    (repo / "extra.txt").write_text("x")
    _git(repo, "add", "extra.txt")
    return repo


def test_amend_deny_remedy_carries_sentinel(tmp_path):
    repo = _repo(tmp_path)
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-mine")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    remedy = [ln for ln in reason.splitlines() if "git notes add" in ln]
    assert remedy and all(('-m "%s ' % SENTINEL) in ln for ln in remedy)
    assert "<correction>" not in reason


def test_prescribed_note_parses_back_to_its_subject():
    note = dispatch_checks._NOTE_CORRECTION.replace("<subject>", "fix: the thing")
    assert parse_note(note) == "fix: the thing"
