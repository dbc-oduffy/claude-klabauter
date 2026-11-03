"""review_brightline_gate._run_git: read-only legs, bounded, degrade on timeout."""

from __future__ import annotations

import subprocess

from coordinator_core.ops import review_brightline_gate as gate


def _result(stdout="", returncode=0, timed_out=False, stderr=""):
    from coordinator_core.git.run import GitResult

    fields = dict.fromkeys(GitResult._fields)
    fields.update(stdout=stdout, returncode=returncode, timed_out=timed_out, stderr=stderr)
    return GitResult(**fields)


def test_run_git_prefixes_no_optional_locks_through_the_shared_runner(monkeypatch):
    seen = {}

    def _fake_run_git(args, **kwargs):
        seen["args"] = list(args)
        return _result("out", 0)

    monkeypatch.setattr("coordinator_core.git.run.run_git", _fake_run_git)

    assert gate._run_git(["diff", "--numstat", "HEAD"]) == ("out", 0)
    assert seen["args"] == ["--no-optional-locks", "diff", "--numstat", "HEAD"]


def test_run_git_timeout_degrades_to_nonzero_without_retry(monkeypatch):
    calls = []

    def _hang(args, **kwargs):
        calls.append(args)
        return _result("", -1, timed_out=True)

    monkeypatch.setattr("coordinator_core.git.run.run_git", _hang)

    out, rc = gate._run_git(["log", "--oneline", "HEAD"])
    assert out == ""
    assert rc != 0
    assert len(calls) == 1


def test_run_git_missing_git_still_degrades_to_127(monkeypatch):
    monkeypatch.setattr("coordinator_core.git.run.run_git", lambda args, **kw: _result("", 127, stderr="no git"))

    assert gate._run_git(["status"]) == ("", 127)


# --- `--worktree-base` mode (D7/C9, AC8) ---

import pytest  # noqa: E402

from coordinator_core.win_portability import no_console_creationflags  # noqa: E402


def _g(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _g(repo, "init", "-q")
    _g(repo, "config", "user.email", "test@example.com")
    _g(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _g(repo, "add", "a.py")
    _g(repo, "commit", "-q", "-m", "init")


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_worktree_base_counts_tracked_and_untracked_declared_paths(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _g(repo, "rev-parse", "HEAD").strip()
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (repo / "b.py").write_text("z = 3\nw = 4\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    rc = gate.main(["--worktree-base", base, "--paths", "a.py", "b.py"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "basis=code-only,worktree" in out
    assert "commits=n/a" in out
    assert f"range={base}..worktree" in out
    assert "loc=3" in out
    assert "files=2" in out


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_worktree_base_requires_paths(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _g(repo, "rev-parse", "HEAD").strip()
    monkeypatch.chdir(repo)

    rc = gate.main(["--worktree-base", base])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert "--paths" in captured.err


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_worktree_base_ignores_peer_commit_to_undeclared_path(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _g(repo, "rev-parse", "HEAD").strip()
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (repo / "peer.py").write_text("p = 1\np = 2\np = 3\n", encoding="utf-8")
    _g(repo, "add", "peer.py")
    _g(repo, "commit", "-q", "-m", "peer work")
    monkeypatch.chdir(repo)

    rc = gate.main(["--worktree-base", base, "--paths", "a.py"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "loc=1" in out
    assert "peer.py" not in out
