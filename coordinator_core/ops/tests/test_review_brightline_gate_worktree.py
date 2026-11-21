"""review_brightline_gate._run_git: read-only legs, bounded, degrade on timeout."""

from __future__ import annotations


from coordinator_core.ops import review_brightline_gate as gate


def _result(stdout: str = "", returncode: int = 0, timed_out: bool = False, stderr: str = ""):
    from coordinator_core.git.run import GitResult

    fields = dict.fromkeys(GitResult._fields)
    fields.update(stdout=stdout, returncode=returncode, timed_out=timed_out, stderr=stderr)
    return GitResult(**fields)


def test_run_git_prefixes_no_optional_locks_through_the_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def _fake_run_git(args, **kwargs):
        seen["args"] = list(args)
        return _result("out", 0)

    monkeypatch.setattr("coordinator_core.git.run.run_git", _fake_run_git)

    assert gate._run_git(["diff", "--numstat", "HEAD"]) == ("out", 0)
    assert seen["args"] == ["--no-optional-locks", "diff", "--numstat", "HEAD"]


def test_run_git_timeout_degrades_to_nonzero_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def _hang(args, **kwargs):
        calls.append(args)
        return _result("", -1, timed_out=True)

    monkeypatch.setattr("coordinator_core.git.run.run_git", _hang)

    out, rc = gate._run_git(["log", "--oneline", "HEAD"])
    assert out == ""
    assert rc != 0
    assert len(calls) == 1


def test_run_git_missing_git_still_degrades_to_127(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinator_core.git.run.run_git", lambda args, **kw: _result("", 127, stderr="no git"))

    assert gate._run_git(["status"]) == ("", 127)


# --- `--worktree-base` mode (D7/C9, AC8) ---

import pytest  # noqa: E402

from coordinator_core.win_portability import no_console_creationflags  # noqa: E402
