"""Tests for coordinator_core.git.remote_url.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C3.

Real throwaway git repos (tmp_path + `git init`/`git remote add`), one
fixture per test -- consistent with the repo-wide convention documented in
`coordinator_core/ops/tests/test_staleness_git.py` and this package's own
`test_repo_root.py`. This module has no walk/memo path to unit-test with a
fake `.git` layout (see `remote_url.py`'s docstring -- it always spawns), so
every test here exercises the real `git` binary.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import remote_url

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_TIMEOUT = 10


def _run_git(args, cwd):
    subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _run_git(["init", "-b", "main"], repo)
    _run_git(["config", "user.email", "test@example.invalid"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    return repo


def test_get_remote_url_returns_configured_url(tmp_path):
    repo = _init_repo(tmp_path)
    _run_git(["remote", "add", "origin", "https://example.invalid/o/r.git"], repo)
    assert remote_url.get_remote_url("origin", cwd=str(repo)) == "https://example.invalid/o/r.git"


def test_get_remote_url_absent_remote_returns_none(tmp_path):
    repo = _init_repo(tmp_path)
    assert remote_url.get_remote_url("origin", cwd=str(repo)) is None


def test_get_remote_url_not_a_repo_returns_none(tmp_path):
    lone = tmp_path / "lonely"
    lone.mkdir()
    assert remote_url.get_remote_url("origin", cwd=str(lone)) is None


def test_get_remote_url_git_missing_oserror_returns_none(tmp_path, monkeypatch):
    def _fake_run(*a, **kw):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    repo = tmp_path / "unused"
    repo.mkdir()
    assert remote_url.get_remote_url("origin", cwd=str(repo)) is None


def test_get_remote_url_timeout_returns_none(tmp_path, monkeypatch):
    def _fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    repo = tmp_path / "unused2"
    repo.mkdir()
    assert remote_url.get_remote_url("origin", cwd=str(repo)) is None


def test_get_remote_url_matches_real_git_rev_parse_toplevel_scoped(tmp_path):
    """resolve()-equality against `git rev-parse --show-toplevel`, comparing
    resolved PATHS never strings (Windows forward-vs-backslash trap) -- this
    is a sanity check that this module's spawn runs in the same resolved
    repo `git rev-parse` itself resolves to, not a test of the URL string
    (per the brief: path-equality cannot stand in for a URL-derivation
    test, which is why the other tests above assert on the URL value
    directly).
    """
    repo = _init_repo(tmp_path)
    _run_git(["remote", "add", "origin", "https://example.invalid/o/r.git"], repo)

    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()

    from pathlib import Path

    assert Path(toplevel).resolve() == repo.resolve()
    assert remote_url.get_remote_url("origin", cwd=str(repo)) == "https://example.invalid/o/r.git"


def test_get_remote_url_matches_git_remote_get_url_directly(tmp_path):
    """Equivalence test (brief's DERIVATION METHOD requirement): this
    module's answer must match `git remote get-url origin` run directly,
    since this module is a thin spawn wrapper around exactly that command
    and not a `.git/config` parser.
    """
    repo = _init_repo(tmp_path)
    _run_git(["remote", "add", "origin", "git@example.invalid:o/r.git"], repo)

    direct = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()

    assert remote_url.get_remote_url("origin", cwd=str(repo)) == direct


def test_get_remote_url_respects_insteadof_rewrite(tmp_path):
    """Fixture repo carrying an `insteadOf` rewrite (brief's named
    divergence case). This module spawns `git remote get-url`, which
    reports the REWRITTEN url -- a `.git/config` line-parser would report
    the pre-rewrite literal instead, which is exactly the divergence this
    module's docstring names as the reason it does not parse.
    """
    repo = _init_repo(tmp_path)
    _run_git(["remote", "add", "origin", "short:o/r.git"], repo)
    _run_git(
        ["config", "url.https://example.invalid/expanded/.insteadOf", "short:"],
        repo,
    )

    direct = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()

    resolved = remote_url.get_remote_url("origin", cwd=str(repo))
    assert resolved == direct
    assert resolved == "https://example.invalid/expanded/o/r.git"


def test_get_remote_url_absent_to_present_within_one_process(tmp_path):
    """No-memo correctness (brief's absent-to-present obligation): a remote
    added mid-process must be visible on the very next call -- this module
    keeps no cwd-keyed memo (unlike `repo_root`), so there is nothing to
    clear and nothing that can serve a stale `None` here.
    """
    repo = _init_repo(tmp_path)
    assert remote_url.get_remote_url("origin", cwd=str(repo)) is None
    _run_git(["remote", "add", "origin", "https://example.invalid/o/r.git"], repo)
    assert remote_url.get_remote_url("origin", cwd=str(repo)) == "https://example.invalid/o/r.git"
