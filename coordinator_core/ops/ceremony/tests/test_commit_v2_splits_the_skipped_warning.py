
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "seed.md").write_bytes(b"seed\n")
    (repo / "held.md").write_bytes(b"held\n")
    _git(["add", "--", "seed.md", "held.md"], repo)
    _git(["commit", "-qm", "seed"], repo)
    return repo


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _joined(result) -> str:
    return " ".join(result["warnings"])


def test_a_path_head_never_had_refuses_the_whole_call_not_a_skipped_warning(tmp_path):
    repo = _repo(tmp_path)
    before = _git(["rev-parse", "HEAD"], repo).strip()
    (repo / "seed.md").write_bytes(b"moved\n")

    result = _call(
        repo,
        {
            "paths": ["seed.md"],
            "deleted_paths": ["ghost.md"],
            "message": "docs: edit",
        },
    )

    assert result["committed"] is False
    assert "ghost.md" in result["error"]
    assert _git(["rev-parse", "HEAD"], repo).strip() == before


def test_a_path_matching_head_still_gets_the_already_at_head_sentence(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.md").write_bytes(b"moved\n")

    result = _call(
        repo, {"paths": ["seed.md", "held.md"], "message": "docs: edit"}
    )

    assert result["committed"] is True
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert "already at HEAD" in warning
    assert "held.md" in warning
    assert "SKIPPED" not in warning


def test_both_kinds_together_still_refuses_the_whole_call_on_the_phantom(tmp_path):
    repo = _repo(tmp_path)
    (repo / "extra.md").write_bytes(b"extra\n")
    _git(["add", "--", "extra.md"], repo)
    _git(["commit", "-qm", "extra"], repo)
    (repo / "extra.md").write_bytes(b"moved\n")
    before = _git(["rev-parse", "HEAD"], repo).strip()

    result = _call(
        repo,
        {
            "paths": ["extra.md", "held.md"],
            "deleted_paths": ["ghost.md"],
            "message": "docs: edit",
        },
    )

    assert result["committed"] is False
    assert "ghost.md" in result["error"]
    assert _git(["rev-parse", "HEAD"], repo).strip() == before


def test_an_ordinary_commit_warns_about_neither(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.md").write_bytes(b"moved\n")

    result = _call(repo, {"paths": ["seed.md"], "message": "docs: edit"})

    assert result["committed"] is True
    assert "already at HEAD" not in _joined(result)
    assert "SKIPPED" not in _joined(result)
