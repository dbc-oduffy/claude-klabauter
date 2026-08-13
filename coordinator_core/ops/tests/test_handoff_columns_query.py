"""
coordinator_core.ops.tests.test_handoff_columns_query — tests for "handoff.columns" (C3).

Purpose: Verify the row shape (path plus exactly the four cockpit columns
plus ``baton_class``), archive-coverage opt-in, comment-contamination
cleanliness (AC5), the ``baton_class`` derivation (real kind resolves,
absent/unknown kind yields None), and the O(1) git-log subprocess-spawn
budget for a multi-record corpus.

Import guard: ``import coordinator_core.ops`` MUST precede all test functions
so that ALL op registrations fire before any test assertion (mirrors
test_records_query.py's own import-guard convention).

Coverage:
  (a) row shape is exactly the six keys: path, status, deployment_state,
      predecessor, shipped_in, baton_class — nothing else.
  (b) an archived record with deployment_state=shipped appears by default
      (archive coverage defaults ON for this op — Review: unlike
      records.query's AC2 default-off floor, this op has exactly one
      intended consumer whose purpose is showing shipped work), and is
      absent when archive=False is explicitly requested.
  (c) AC5 — a trailing `#` comment on shipped_in/deployment_state yields a
      clean value, built from this repo's real archived-corpus shapes
      (`shipped_in: 99c30e8  # code ops (...)`,
      `deployment_state: shipped  # crash reconstruction clears t`).
  (d) the git-log subprocess spawn count for a multi-record query is O(1),
      asserted via a monkeypatched subprocess.run counter.

Spec backlink: pln-a-pull-surface-for-cockpit-the-b8e2f3 § C3
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects, including
# "handoff.columns". MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_columns_query import _handler
from coordinator_core.ops.emit.sections import handoff_columns as handoff_columns_mod

# Real-git spawn is load-bearing: coverage (d) asserts the O(1) git-log
# subprocess spawn count for a multi-record query, a budget claim that a
# mocked git would trivially satisfy without proving anything. Per-test
# isolation via tmp_path fixtures, not hoisted. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_OP_NAME = "handoff.columns"

assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_columns_query @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_git_repo(root: Path) -> Path:
    """Create a minimal git repo at ``root`` and return its common_dir (.git path)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "handoff-columns-query-test@claude-klabauter.test"],
        cwd=str(root), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Handoff Columns Query Test"],
        cwd=str(root), capture_output=True, check=True,
    )
    return (root / ".git").resolve()


