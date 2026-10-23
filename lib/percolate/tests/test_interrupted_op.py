from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate.interrupted_op import (  # noqa: E402
    PEER_MERGE_MARKER,
    InProgress,
    inspect_in_progress,
    is_own_peer_merge,
    peer_merge_message,
)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_clean_dir_yields_none(tmp_path):
    assert inspect_in_progress(_repo(tmp_path)) is None


def test_merge_head_with_msg(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".git" / "MERGE_HEAD").write_text("abc\n")
    (repo / ".git" / "MERGE_MSG").write_text("first line\nsecond\n")
    assert inspect_in_progress(repo) == InProgress("merge", "first line")


def test_merge_head_without_msg(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".git" / "MERGE_HEAD").write_text("abc\n")
    assert inspect_in_progress(repo) == InProgress("merge", "")


@pytest.mark.parametrize(
    "name,is_dir,kind",
    [
        ("CHERRY_PICK_HEAD", False, "cherry-pick"),
        ("REVERT_HEAD", False, "revert"),
        ("rebase-merge", True, "rebase"),
        ("rebase-apply", True, "rebase"),
        ("BISECT_LOG", False, "bisect"),
    ],
)
def test_each_state_file_yields_its_kind(tmp_path, name, is_dir, kind):
    repo = _repo(tmp_path)
    target = repo / ".git" / name
    target.mkdir() if is_dir else target.write_text("x\n")
    assert inspect_in_progress(repo) == InProgress(kind, "")


def test_merge_wins_over_later_kinds(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".git" / "MERGE_HEAD").write_text("abc\n")
    (repo / ".git" / "BISECT_LOG").write_text("x\n")
    assert inspect_in_progress(repo).kind == "merge"


def test_gitfile_pointer_resolved(tmp_path):
    real = tmp_path / "gitdirs" / "wt"
    real.mkdir(parents=True)
    (real / "REVERT_HEAD").write_text("x\n")
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {real}\n")
    assert inspect_in_progress(repo) == InProgress("revert", "")


def test_marker_message_matches():
    msg = peer_merge_message("origin/candidate", "candidate")
    assert msg.startswith(PEER_MERGE_MARKER)
    assert is_own_peer_merge(msg, "origin/candidate")
    assert not is_own_peer_merge(msg, "origin/other")


def test_default_message_matches_with_and_without_into():
    assert is_own_peer_merge("Merge remote-tracking branch 'origin/candidate'", "origin/candidate")
    assert is_own_peer_merge(
        "Merge remote-tracking branch 'origin/candidate' into candidate", "origin/candidate"
    )


@pytest.mark.parametrize(
    "line",
    [
        "Merge remote-tracking branch 'origin/other'",
        "Merge remote-tracking branch 'origin/candidate-2' into candidate",
        "Merge branch 'feature'",
        "",
    ],
)
def test_foreign_messages_do_not_match(line):
    assert not is_own_peer_merge(line, "origin/candidate")
