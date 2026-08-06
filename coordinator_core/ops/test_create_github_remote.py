"""Tests for coordinator_core.ops.create_github_remote (op
`repo.create_and_push_remote`, settlement A3).

All git activity runs in tmp_path throwaway repos; `gh` is never invoked —
the module's `_gh` seam is monkeypatched with a stateful fake whose "GitHub"
is a local bare repo, so `git push` is exercised for real against it.

Coverage map (settlement A3):
  - create path: repo absent on "GitHub" → created + remote configured + pushed
  - already-existed path: created:false, already_existed:true, push still runs
  - double-invocation (CC-4/AC7): second call is the documented no-op shape
  - TOCTOU belt-and-suspenders: view says absent, create errors "already
    exists" → treated as already_existed, push runs
  - created-but-push-failed rerun: push failure raises; rerun after the
    remote is repaired runs straight through to the push
  - param validation fail-loud: bad visibility / empty name / non-repo root
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import create_github_remote as cgr


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


class _FakeGh:
    """Stateful stand-in for the `gh` CLI: its 'GitHub' is a dict of
    repo-name → clone URL (a local bare repo path), so pushes are real."""

    def __init__(self, url_for: dict[str, str]):
        self.url_for = url_for
        self.existing: set[str] = set()
        self.calls: list[list[str]] = []
        self.create_error: str | None = None

    def __call__(self, args: list[str], cwd=None) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:2] == ["repo", "view"]:
            name = args[2]
            if name not in self.existing:
                return subprocess.CompletedProcess(args, 1, "", "GraphQL: Could not resolve")
            if "--json" in args:
                return subprocess.CompletedProcess(
                    args, 0, '{"url": "%s"}' % self.url_for[name].replace("\\", "\\\\"), ""
                )
            return subprocess.CompletedProcess(args, 0, name, "")
        if args[:2] == ["repo", "create"]:
            name = args[2]
            if self.create_error is not None:
                return subprocess.CompletedProcess(args, 1, "", self.create_error)
            if name in self.existing:
                return subprocess.CompletedProcess(
                    args, 1, "", f"Name already exists on this account: {name}"
                )
            self.existing.add(name)
            # Unlike real gh, deliberately does NOT configure the local
            # remote — exercises the op's own layer-2 remote-add path.
            return subprocess.CompletedProcess(args, 0, self.url_for[name], "")
        raise AssertionError(f"unexpected gh invocation: {args}")


@pytest.fixture()
def gh_env(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    bare = tmp_path / "hub.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
    )
    fake = _FakeGh({"myproj": str(bare)})
    monkeypatch.setattr(cgr, "_gh", fake)
    return repo, bare, fake


def test_create_path_creates_configures_remote_and_pushes(gh_env):
    repo, bare, fake = gh_env
    out = cgr.create_and_push_remote(repo, "myproj", "private")
    assert out == {"created": True, "already_existed": False, "pushed": True}
    assert "myproj" in fake.existing
    url = _git(repo, "remote", "get-url", "origin").stdout.strip()
    assert url == str(bare)
    # The push landed for real in the bare repo.
    heads = subprocess.run(
        ["git", "for-each-ref", "refs/heads"], cwd=bare,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "main" in heads


def test_double_invocation_is_documented_noop_shape(gh_env):
    """CC-4/AC7: second call with identical inputs is a safe no-op returning
    {created: false, already_existed: true, pushed: true}."""
    repo, _bare, _fake = gh_env
    first = cgr.create_and_push_remote(repo, "myproj", "private")
    assert first["created"] is True
    second = cgr.create_and_push_remote(repo, "myproj", "private")
    assert second == {"created": False, "already_existed": True, "pushed": True}


def test_already_existed_path_still_configures_remote_and_pushes(gh_env):
    repo, bare, fake = gh_env
    fake.existing.add("myproj")  # GitHub repo pre-exists; no local remote yet
    out = cgr.create_and_push_remote(repo, "myproj", "public")
    assert out == {"created": False, "already_existed": True, "pushed": True}
    assert _git(repo, "remote", "get-url", "origin").stdout.strip() == str(bare)
    assert ["repo", "create", "myproj", "--public", "--source", str(repo),
            "--remote", "origin"] not in fake.calls


def test_toctou_already_exists_error_treated_as_existing(gh_env):
    """View says absent, create races and errors 'already exists' →
    already_existed, not a failure; push still runs."""
    repo, bare, fake = gh_env
    fake.create_error = "GraphQL: Name already exists on this account (createRepository)"
    _git(repo, "remote", "add", "origin", str(bare))
    fake.url_for["myproj"] = str(bare)
    fake.existing.add("myproj")  # so any later view resolves
    # Force the view-probe miss despite existence: probe happens first, so
    # simulate by removing then restoring around the call is unnecessary —
    # the fake's view consults `existing`, so drop it for the probe and let
    # create_error fire.
    fake.existing.discard("myproj")
    out = cgr.create_and_push_remote(repo, "myproj", "private")
    assert out == {"created": False, "already_existed": True, "pushed": True}


def test_push_failure_raises_and_rerun_pushes_through(gh_env):
    """Created-but-push-failed reruns straight through to the push
    (settlement A3 rerunnability story)."""
    repo, bare, fake = gh_env
    dead = repo.parent / "nonexistent.git"
    fake.url_for["myproj"] = str(dead)
    with pytest.raises(RuntimeError, match="git push"):
        cgr.create_and_push_remote(repo, "myproj", "private")
    # Repair the remote (out-of-band) and rerun: no re-create, push succeeds.
    _git(repo, "remote", "set-url", "origin", str(bare))
    out = cgr.create_and_push_remote(repo, "myproj", "private")
    assert out == {"created": False, "already_existed": True, "pushed": True}


def test_invalid_visibility_fails_loud(gh_env):
    repo, _bare, _fake = gh_env
    with pytest.raises(ValueError, match="visibility"):
        cgr.create_and_push_remote(repo, "myproj", "internal")


def test_empty_name_fails_loud(gh_env):
    repo, _bare, _fake = gh_env
    with pytest.raises(ValueError, match="name"):
        cgr.create_and_push_remote(repo, "", "private")


def test_non_repo_root_fails_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(cgr, "_gh", _FakeGh({}))
    bare_dir = tmp_path / "notarepo"
    bare_dir.mkdir()
    with pytest.raises(ValueError, match="not a git worktree"):
        cgr.create_and_push_remote(bare_dir, "myproj", "private")


def test_handler_registered_and_routes_params(gh_env):
    repo, _bare, _fake = gh_env
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler("repo.create_and_push_remote")
    assert handler is not None
    out = asyncio.run(
        handler({"repo_root": str(repo), "name": "myproj", "visibility": "private"})
    )
    assert out == {"created": True, "already_existed": False, "pushed": True}


def test_handler_without_any_repo_root_fails_loud():
    with pytest.raises(ValueError, match="repo_root"):
        asyncio.run(
            cgr._create_and_push_remote_handler(
                {"name": "x", "visibility": "private"}, None
            )
        )