def _write_md(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def tmp_repo(tmp_path: Path):
    """A minimal git repo with one live handoff and one archived (shipped) handoff.

    Tree:
      state/handoffs/hoff-live.md                       — status=open, awaiting_gate
      archive/handoffs/2026-08/hoff-archived-shipped.md — deployment_state: shipped,
                                                            with a trailing `#` comment
                                                            on both deployment_state and
                                                            shipped_in (AC5 fixture).

    Returns (git_dir, worktree_root).
    """
    worktree = tmp_path / "repo"
    git_dir = _make_git_repo(worktree)

    _write_md(
        worktree / "state" / "handoffs" / "hoff-live.md",
        dedent("""\
            ---
            status: open
            deployment_state: awaiting_gate
            kind: session-handoff
            ---
            Live handoff body.
        """),
    )

    _write_md(
        worktree / "archive" / "handoffs" / "2026-08" / "hoff-archived-shipped.md",
        dedent("""\
            ---
            status: claimed
            deployment_state: shipped  # crash reconstruction clears t
            shipped_in: 99c30e8  # code ops (C0b/C1/C2/C5)
            ---
            Archived shipped handoff body.
        """),
    )

    return git_dir, worktree


# ---------------------------------------------------------------------------
# (a) Row shape
# ---------------------------------------------------------------------------


class TestRowShape:
    def test_row_carries_exactly_six_keys(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": False}, repo_root=git_dir)
        records = result["records"]
        assert len(records) == 1  # archive opted out — only the live handoff
        row = records[0]
        assert set(row.keys()) == {
            "path", "status", "deployment_state", "predecessor", "shipped_in",
            "baton_class",
        }


# ---------------------------------------------------------------------------
# baton_class derivation
# ---------------------------------------------------------------------------


class TestBatonClassDerivation:
    def test_known_kind_resolves_to_its_class(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": False}, repo_root=git_dir)
        row = result["records"][0]
        assert row["path"].endswith("hoff-live.md")
        # kind: session-handoff -> baton_class: continuation, per the
        # vendored handoff.schema.json's x-baton-class.mapping.
        assert row["baton_class"] == "continuation"

    def test_absent_kind_yields_none_not_a_raise(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": True}, repo_root=git_dir)
        matching = [
            row for row in result["records"] if "hoff-archived-shipped" in row["path"]
        ][0]
        # The archived fixture carries no `kind:` key — must resolve to
        # None, never raise.
        assert matching["baton_class"] is None


# ---------------------------------------------------------------------------
# (b) Archive opt-in
# ---------------------------------------------------------------------------


class TestArchiveCoverageOptOut:
    def test_archived_shipped_record_appears_by_default(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={}, repo_root=git_dir)
        paths = {row["path"] for row in result["records"]}
        assert any("hoff-archived-shipped" in p for p in paths)
        matching = [
            row for row in result["records"] if "hoff-archived-shipped" in row["path"]
        ][0]
        assert matching["deployment_state"] == "shipped"

    def test_archived_shipped_record_absent_with_archive_false(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": False}, repo_root=git_dir)
        paths = {row["path"] for row in result["records"]}
        assert not any("hoff-archived-shipped" in p for p in paths)


# ---------------------------------------------------------------------------
# (c) AC5 — comment contamination
# ---------------------------------------------------------------------------


class TestCommentContaminationCleanValues:
    def test_trailing_comment_stripped_from_deployment_state_and_shipped_in(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler(params={"archive": True}, repo_root=git_dir)
        matching = [
            row for row in result["records"] if "hoff-archived-shipped" in row["path"]
        ][0]

        # deployment_state must be the clean token, no trailing comment text.
        assert matching["deployment_state"] == "shipped"
        assert "#" not in str(matching["deployment_state"])
        assert "crash" not in str(matching["deployment_state"])

        # shipped_in must be a clean {sha, date} dict (or None if the SHA is
        # unresolvable in this throwaway repo) — never a raw comment-carrying
        # string, and never containing the comment text.
        shipped_in = matching["shipped_in"]
        if shipped_in is not None:
            assert shipped_in["sha"] == "99c30e8"
            assert "#" not in shipped_in["sha"]
            assert "code ops" not in shipped_in["sha"]


# ---------------------------------------------------------------------------
# (d) O(1) git-log subprocess spawn count
# ---------------------------------------------------------------------------


class TestGitLogSpawnBudget:
    def test_multi_record_query_spawns_git_log_at_most_once(self, tmp_repo, monkeypatch):
        git_dir, worktree = tmp_repo

        # Add a second archived record with its own shipped_in SHA, so a
        # naive per-record loop would spawn 2 git-log calls where the batch
        # path spawns exactly 1.
        _write_md(
            worktree / "archive" / "handoffs" / "2026-08" / "hoff-archived-shipped-2.md",
            dedent("""\
                ---
                status: claimed
                deployment_state: shipped
                shipped_in: abc1234
                ---
                Second archived shipped handoff body.
            """),
        )

        real_run = subprocess.run
        call_count = {"n": 0}

        def _counting_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) > 2 and cmd[0] == "git" and "log" in cmd:
                call_count["n"] += 1
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(handoff_columns_mod.subprocess, "run", _counting_run)

        result = _handler(params={"archive": True}, repo_root=git_dir)

        assert len(result["records"]) == 3  # 1 live + 2 archived
        assert call_count["n"] <= 1, (
            f"expected at most 1 'git log' spawn for the whole batch, got {call_count['n']}"
        )
