"""
coordinator_core.ops.tests.test_campaign_enumerate — smoke tests for
"records.by_origin_plan" (C21, Item 22).

Coverage:
  (a) op-registered            — "records.by_origin_plan" is in the registry
  (b) cross-type union         — a matching handoff AND a matching debt-queue
                                  entry (different globs, different
                                  ``_load_record`` branches — ``.md`` fenced
                                  vs ``.yaml`` whole-file) are BOTH returned
                                  for one ``origin_plan_id``, non-matching
                                  records of either type are excluded
  (c) projection shape         — each artifact is exactly ``{path, kind,
                                  status}``; ``kind`` is the source record
                                  type, not a frontmatter field
  (d) missing origin_plan_id   — raises ValueError (no unfiltered-superset
                                  silent default)
  (e) no repo_root             — well-formed empty payload, no raise (mirrors
                                  records.query's own contract)

Spec backlink: docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md (C21)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops import records_query
from coordinator_core.ops.campaign_enumerate import _handler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_OP_NAME = "records.by_origin_plan"


def test_op_registered():
    assert len(_REGISTRY) > 0, (
        "registry is empty after 'import coordinator_core.ops' — "
        "all @register_op decorators must have fired at module import time"
    )
    assert _OP_NAME in _REGISTRY, (
        f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
        "coordinator_core.ops.campaign_enumerate @register_op did not fire"
    )


def _make_git_repo(root: Path) -> Path:
    """Create a minimal git repo at ``root`` and return its common_dir (.git path)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root),
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.email", "campaign-enumerate-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.name", "Campaign Enumerate Test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    return (root / ".git").resolve()


@pytest.fixture()
def tmp_repo(tmp_path: Path):
    """A minimal git repo with a matching handoff, a matching debt entry, and
    a non-matching handoff, spanning two record types with different
    ``_load_record`` branches (``.md`` fenced vs ``.yaml`` whole-file).

    Tree:
      state/handoffs/hoff-match.md   — origin_plan_id=plan-abc, status=open
      state/handoffs/hoff-no-match.md — origin_plan_id=plan-xyz
      state/debt-backlog/debt-match.yaml — origin_plan_id=plan-abc, status=open

    Returns (repo_root_git_dir, worktree_root).
    """
    worktree = tmp_path / "repo"
    git_dir = _make_git_repo(worktree)

    handoffs_dir = worktree / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / "hoff-match.md").write_text(
        dedent("""\
            ---
            kind: spinoff-roadmap
            origin_plan_id: plan-abc
            status: open
            ---
            Body content.
        """),
        encoding="utf-8",
    )
    (handoffs_dir / "hoff-no-match.md").write_text(
        dedent("""\
            ---
            kind: spinoff-roadmap
            origin_plan_id: plan-xyz
            status: open
            ---
            Body content.
        """),
        encoding="utf-8",
    )

    debt_dir = worktree / "state" / "debt-backlog"
    debt_dir.mkdir(parents=True, exist_ok=True)
    (debt_dir / "debt-match.yaml").write_text(
        dedent("""\
            id: DSR-2026-09-26-1
            origin_plan_id: plan-abc
            status: open
            title: Fixture debt entry
        """),
        encoding="utf-8",
    )

    return git_dir, worktree


class TestCrossTypeUnion:
    def test_matching_records_returned_across_types(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler({"origin_plan_id": "plan-abc"}, repo_root=git_dir)

        assert result["origin_plan_id"] == "plan-abc"
        artifacts = result["artifacts"]
        paths = {a["path"] for a in artifacts}
        assert "state/handoffs/hoff-match.md" in paths
        assert "state/debt-backlog/debt-match.yaml" in paths
        assert "state/handoffs/hoff-no-match.md" not in paths

    def test_projection_shape(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler({"origin_plan_id": "plan-abc"}, repo_root=git_dir)
        for artifact in result["artifacts"]:
            assert set(artifact) == {"path", "kind", "status"}
            assert artifact["status"] == "open"

        by_path = {a["path"]: a for a in result["artifacts"]}
        assert by_path["state/handoffs/hoff-match.md"]["kind"] == "handoff"
        assert by_path["state/debt-backlog/debt-match.yaml"]["kind"] == "debt"

    def test_no_match_returns_empty_artifacts(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        result = _handler({"origin_plan_id": "plan-does-not-exist"}, repo_root=git_dir)
        assert result == {"origin_plan_id": "plan-does-not-exist", "artifacts": []}


class TestGuards:
    def test_missing_origin_plan_id_raises(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        with pytest.raises(ValueError):
            _handler({}, repo_root=git_dir)

    def test_empty_origin_plan_id_raises(self, tmp_repo):
        git_dir, _worktree = tmp_repo
        with pytest.raises(ValueError):
            _handler({"origin_plan_id": ""}, repo_root=git_dir)

    def test_no_repo_root_empty_payload(self):
        result = _handler({"origin_plan_id": "plan-abc"}, repo_root=None)
        assert result == {"origin_plan_id": "plan-abc", "artifacts": []}


class TestRecordsQueryPrivateCoupling:
    """Pins the underscore-prefixed ``records_query`` internals this module
    reaches across the module boundary (``_TYPE_TO_GLOB``, ``_SYNTHETIC_TYPES``,
    ``_collect_type_records``, ``_RecordsCollectError``) — no stable public
    export exists for them (see this op's module docstring). This test exists
    so a rename/refactor inside ``records_query.py`` fails loud here instead of
    silently breaking ``campaign_enumerate.py`` with no import error.
    """

    def test_relied_on_private_symbols_still_exist(self):
        assert hasattr(records_query, "_TYPE_TO_GLOB")
        assert hasattr(records_query, "_SYNTHETIC_TYPES")
        assert hasattr(records_query, "_collect_type_records")
        assert hasattr(records_query, "_RecordsCollectError")
        assert callable(records_query._collect_type_records)
        assert isinstance(records_query._TYPE_TO_GLOB, dict)
        assert isinstance(records_query._SYNTHETIC_TYPES, frozenset)
