
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.ops.emit.context import EmitContext


_FAKE_CENTRAL = Path("/fake/state/coordinator_state")
_FAKE_REPO = Path("/meta/repo/root")   # the META-REPO — must NOT be passed as worktree


def _make_ctx() -> EmitContext:
    return EmitContext(
        repo_root=_FAKE_REPO,
        coordinator_root=_FAKE_REPO,
        central_state_root=_FAKE_CENTRAL,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-07-06T00:00:00Z",
        hostname="test-host",
        repo_name="dbc-oduffy/.example-doctrine-mirror-repo",
    )


_FAKE_DAG_ALPHA = {"nodes": [{"stub_id": "alpha-01"}], "edges": [], "roll_up": None, "critical_path": []}
_FAKE_DAG_BETA  = {"nodes": [{"stub_id": "beta-01"}],  "edges": [], "roll_up": None, "critical_path": []}


class TestAssemblerDagMemoization:

    def test_same_roadmap_id_calls_assembler_once(self) -> None:
        ctx = _make_ctx()
        mock_result = _FAKE_DAG_ALPHA.copy()

        with patch(
            "coordinator_core.ops.emit.context.EmitContext.assembler_dag.__wrapped__"
            if hasattr(EmitContext.assembler_dag, "__wrapped__") else
            "coordinator_core.ops.roadmap_dag.assemble_roadmap_dag",
            return_value=mock_result,
        ) as mock_assemble:
            result_1 = ctx.assembler_dag("roadmap-alpha")
            result_2 = ctx.assembler_dag("roadmap-alpha")

        assert mock_assemble.call_count == 1, (
            "assemble_roadmap_dag should be called once; second call must return cached result"
        )
        assert result_1 is result_2, "Both calls must return the identical cached object"

    def test_distinct_roadmap_ids_each_compute_independently(self) -> None:
        ctx = _make_ctx()
        call_order: list[str] = []

        def _side_effect(roadmap_id: str, worktree_root: Path) -> dict:
            call_order.append(roadmap_id)
            return _FAKE_DAG_ALPHA.copy() if roadmap_id == "roadmap-alpha" else _FAKE_DAG_BETA.copy()

        with patch(
            "coordinator_core.ops.roadmap_dag.assemble_roadmap_dag",
            side_effect=_side_effect,
        ) as mock_assemble:
            alpha_1 = ctx.assembler_dag("roadmap-alpha")
            beta_1  = ctx.assembler_dag("roadmap-beta")
            alpha_2 = ctx.assembler_dag("roadmap-alpha")
            beta_2  = ctx.assembler_dag("roadmap-beta")

        assert mock_assemble.call_count == 2, (
            "Two distinct roadmap_ids → two assembler calls; cached on subsequent access"
        )
        assert call_order == ["roadmap-alpha", "roadmap-beta"], (
            "Each id computed exactly once in first-access order"
        )
        assert alpha_1 is alpha_2, "roadmap-alpha: cached object returned on second call"
        assert beta_1  is beta_2,  "roadmap-beta: cached object returned on second call"

    def test_cache_is_instance_level_not_shared_across_contexts(self) -> None:
        ctx_a = _make_ctx()
        ctx_b = _make_ctx()

        fake_a = {"nodes": [{"stub_id": "ctx-a"}], "edges": [], "roll_up": None, "critical_path": []}
        fake_b = {"nodes": [{"stub_id": "ctx-b"}], "edges": [], "roll_up": None, "critical_path": []}
        results = iter([fake_a, fake_b])

        with patch(
            "coordinator_core.ops.roadmap_dag.assemble_roadmap_dag",
            side_effect=lambda *a, **kw: next(results),
        ) as mock_assemble:
            r_a = ctx_a.assembler_dag("roadmap-shared")
            r_b = ctx_b.assembler_dag("roadmap-shared")

        assert mock_assemble.call_count == 2, (
            "Each context instance computes independently; no cross-instance cache"
        )
        assert r_a is not r_b, "ctx_a and ctx_b hold separate cached objects"


class TestAssemblerDagWorktreeRoot:

    def test_worktree_root_is_central_state_root_parent(self) -> None:
        ctx = _make_ctx()
        expected_worktree = _FAKE_CENTRAL.parent

        with patch(
            "coordinator_core.ops.roadmap_dag.assemble_roadmap_dag",
            return_value=_FAKE_DAG_ALPHA.copy(),
        ) as mock_assemble:
            ctx.assembler_dag("roadmap-worktree-check")

        mock_assemble.assert_called_once_with(
            "roadmap-worktree-check",
            worktree_root=expected_worktree,
        )

    def test_worktree_root_is_not_repo_root(self) -> None:
        ctx = _make_ctx()

        captured: dict = {}

        def _capture(roadmap_id: str, worktree_root: Path) -> dict:
            captured["worktree_root"] = worktree_root
            return _FAKE_DAG_ALPHA.copy()

        with patch(
            "coordinator_core.ops.roadmap_dag.assemble_roadmap_dag",
            side_effect=_capture,
        ):
            ctx.assembler_dag("roadmap-meta-repo-check")

        assert captured["worktree_root"] != _FAKE_REPO, (
            "worktree_root must NOT be repo_root (the meta-repo); "
            "passing repo_root silently scans the wrong tree and returns zero stubs"
        )
        assert captured["worktree_root"] == _FAKE_CENTRAL.parent, (
            "worktree_root must be central_state_root.parent (the claude-klabauter working tree)"
        )
