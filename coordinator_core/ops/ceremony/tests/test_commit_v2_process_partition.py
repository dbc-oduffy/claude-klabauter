"""`ceremony.commit_v2` lands product paths and process paths (`state/`, `docs/plans/`, `archive/`,
`.coordinator-local/`) as two commits on a non-DoE repo, product first. Throwaway repos under `tmp_path`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import CommitRefused
from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout


def _repo(tmp_path: Path, doe: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    files = ["seed.txt"]
    if doe:
        (repo / ".coordinator-dev-repo").write_text("", encoding="utf-8")
        files.append(".coordinator-dev-repo")
    _git(["add", "--", *files], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _head(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _files(repo: Path, rev: str) -> list:
    return _git(["show", "--name-only", "--format=", rev], repo).split()


def _subject(repo: Path, rev: str) -> str:
    return _git(["log", "-1", "--format=%s", rev], repo).strip()


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _mixed(repo: Path) -> dict:
    _write(repo, "src/app.py")
    _write(repo, "state/notes/n.md")
    return {"paths": ["src/app.py", "state/notes/n.md"], "message": "ship feature\n"}


def test_mixed_call_lands_product_then_process(tmp_path):
    repo = _repo(tmp_path)
    base = _head(repo)

    result = _call(repo, _mixed(repo))

    assert result["committed"] is True, result
    assert result["sha"] != result["process_sha"]
    assert _files(repo, result["sha"]) == ["src/app.py"]
    assert _files(repo, result["process_sha"]) == ["state/notes/n.md"]
    assert _subject(repo, result["process_sha"]) == "ship feature (process files)"
    assert _subject(repo, result["sha"]) == "ship feature"
    assert _git(["rev-parse", f"{result['process_sha']}^"], repo).strip() == result["sha"]
    assert _git(["rev-parse", f"{result['sha']}^"], repo).strip() == base


def test_pure_product_lands_one_commit(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "src/app.py")

    result = _call(repo, {"paths": ["src/app.py"], "message": "product only\n"})

    assert result["committed"] is True, result
    assert "process_sha" not in result
    assert _head(repo) == result["sha"]


def test_pure_process_lands_one_commit(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "docs/plans/p.md")

    result = _call(repo, {"paths": ["docs/plans/p.md"], "message": "plan only\n"})

    assert result["committed"] is True, result
    assert "process_sha" not in result
    assert _subject(repo, result["sha"]) == "plan only"


def test_doe_repo_lands_one_mixed_commit(tmp_path):
    repo = _repo(tmp_path, doe=True)

    result = _call(repo, _mixed(repo))

    assert result["committed"] is True, result
    assert "process_sha" not in result
    assert sorted(_files(repo, result["sha"])) == ["src/app.py", "state/notes/n.md"]


def test_process_commit_failure_keeps_product_commit_and_names_unlanded(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    params = _mixed(repo)
    real = commit_v2.commit_paths
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise CommitRefused("synthetic process refusal")
        return real(*args, **kwargs)

    monkeypatch.setattr(commit_v2, "commit_paths", flaky)

    result = _call(repo, params)

    assert result["committed"] is False
    assert "synthetic process refusal" in result["error"]
    assert result["unlanded_paths"] == ["state/notes/n.md"]
    assert _head(repo) == result["landed_sha"]
    assert _files(repo, result["landed_sha"]) == ["src/app.py"]


def test_retry_of_a_mixed_call_reports_both_landed(tmp_path):
    repo = _repo(tmp_path)
    params = _mixed(repo)
    first = _call(repo, dict(params))
    assert first["committed"] is True, first

    again = _call(repo, dict(params))

    assert again["committed"] is True, again
    assert again["sha"] == first["sha"]
    assert again["process_sha"] == first["process_sha"]
    assert _head(repo) == first["process_sha"]
