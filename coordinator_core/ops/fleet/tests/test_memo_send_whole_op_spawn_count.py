"""
coordinator_core.ops.fleet.tests.test_memo_send_whole_op_spawn_count

ASSERTION and EXECUTION legs for `memo.send`'s composition-gate enrolment
(`_LEGITIMIZED_SITES`, keyed `"memo.send"`, in
`coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_path.py`).

Spec backlink: docs/plans/2026-09-12-memo-send-enrolled-in-the-composition-gate.md (C1)

Negative spec:
  - This is NOT the killed `test_memo_send_spawn_budget.py` (deleted whole by
    DR-344's kill bar, `c07062c99`; "kill means kill forever"). The functions
    it named do not exist in the rebuilt op and are not restored here.
  - Does NOT count through the `git_native._git` seam. `run_git`
    (`coordinator_core/git/run.py`) reaches git WITHOUT going through `_git`,
    so a `_git`-scoped counter undercounts (see
    `test_commit_authored_new_file.py`'s own module docstring for the exact
    incident: a `_git`-scoped counter read 3 while the leg issued 4
    processes). This file patches `subprocess.Popen` in the `subprocess`
    module itself, seam-independently, and does NOT also patch
    `subprocess.run` (which constructs a `Popen`, so patching both would
    double-count every `run()`-shaped spawn).
  - No budget number here was chosen to agree with any docstring's prose
    claim. Every `spawn_count_budget` value in the manifest and every
    ceiling in the ratchet table is the number this file's own counter
    measured on the fixtures below.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.git import git_state
from coordinator_core.ops.ceremony import git_native

from .test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _budget() -> dict:
    return load_manifest()["overrides"]["memo.send"]["spawn_count_budget"]


def test_green_path_spawn_count_matches_budget_and_is_attributed(tmp_path, monkeypatch):
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, "green-path-topic")

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _memo_send(
            {"dry_run": False, "topic": "green-path-topic"}, repo_root=sender_repo
        )

    assert result["exit_code"] == 0, result
    budget = _budget()
    assert len(spawns) == budget["green_path"], [s.origin for s in spawns]


def test_unspliceable_receiver_index_costs_no_spawn_and_is_left_byte_identical(
    tmp_path, monkeypatch
):
    """A v4 receiver index (a NATURAL fixture precondition) is refused by the
    in-process splice; the delivery never writes the receiver's index, so it
    costs the green-path count and the index bytes do not change."""
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    subprocess.run(
        ["git", "update-index", "--index-version", "4"],
        cwd=str(receiver_repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, "receiver-index-v4-topic")
    index_before = (receiver_repo / ".git" / "index").read_bytes()

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _memo_send(
            {"dry_run": False, "topic": "receiver-index-v4-topic"}, repo_root=sender_repo
        )

    assert result["exit_code"] == 0, result
    assert len(spawns) == _budget()["green_path"], [s.origin for s in spawns]
    assert (receiver_repo / ".git" / "index").read_bytes() == index_before


def test_unreadable_base_tree_refusal_costs_no_spawn(tmp_path, monkeypatch):
    """PLANTED precondition: `read_tree_spine` monkeypatched to return `None`
    in both modules that bind it. No natural fixture makes it return `None`
    (that needs a corrupt base tree, not merely an absent path).

    The delivery reads the upstream base tree in process, so an unreadable one
    is refused loud (AC4) with no `ls-tree` fallback spawn and no spawning
    ladder that would run the receiver's hooks."""
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, "head-spine-unreadable-topic")

    monkeypatch.setattr(git_native, "read_tree_spine", lambda *a, **kw: None)
    monkeypatch.setattr(git_state, "read_tree_spine", lambda *a, **kw: None)

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _memo_send(
            {"dry_run": False, "topic": "head-spine-unreadable-topic"}, repo_root=sender_repo
        )

    assert result["exit_code"] != 0, result
    assert result["acted"] == []
    assert not (
        sender_repo / ".coordinator-local" / "memo-outbox" / "sent" / "head-spine-unreadable-topic.md"
    ).exists()
    inbox_files = [
        p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
        if p.name != ".gitkeep"
    ]
    assert inbox_files == []

    assert len(spawns) == _budget()["green_path"], [s.origin for s in spawns]


def test_receiver_signing_enabled_spawn_count_matches_budget_and_reaches_write_signed_commit_object(
    tmp_path, monkeypatch
):
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "true"],
        cwd=str(receiver_repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, "receiver-signing-topic")

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _memo_send(
            {"dry_run": False, "topic": "receiver-signing-topic"}, repo_root=sender_repo
        )

    assert result["exit_code"] == 0, result
    budget = _budget()
    assert len(spawns) == budget["receiver_signing_enabled"], [s.origin for s in spawns]
    origins = [s.origin for s in spawns]
    assert "unattributed" not in origins, origins
    assert "write_signed_commit_object" in origins, origins
