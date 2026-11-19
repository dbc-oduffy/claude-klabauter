"""memo.send reports delivered only when the delivery commit carries the memo at the receiver inbox path.

A receiver whose HEAD branch is at its tracking tip gets the delivery commit on that branch: the
branch advances to it, the memo file appears in the worktree and the index, HEAD's symref stays and
nothing else in the checkout changes. A different memo already at the path is a collision, never an
overwrite. Receivers are real tmp_path git repos; `_memo_send` runs at its default params.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._memo_compose import _memo_filename
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _git as _run_git,
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)
from coordinator_core.ops.fleet.tests.test_memo_send_cc_delivery import _write_draft_with_cc

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_INBOX = "cross-repo/inbox"


def _git(cwd, *args) -> str:
    out = _run_git(cwd, *args).stdout
    return (out.decode() if isinstance(out, bytes) else out).strip()


def _in_commit(receiver: Path, sha: str, rel: str) -> bool:
    return rel in _git(receiver, "ls-tree", "-r", "--name-only", sha).splitlines()


def _memo_rel(topic: str) -> str:
    return f"{_INBOX}/{_memo_filename(_dt.date.today().isoformat(), 'claude-klabauter-engine', topic)}"


@pytest.fixture
def world(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    receiver = _make_receiver_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
    return {"sender": sender, "receiver": receiver, "tmp": tmp_path}


def _assert_materialized(receiver: Path, sha: str, rel: str, symref: str) -> None:
    assert _in_commit(receiver, sha, rel)
    assert _git(receiver, "cat-file", "-t", sha) == "commit"
    assert _git(receiver, "rev-parse", "HEAD") == sha
    assert _git(receiver, "symbolic-ref", "HEAD") == symref
    assert (receiver / rel).exists()
    assert rel in _git(receiver, "ls-files", "--", rel)
    assert _git(receiver, "status", "--porcelain") == ""


def test_default_send_advances_the_head_branch_and_materializes_the_memo(world):
    sender, receiver = world["sender"], world["receiver"]
    _write_draft(sender, "file-topic")
    symref = _git(receiver, "symbolic-ref", "HEAD")

    result = _memo_send({"dry_run": False, "topic": "file-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert result["acted"][0]["delivery_outcome"] == "delivered"
    _assert_materialized(
        receiver, result["acted"][0]["delivery_commit_sha"], _memo_rel("file-topic"), symref
    )


def test_an_unrelated_dirty_tracked_file_keeps_its_worktree_and_index_bytes(world):
    sender, receiver = world["sender"], world["receiver"]
    dirty = receiver / "cross-repo" / "inbox" / ".gitkeep"
    dirty.write_text("edited\n", encoding="utf-8")
    index_entry = _git(receiver, "ls-files", "-s", "--", "cross-repo/inbox/.gitkeep")
    _write_draft(sender, "dirty-topic")

    result = _memo_send({"dry_run": False, "topic": "dirty-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert dirty.read_text(encoding="utf-8") == "edited\n"
    assert _git(receiver, "ls-files", "-s", "--", "cross-repo/inbox/.gitkeep") == index_entry
    assert _in_commit(receiver, result["acted"][0]["delivery_commit_sha"], _memo_rel("dirty-topic"))


def test_a_held_receiver_ref_or_index_lock_does_not_stop_the_delivery(world):
    sender, receiver = world["sender"], world["receiver"]
    _write_draft(sender, "locked-topic")
    (receiver / ".git" / "index.lock").write_text("", encoding="utf-8")
    (receiver / ".git" / "refs" / "heads" / "main.lock").write_text("", encoding="utf-8")

    result = _memo_send({"dry_run": False, "topic": "locked-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert _in_commit(receiver, result["acted"][0]["delivery_commit_sha"], _memo_rel("locked-topic"))


def test_a_different_memo_already_committed_at_the_path_is_never_overwritten(world):
    sender, receiver = world["sender"], world["receiver"]
    _write_draft(sender, "clash-topic")
    rel = _memo_rel("clash-topic")
    target = receiver / rel
    target.write_text("committed elsewhere\n", encoding="utf-8")
    _git(receiver, "add", "--", rel)
    _git(receiver, "commit", "-q", "-m", "clash")
    _git(receiver, "push", "-q", "origin", "work/test/2026-11-16")

    result = _memo_send({"dry_run": False, "topic": "clash-topic"}, repo_root=sender)

    assert result["exit_code"] != 0, result
    assert result["failed"][0]["reason"].startswith("collision"), result["failed"]
    assert target.read_text(encoding="utf-8") == "committed elsewhere\n"


def test_each_cc_receiver_gets_the_memo_on_its_head_branch(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
    cc_repo = _make_receiver_git_repo(tmp_path, name="cc-repo")
    monkeypatch.setenv(
        "CLAUDE_HOME",
        str(_make_claude_home(tmp_path, {"project_rag": to_repo, "example_cockpit_repo": cc_repo})),
    )
    _write_draft_with_cc(sender, "cc-file-topic", to="example-retrieval-repo-em", cc="example-cockpit-repo-em")
    symrefs = {r: _git(r, "symbolic-ref", "HEAD") for r in (to_repo, cc_repo)}

    result = _memo_send({"dry_run": False, "topic": "cc-file-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    rel = _memo_rel("cc-file-topic")
    for repo in (to_repo, cc_repo):
        assert _git(repo, "symbolic-ref", "HEAD") == symrefs[repo]
        assert (repo / rel).exists()
        assert _git(repo, "status", "--porcelain") == ""
    assert _in_commit(to_repo, result["acted"][0]["delivery_commit_sha"], rel)
