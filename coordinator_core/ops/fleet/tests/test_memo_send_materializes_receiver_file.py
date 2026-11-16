"""memo.send reports delivered only when the delivery commit carries the memo at the receiver inbox path.

The delivery commit is built object-only on the receiver's upstream tip and anchored; the receiver's
checkout (HEAD, index, worktree) is never written, so a live session there is never disturbed. A
different memo already at the path is a collision, never an overwrite. Receivers are real tmp_path
git repos; `_memo_send` runs at its default params.
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


def _checkout_state(root: Path) -> tuple:
    return (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "symbolic-ref", "HEAD"),
        (root / ".git" / "index").read_bytes(),
        _git(root, "status", "--porcelain"),
    )


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


def test_default_send_delivers_a_commit_carrying_the_memo_and_leaves_the_checkout_alone(world):
    sender, receiver = world["sender"], world["receiver"]
    _write_draft(sender, "file-topic")
    before = _checkout_state(receiver)

    result = _memo_send({"dry_run": False, "topic": "file-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    sha = result["acted"][0]["delivery_commit_sha"]
    rel = _memo_rel("file-topic")
    assert _in_commit(receiver, sha, rel)
    assert _git(receiver, "cat-file", "-t", sha) == "commit"
    assert _checkout_state(receiver) == before
    assert not (receiver / rel).exists()


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


def test_each_cc_receiver_gets_a_delivery_commit_and_an_untouched_checkout(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
    cc_repo = _make_receiver_git_repo(tmp_path, name="cc-repo")
    monkeypatch.setenv(
        "CLAUDE_HOME",
        str(_make_claude_home(tmp_path, {"project_rag": to_repo, "example_cockpit_repo": cc_repo})),
    )
    _write_draft_with_cc(sender, "cc-file-topic", to="example-retrieval-repo-em", cc="example-cockpit-repo-em")
    before = {r: _checkout_state(r) for r in (to_repo, cc_repo)}

    result = _memo_send({"dry_run": False, "topic": "cc-file-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    for repo in (to_repo, cc_repo):
        assert _checkout_state(repo) == before[repo]
        assert not list((repo / "cross-repo" / "inbox").glob("*.md"))
    assert _in_commit(to_repo, result["acted"][0]["delivery_commit_sha"], _memo_rel("cc-file-topic"))
