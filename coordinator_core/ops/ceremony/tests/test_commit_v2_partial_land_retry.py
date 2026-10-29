"""`ceremony.commit_v2` over 11 product + 157 docs/ process paths: default params land every path;
a forced blob-fallback failure reports the true unlanded count and cause, and the retry makes progress."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import CommitRefused
from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_N_PRODUCT = 11
_N_DOCS = 157


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", ".gitattributes", "seed.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _is_process(args) -> bool:
    return bool(args[1]) and all(p.startswith("docs/plans/") for p in args[1])


def _params(repo: Path) -> dict:
    paths = []
    for i in range(_N_PRODUCT):
        rel = f"src/m{i}.py"
        (repo / "src").mkdir(exist_ok=True)
        (repo / rel).write_bytes(f"x = {i}\n".encode())
        paths.append(rel)
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    for i in range(_N_DOCS):
        rel = f"docs/plans/d{i}.seam.yaml" if i % 2 else f"docs/plans/d{i}.md"
        (repo / rel).write_bytes(f"k: {i}\r\nv: y\r\n".encode())
        paths.append(rel)
    return {"paths": paths, "message": "big land\n", "full_reply": True}


def test_default_params_land_every_path(tmp_path):
    repo = _repo(tmp_path)
    params = _params(repo)

    result = commit_v2._handler(dict(params), repo_root=repo / ".git")

    assert result["committed"] is True, result
    tracked = set(_git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split())
    assert set(params["paths"]) <= tracked


def test_forced_fallback_failure_reports_true_count_and_cause_then_retry_progresses(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    params = _params(repo)
    real = commit_v2.commit_paths
    state = {"fail": True}

    def flaky(*args, **kwargs):
        if state["fail"] and _is_process(args):
            raise CommitRefused("hash-object failed", cause="fatal: boom", first_unhashed="docs/d0.md")
        return real(*args, **kwargs)

    monkeypatch.setattr(commit_v2, "commit_paths", flaky)

    result = commit_v2._handler(dict(params), repo_root=repo / ".git")

    assert result["committed"] is False
    assert len(result["unlanded_paths"]) == _N_DOCS
    assert f"{_N_DOCS} process path(s) did not land" in result["error"]
    assert "fatal: boom" in result["error"]
    assert "Retry the same call" not in result["error"]
    assert result["landed_sha"] == _git(["rev-parse", "HEAD"], repo).strip()

    state["fail"] = False
    retry = commit_v2._handler(dict(params), repo_root=repo / ".git")

    assert retry["committed"] is True, retry
    assert retry["sha"] == result["landed_sha"]
    tracked = set(_git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split())
    assert set(params["paths"]) <= tracked


def test_timeout_cause_says_retry(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    params = _params(repo)
    real = commit_v2.commit_paths

    def flaky(*args, **kwargs):
        if _is_process(args):
            raise CommitRefused("hash-object failed", cause="timed out")
        return real(*args, **kwargs)

    monkeypatch.setattr(commit_v2, "commit_paths", flaky)

    result = commit_v2._handler(dict(params), repo_root=repo / ".git")

    assert "Retry the same call" in result["error"]
