
from __future__ import annotations

from pathlib import Path

from coordinator_core.commit_ledger import store


def _init_git_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_closes_and_reverts_sha_recorded(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    assert store.append_entry(
        "hnd-a",
        "sha1",
        "code",
        cwd=cwd,
        closes=["RECS-1", "RECS-2"],
        reverts_sha="deadbeef",
    )
    entries = store.read_entries("hnd-a", cwd=cwd)
    assert len(entries) == 1
    assert entries[0]["closes"] == ["RECS-1", "RECS-2"]
    assert entries[0]["reverts_sha"] == "deadbeef"


def test_closes_and_reverts_sha_omitted_when_not_supplied(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    assert store.append_entry("hnd-a", "sha1", "code", cwd=cwd)
    entries = store.read_entries("hnd-a", cwd=cwd)
    assert "closes" not in entries[0]
    assert "reverts_sha" not in entries[0]


def test_closes_follows_first_line_wins_not_union(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-a", "sha1", "code", cwd=cwd, closes=["RECS-1"])
    store.append_entry("hnd-a", "sha1", "code", cwd=cwd, closes=["RECS-2"])
    entries = store.read_entries("hnd-a", cwd=cwd)
    assert len(entries) == 1
    assert entries[0]["closes"] == ["RECS-1"]


def test_reviewed_by_still_unions_alongside_closes(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-a", "sha1", "code", cwd=cwd, closes=["RECS-1"], reviewed_by=["alice"])
    store.append_entry("hnd-a", "sha1", "code", cwd=cwd, reviewed_by=["bob"])
    entries = store.read_entries("hnd-a", cwd=cwd)
    assert len(entries) == 1
    assert entries[0]["closes"] == ["RECS-1"]
    assert entries[0]["reviewed_by"] == ["alice", "bob"]
