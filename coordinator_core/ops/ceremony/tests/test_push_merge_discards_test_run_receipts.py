"""Push-ladder merge recovery discards dirty regenerable test-run receipts."""
from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony import push as push_mod
from coordinator_core.ops.ceremony.tests.test_push_reject_landed_by_peer import (
    _git,
    _init_repo_with_upstream,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_RECEIPT = "state/test-runs/fast-last.json"


def _commit_file(repo, rel: str, text: str, msg: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", msg], repo)


def _setup(tmp_path, tracked: str = _RECEIPT, upstream_edits: str = _RECEIPT):
    repo = _init_repo_with_upstream(tmp_path)
    _commit_file(repo, tracked, "v1", "tracked")
    _git(["push", "-q", "origin", "work/x"], repo)
    other = tmp_path / "other"
    _git(["clone", "-q", str(tmp_path / "origin.git"), str(other)], tmp_path)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["checkout", "-q", "work/x"], other)
    _commit_file(other, upstream_edits, "upstream", "upstream edit")
    _git(["push", "-q", "origin", "work/x"], other)
    _commit_file(repo, "ours.txt", "unpublished", "ours")
    _git(["fetch", "-q", "origin"], repo)
    return repo


def test_dirty_receipt_the_merge_changes_no_longer_blocks_the_merge(tmp_path):
    repo = _setup(tmp_path)
    (repo / _RECEIPT).write_text("local rewrite", encoding="utf-8")

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code == 0, reason
    assert (repo / _RECEIPT).read_text(encoding="utf-8") == "upstream"
    assert _git(["status", "--porcelain"], repo).strip() == ""


def test_dirty_non_receipt_path_the_merge_changes_still_declines(tmp_path):
    repo = _setup(tmp_path, "docs/note.md", "docs/note.md")
    (repo / "docs/note.md").write_text("local edit", encoding="utf-8")
    head_before = _git(["rev-parse", "HEAD"], repo).strip()

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code != 0
    assert reason.startswith("merge recovery declined, nothing touched: ")
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before
    assert (repo / "docs/note.md").read_text(encoding="utf-8") == "local edit"


def test_dirty_receipt_upstream_did_not_change_is_left_alone(tmp_path):
    repo = _setup(tmp_path, _RECEIPT, "docs/n.md")
    (repo / _RECEIPT).write_text("local rewrite", encoding="utf-8")

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code == 0, reason
    assert (repo / _RECEIPT).read_text(encoding="utf-8") == "local rewrite"
