"""
coordinator_core.ops.ceremony.tests.test_post_commit_tail_completion_fold

Regression coverage for the 2026-08-30 completion-entry commit-ledger fold
(`post_commit_tail.py`'s "Completion-entry commit-ledger fold" section) —
AC5 of `state/handoffs/2026-08-29-rebuild-completion-reconcile-commits-
under-the-bar.md`: a real completion-entry write, then a late-landing
commit, then the fold folding that commit's sha into the entry's
`commits:` YAML list.

Spec backlink: docs/research/spike-verdicts/2026-08-30-the-completion-
entrys-commit-ledger-folds-at-the-event.md.

Spawns real git (mirrors `test_post_commit_tail.py`'s own `real_git_repo`
fixture import — the mocked suites in this package deliberately do not
import it).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.ops.ceremony import consumed_handoff_stamp
from coordinator_core.ops.ceremony import post_commit_tail as m
from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _write_completion_entry(root: Path, rel: str, extra_commits_block: str = "commits: []") -> Path:
    entry_path = root / rel
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(
        f"---\nstatus: draft\n{extra_commits_block}\n---\n\n# a completion entry\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "add completion entry"], root)
    return entry_path


async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
    return consumed_handoff_stamp.StampOutcome()


async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
    return {"exit_code": 0, "closed": [], "skipped": []}


def _run_tail(root: Path, entry_rel: str, sha: str) -> m.PostCommitTailOutcome:
    return _run(
        m.run(
            root,
            root,
            "sid-1",
            sha,
            chain_terminal=False,
            governing_plan_slug="",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub,
            push_mode="none",
            completion_entry_path=entry_rel,
        )
    )


def test_late_landing_commit_folds_into_entry_commits_list(monkeypatch, tmp_path):
    root = real_git_repo(tmp_path)
    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    entry_rel = "archive/completed/2026-08/entry.md"
    _write_completion_entry(root, entry_rel)

    # A late-landing session commit, unrelated to the entry file itself.
    (root / "some-file.txt").write_text("work\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "the late-landing session commit"], root)
    landed_sha = _git(["rev-parse", "HEAD"], root).stdout.strip()

    outcome = _run_tail(root, entry_rel, landed_sha)

    assert outcome.completion_entry_fold_result["failed"] == []
    assert outcome.completion_entry_fold_result["acted"] == [entry_rel]

    entry_text_after = (root / entry_rel).read_text(encoding="utf-8")
    assert landed_sha in entry_text_after

    # The fold's own commit landed and is real HEAD -- read the entry back
    # from git, not just the worktree, to prove it is actually committed.
    committed_text = subprocess.run(
        ["git", "show", f"HEAD:{entry_rel}"], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout
    assert landed_sha in committed_text


def test_placeholder_comment_form_is_handled(monkeypatch, tmp_path):
    root = real_git_repo(tmp_path)
    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    entry_rel = "archive/completed/2026-08/entry-comment.md"
    _write_completion_entry(
        root,
        entry_rel,
        extra_commits_block="commits: []  # fill via completion.reconcile_commits (Step 2.6.8)",
    )

    (root / "some-file2.txt").write_text("work\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "another late-landing session commit"], root)
    landed_sha = _git(["rev-parse", "HEAD"], root).stdout.strip()

    outcome = _run_tail(root, entry_rel, landed_sha)

    assert outcome.completion_entry_fold_result["failed"] == []
    assert landed_sha in (root / entry_rel).read_text(encoding="utf-8")


def test_second_fold_pass_is_idempotent_no_op(monkeypatch, tmp_path):
    root = real_git_repo(tmp_path)
    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    entry_rel = "archive/completed/2026-08/entry-idempotent.md"
    _write_completion_entry(root, entry_rel)

    (root / "some-file3.txt").write_text("work\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "yet another late-landing session commit"], root)
    landed_sha = _git(["rev-parse", "HEAD"], root).stdout.strip()

    first = _run_tail(root, entry_rel, landed_sha)
    assert first.completion_entry_fold_result["acted"] == [entry_rel]

    head_after_first = _git(["rev-parse", "HEAD"], root).stdout.strip()

    second = _run_tail(root, entry_rel, landed_sha)
    assert second.completion_entry_fold_result["acted"] == []
    assert second.completion_entry_fold_result["failed"] == []
    assert second.completion_entry_fold_result["skipped"] == [
        f"{m.OP_COMPLETION_ENTRY_FOLD}:already-present"
    ]

    # A no-op fold must not create a duplicate row, and must not land a
    # second, empty commit.
    head_after_second = _git(["rev-parse", "HEAD"], root).stdout.strip()
    assert head_after_second == head_after_first
    entry_text = (root / entry_rel).read_text(encoding="utf-8")
    assert entry_text.count(landed_sha) == 1


def test_no_entry_path_is_a_clean_skip_not_a_failure(monkeypatch, tmp_path):
    root = real_git_repo(tmp_path)
    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)
    landed_sha = _git(["rev-parse", "HEAD"], root).stdout.strip()

    outcome = _run(
        m.run(
            root,
            root,
            "sid-1",
            landed_sha,
            chain_terminal=False,
            governing_plan_slug="",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub,
            push_mode="none",
        )
    )

    assert outcome.completion_entry_fold_result["failed"] == []
    assert outcome.completion_entry_fold_result["acted"] == []


def test_entry_path_escaping_allowed_roots_is_skipped_not_written(monkeypatch, tmp_path):
    root = real_git_repo(tmp_path)
    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)
    landed_sha = _git(["rev-parse", "HEAD"], root).stdout.strip()

    outcome = _run_tail(root, "state/handoffs/not-a-completion-entry.md", landed_sha)

    assert outcome.completion_entry_fold_result["failed"] == []
    assert outcome.completion_entry_fold_result["acted"] == []
    assert not (root / "state/handoffs/not-a-completion-entry.md").exists()


def test_malformed_commits_shape_soft_fails(monkeypatch, tmp_path):
    root = real_git_repo(tmp_path)
    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    entry_rel = "docs/plans/2026-08-30-malformed.md"
    entry_path = root / entry_rel
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(
        "---\nstatus: draft\ncommits: not-a-list-or-block\n---\n\nbody\n", encoding="utf-8"
    )
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "malformed entry"], root)
    landed_sha = _git(["rev-parse", "HEAD"], root).stdout.strip()

    outcome = _run_tail(root, entry_rel, landed_sha)

    assert outcome.completion_entry_fold_result["acted"] == []
    assert len(outcome.completion_entry_fold_result["failed"]) == 1
    assert m.OP_COMPLETION_ENTRY_FOLD in outcome.completion_entry_fold_result["failed"][0]
