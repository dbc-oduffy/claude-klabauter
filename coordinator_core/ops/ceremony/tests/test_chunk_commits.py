
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import chunk_commits
from coordinator_core.win_portability import no_console_creationflags

# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _write(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _commit(repo: Path, rel_path: str, *messages: str) -> str:
    _git(["add", "--", rel_path], repo)
    args = ["commit", "-q"]
    for m in messages:
        args += ["-m", m]
    _git(args, repo)
    return _git(["rev-parse", "HEAD"], repo).strip()


def _touch_commit(repo: Path, rel_path: str, content: str, subject: str) -> str:
    _write(repo, rel_path, content)
    return _commit(repo, rel_path, subject)


def test_correct_form_returns_chunk_commits(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_commit(repo, "README.md", "seed\n", "chore: seed repo")
    plan_sha = _touch_commit(
        repo, "docs/plans/x.md", "---\ntitle: x\n---\n", "docs: add plan x"
    )
    other_sha = _touch_commit(repo, "src/other.py", "pass\n", "unrelated commit")
    chunk_sha = _touch_commit(
        repo, "src/foo.py", "def foo(): pass\n", "C1: implement foo"
    )

    result = chunk_commits.resolve_chunk_commits(repo, "docs/plans/x.md", "C1")

    assert result == [{"sha": chunk_sha, "subject": "C1: implement foo"}]
    assert plan_sha != chunk_sha and other_sha != chunk_sha


def test_subject_filter_rejects_body_line_match(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_commit(
        repo, "docs/plans/x.md", "---\ntitle: x\n---\n", "docs: add plan x"
    )
    _write(repo, "src/foo.py", "def foo(): pass\n")
    _git(["add", "--", "src/foo.py"], repo)
    _git(
        ["commit", "-q", "-m", "unrelated subject", "-m", "C1: sneaky body trailer"],
        repo,
    )

    result = chunk_commits.resolve_chunk_commits(repo, "docs/plans/x.md", "C1")

    assert result == []


def test_anchor_excludes_commits_before_add_commit(tmp_path):
    repo = _init_repo(tmp_path)
    # A DIFFERENT plan's C1 chunk lands FIRST, entirely before our plan's own
    foreign_sha = _touch_commit(
        repo, "src/foreign.py", "pass\n", "C1: foreign plan's own chunk"
    )
    plan_sha = _touch_commit(
        repo, "docs/plans/x.md", "---\ntitle: x\n---\n", "docs: add plan x"
    )
    in_range_sha = _touch_commit(
        repo, "src/foo.py", "def foo(): pass\n", "C1: implement foo"
    )

    result = chunk_commits.resolve_chunk_commits(repo, "docs/plans/x.md", "C1")

    assert result == [{"sha": in_range_sha, "subject": "C1: implement foo"}]
    shas = [c["sha"] for c in result]
    assert foreign_sha not in shas
    assert plan_sha != in_range_sha


def test_never_ran_chunk_returns_empty_list(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_commit(
        repo, "docs/plans/x.md", "---\ntitle: x\n---\n", "docs: add plan x"
    )
    _touch_commit(repo, "src/foo.py", "def foo(): pass\n", "C1: implement foo")

    result = chunk_commits.resolve_chunk_commits(repo, "docs/plans/x.md", "C9")

    assert result == []


def test_unresolvable_plan_path_fails_loud(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_commit(repo, "README.md", "seed\n", "chore: seed repo")

    with pytest.raises(ValueError):
        chunk_commits.resolve_chunk_commits(repo, "docs/plans/does-not-exist.md", "C1")


def test_plan_with_no_add_commit_fails_loud(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_commit(repo, "README.md", "seed\n", "chore: seed repo")
    _write(repo, "docs/plans/x.md", "---\ntitle: x\n---\n")

    with pytest.raises(ValueError):
        chunk_commits.resolve_chunk_commits(repo, "docs/plans/x.md", "C1")


def test_multiple_commits_for_one_chunk_returns_all(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_commit(
        repo, "docs/plans/x.md", "---\ntitle: x\n---\n", "docs: add plan x"
    )
    sha1 = _touch_commit(repo, "src/a.py", "a = 1\n", "C1: part one")
    sha2 = _touch_commit(repo, "src/b.py", "b = 2\n", "C1: part two")
    sha3 = _touch_commit(repo, "src/c.py", "c = 3\n", "C1: part three")

    result = chunk_commits.resolve_chunk_commits(repo, "docs/plans/x.md", "C1")

    assert [c["sha"] for c in result] == [sha1, sha2, sha3]
    assert [c["subject"] for c in result] == [
        "C1: part one",
        "C1: part two",
        "C1: part three",
    ]


def test_handler_resolves_repo_from_plan_path_and_returns_bare_list(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_commit(
        repo, "docs/plans/x.md", "---\ntitle: x\n---\n", "docs: add plan x"
    )
    chunk_sha = _touch_commit(
        repo, "src/foo.py", "def foo(): pass\n", "C1: implement foo"
    )

    result = chunk_commits._handler(
        {"plan_path": str(repo / "docs" / "plans" / "x.md"), "chunk_id": "C1"}
    )

    assert result == [{"sha": chunk_sha, "subject": "C1: implement foo"}]


def test_handler_rejects_plan_path_that_resolves_to_a_directory(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_commit(
        repo, "docs/plans/x.md", "---\ntitle: x\n---\n", "docs: add plan x"
    )

    with pytest.raises(ValueError, match="resolves to a directory"):
        chunk_commits._handler({"plan_path": str(repo / "docs" / "plans"), "chunk_id": "C1"})
