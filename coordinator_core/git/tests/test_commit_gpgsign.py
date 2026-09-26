"""`commit_paths` honours `commit.gpgsign` (claude-klabauter#34).

Three things this file pins:

1. `_gpgsign_enabled` reads `[commit] gpgsign`, ZERO spawns, same
   candidate-file/precedence shape as `_config_identity`.
2. `gpgsign` unset (the ordinary case) costs nothing extra -- the whole
   `commit_paths` route stays at zero spawns, exactly the pre-existing
   invariant `test_commit_zero_spawn.py` pins.
3. `gpgsign` set spawns exactly ONE `git commit-tree -S` and uses its sha;
   a signing failure lands the commit UNSIGNED with `sign_warning` set,
   never refuses the commit.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import commit as commit_mod
from coordinator_core.git.run import GitResult

_ENV_KEYS = (
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
)

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _write(path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_memos(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    commit_mod._CONFIG_IDENTITY_MEMO.clear()
    commit_mod._GPGSIGN_CACHE.clear()
    yield
    commit_mod._CONFIG_IDENTITY_MEMO.clear()
    commit_mod._GPGSIGN_CACHE.clear()


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global-gitconfig"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def _popen_spy(monkeypatch):
    seen: "list[list[str]]" = []
    real = subprocess.Popen

    class Spy(real):  # type: ignore[misc,valid-type]
        def __init__(self, cmd, *a, **kw):
            seen.append(cmd if isinstance(cmd, str) else list(cmd)[:4])
            super().__init__(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    return seen


def test_gpgsign_defaults_false_with_no_config(isolated_config):
    assert commit_mod._gpgsign_enabled(None) is False


def test_gpgsign_true_read_from_global_config(isolated_config):
    _write(isolated_config / "global-gitconfig", "[commit]\n\tgpgsign = true\n")
    assert commit_mod._gpgsign_enabled(None) is True


def test_gpgsign_false_read_explicitly(isolated_config):
    _write(isolated_config / "global-gitconfig", "[commit]\n\tgpgsign = false\n")
    assert commit_mod._gpgsign_enabled(None) is False


def test_gpgsign_repo_local_outranks_global(isolated_config):
    _write(isolated_config / "global-gitconfig", "[commit]\n\tgpgsign = true\n")
    repo = isolated_config / "repo"
    _write(repo / ".git" / "config", "[commit]\n\tgpgsign = false\n")
    assert commit_mod._gpgsign_enabled(repo) is False


def test_gpgsign_read_costs_zero_spawns(isolated_config, monkeypatch):
    _write(isolated_config / "global-gitconfig", "[commit]\n\tgpgsign = true\n")
    seen = _popen_spy(monkeypatch)
    assert commit_mod._gpgsign_enabled(None) is True
    assert seen == [], f"gpgsign config read spawned {seen}"


def test_gpgsign_cached_across_repeated_resolves(isolated_config, monkeypatch):
    _write(isolated_config / "global-gitconfig", "[commit]\n\tgpgsign = true\n")
    first = commit_mod._gpgsign_enabled(None)
    seen = _popen_spy(monkeypatch)
    for _ in range(5):
        assert commit_mod._gpgsign_enabled(None) is first
    assert seen == [], f"cached gpgsign reads spawned {seen}"


def test_gpgsign_answers_per_repo_in_one_process(isolated_config):
    signed = isolated_config / "signed"
    plain = isolated_config / "plain"
    _write(signed / ".git" / "config", "[commit]\n\tgpgsign = true\n")
    _write(plain / ".git" / "config", "[core]\n\tbare = false\n")
    assert commit_mod._gpgsign_enabled(signed) is True
    assert commit_mod._gpgsign_enabled(plain) is False


def test_commit_paths_stays_zero_spawn_when_gpgsign_unset(tmp_path, isolated_config, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
    seen = _popen_spy(monkeypatch)

    outcome = commit_mod.commit_paths(repo, ["new.txt"], "add new")

    assert seen == [], f"expected zero spawns with gpgsign unset, got {seen}"
    assert outcome.sign_warning is None


def test_commit_paths_signs_via_commit_tree_when_gpgsign_true(tmp_path, isolated_config, monkeypatch):
    """Real signing end to end, via `gpg.format=ssh` -- `ssh-keygen` is on
    every box this suite runs on; `gpg` is not, so this is the format that
    can exercise the real external program rather than a mock of it.

    `GIT_CONFIG_NOSYSTEM` and a cleared `SSH_AUTH_SOCK`: this box's system
    and global git config route ssh signing through a 1Password agent
    helper, which fails against a throwaway test key -- this isolates the
    test from that ambient machine config, it is not part of the behaviour
    under test.
    """
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    repo = _repo(tmp_path)
    keyfile = isolated_config / "signing_key"
    keygen = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(keyfile), "-q"],
        capture_output=True, text=True, **_NOWIN,
    )
    assert keygen.returncode == 0, keygen.stderr
    _write(
        repo / ".git" / "config",
        "[commit]\n\tgpgsign = true\n"
        "[gpg]\n\tformat = ssh\n"
        f"[user]\n\tsigningkey = {keyfile.as_posix()}\n",
    )
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    calls = []
    from coordinator_core.git import run as run_mod

    real_run_git = run_mod.run_git

    def spying_run_git(args, **kw):
        calls.append(list(args))
        return real_run_git(args, **kw)

    monkeypatch.setattr(run_mod, "run_git", spying_run_git)

    outcome = commit_mod.commit_paths(repo, ["new.txt"], "add new, signed")

    assert len(calls) == 1, f"expected exactly one commit-tree -S spawn, got {calls}"
    assert calls[0][:2] == ["commit-tree", "-S"]
    assert outcome.sign_warning is None, outcome.sign_warning
    assert _git(repo, "show", "HEAD:new.txt").stdout == "new\n"
    assert _git(repo, "cat-file", "-e", outcome.sha).returncode == 0
    verify = _git(repo, "log", "-1", "--show-signature", outcome.sha, check=False)
    combined = (verify.stdout + verify.stderr).lower()
    assert "good" in combined or "signature" in combined, combined


def test_commit_paths_degrades_to_unsigned_on_signing_failure(tmp_path, isolated_config, monkeypatch):
    repo = _repo(tmp_path)
    _write(repo / ".git" / "config", "[commit]\n\tgpgsign = true\n")
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    def failing_run_git(args, **kw):
        return GitResult(
            returncode=128, timed_out=False,
            stdout="", stderr="gpg failed to sign the data",
            stdout_bytes=b"",
        )

    monkeypatch.setattr("coordinator_core.git.run.run_git", failing_run_git)

    outcome = commit_mod.commit_paths(repo, ["new.txt"], "add new, unsigned fallback")

    assert outcome.sign_warning is not None
    assert "committed unsigned" in outcome.sign_warning
    assert "gpg failed to sign the data" in outcome.sign_warning
    assert _git(repo, "show", "HEAD:new.txt").stdout == "new\n"
    assert _git(repo, "cat-file", "-e", outcome.sha).returncode == 0


def test_commit_paths_degrades_to_unsigned_when_signing_raises(tmp_path, isolated_config, monkeypatch):
    repo = _repo(tmp_path)
    _write(repo / ".git" / "config", "[commit]\n\tgpgsign = true\n")
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    def raising_run_git(args, **kw):
        raise OSError("git executable not found")

    monkeypatch.setattr("coordinator_core.git.run.run_git", raising_run_git)

    outcome = commit_mod.commit_paths(repo, ["new.txt"], "add new, executable missing")

    assert outcome.sign_warning is not None
    assert "committed unsigned" in outcome.sign_warning
    assert _git(repo, "show", "HEAD:new.txt").stdout == "new\n"
