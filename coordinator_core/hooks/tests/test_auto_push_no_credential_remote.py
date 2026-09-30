"""A push that cannot authenticate is not logged as a push failure.

The cadence sweep's bare-https probe in a cloud container dies with "could not
read Username" while the session's real pushes succeed through a proxy; every
such row is noise in `.git/push-failures.log`. A credentialless https remote is
skipped by `log_failure`; a remote with any credential source still logs.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.hooks import auto_push

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_NO_CRED_ERR = (
    "git push: fatal: could not read Username for 'https://github.com': "
    "terminal prompts disabled"
)


@pytest.fixture
def hermetic_git_env(monkeypatch, tmp_path):
    for key in ("GIT_ASKPASS", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(key, raising=False)
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _repo(tmp_path, url):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", url], check=True)
    return root


def _log(root):
    return root / ".git" / "push-failures.log"


def _feed(root, first_err=_NO_CRED_ERR):
    auto_push.log_failure(
        str(root), "work/foo", "cadence-sweep", "sweep-failed", 1, first_err, ""
    )


def test_credentialless_https_remote_writes_no_row(tmp_path, hermetic_git_env):
    root = _repo(tmp_path, "https://github.com/org/repo")
    _feed(root)
    assert not _log(root).exists()
    assert not list((root / ".git").glob("push-stderr-*.log"))


def test_credential_helper_keeps_the_row(tmp_path, hermetic_git_env):
    root = _repo(tmp_path, "https://github.com/org/repo")
    subprocess.run(
        ["git", "-C", str(root), "config", "credential.helper", "store"], check=True
    )
    _feed(root)
    assert "PUSH FAILED on work/foo" in _log(root).read_text(encoding="utf-8")


def test_askpass_keeps_the_row(tmp_path, hermetic_git_env, monkeypatch):
    root = _repo(tmp_path, "https://github.com/org/repo")
    monkeypatch.setenv("GIT_ASKPASS", "/bin/true")
    _feed(root)
    assert _log(root).exists()


def test_userinfo_url_keeps_the_row(tmp_path, hermetic_git_env):
    root = _repo(tmp_path, "https://user:tok@github.com/org/repo")
    _feed(root)
    assert _log(root).exists()


def test_ssh_remote_keeps_the_row(tmp_path, hermetic_git_env):
    root = _repo(tmp_path, "git@github.com:org/repo.git")
    _feed(root)
    assert _log(root).exists()


def test_other_failure_on_credentialless_remote_keeps_the_row(tmp_path, hermetic_git_env):
    root = _repo(tmp_path, "https://github.com/org/repo")
    _feed(root, "git push: fatal: unable to access: Could not resolve host: github.com")
    assert _log(root).exists()
