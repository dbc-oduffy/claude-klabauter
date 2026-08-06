"""Tests for coordinator_core.ops.emit_cadence — the "emit.cadence" composite sequencer.

Covers:
  - Fail-loud on repo_root=None (AC5), mirroring artifact.emit / backlog.record.
  - Handler derives main_worktree_root(repo_root) and resolves ONE EmitContext, reused
    for both sub-ops (no double context resolution).
  - Ordering is load-bearing: record(ctx) is called strictly before envelope.emit(ctx, ...),
    and both receive the SAME ctx object.
  - Real-effect end-to-end: a genuine tmp-repo invocation writes today's backlog-snapshot
    shard row AND the aggregated cockpit-emission.json reflects that same-invocation row
    (the aggregate saw the row the same call just wrote) — proving the invariant this op
    exists to guarantee, not just that two mocks were called in order.

Spec backlink: cross-repo/inbox/2026-07-11-claude-central-em-backlog-record-cadence-relocation-reconcile.md
§ EM Response (Decision B — claude-klabauter control-plane adjacency).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.ops.emit.context import EmitContext


def _run(result):
    """Run an async coroutine synchronously, or pass through a plain value.

    _emit_cadence is a plain (sync) handler as of the C3/AC-3 dispatch-interruptibility
    fix (async def with no internal await left the DISPATCH_TIMEOUT_SECS runaway guard
    inert — ipc.dispatch_message only offloads sync handlers to asyncio.to_thread, so a
    never-yielding async def stalled the event loop for its full blocking duration; see
    ipc.py's iscoroutinefunction branch). This helper stays name-stable across that
    signature change so callers below don't need to care whether the handler is sync or
    async.
    """
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _fake_ctx(tmp_path: Path, repo_name: str = "test-org/test-repo") -> MagicMock:
    """Build a minimal EmitContext-shaped mock for handler-wiring tests."""
    ctx = MagicMock(spec=EmitContext)
    ctx.repo_root = tmp_path
    ctx.central_state_root = tmp_path / "state"
    ctx.repo_name = repo_name
    ctx.hostname = "test-host"
    return ctx


# ---------------------------------------------------------------------------
# Fail-loud on None repo_root (AC5)
# ---------------------------------------------------------------------------

class TestEmitCadenceFailLoudOnNone:
    """_emit_cadence raises ValueError when repo_root is None (AC5)."""

    def test_none_repo_root_raises_value_error(self) -> None:
        """Handler must raise ValueError when repo_root is None — not fall back to meta-repo."""
        from coordinator_core.ops.emit_cadence import _emit_cadence

        with pytest.raises(ValueError, match="_origin_worktree"):
            _run(_emit_cadence({}, repo_root=None))

    def test_none_repo_root_error_mentions_ac5(self) -> None:
        """Error message must mention no silent fallback to meta-repo."""
        from coordinator_core.ops.emit_cadence import _emit_cadence

        with pytest.raises(ValueError, match="No silent fallback"):
            _run(_emit_cadence({}, repo_root=None))


# ---------------------------------------------------------------------------
# Context derivation: ONE resolve_context call, reused for both sub-ops
# ---------------------------------------------------------------------------

class TestEmitCadenceContextDerivation:
    """Handler derives main_worktree_root(common_dir), resolves ctx ONCE, reuses it."""

    def test_handler_calls_resolve_context_with_derived_root_once(self, tmp_path: Path) -> None:
        """resolve_context is called exactly once, with the DERIVED root (not bare repo_root)."""
        from coordinator_core.ops.emit_cadence import _emit_cadence

        main_worktree = tmp_path / "main_worktree"
        common_dir = main_worktree / ".git"  # main_worktree_root(common_dir) = main_worktree

        fake_ctx = _fake_ctx(main_worktree)
        fake_record_result = {"shard": "x", "machine": "m", "date": "d", "rows": []}
        fake_emit_result = {"ok": True, "out": str(tmp_path / "out.json"), "schema_version": "test"}

        with patch(
            "coordinator_core.ops.emit_cadence.resolve_context",
            return_value=fake_ctx,
        ) as mock_resolve, patch(
            "coordinator_core.ops.emit_cadence.record",
            return_value=fake_record_result,
        ) as mock_record, patch(
            "coordinator_core.ops.emit_cadence.envelope.emit",
            return_value=fake_emit_result,
        ) as mock_emit:
            result = _run(_emit_cadence({}, repo_root=common_dir))

        mock_resolve.assert_called_once_with(main_worktree)
        mock_record.assert_called_once_with(fake_ctx)
        mock_emit.assert_called_once_with(fake_ctx, out=None)
        assert result == {"backlog_record": fake_record_result, "artifact_emit": fake_emit_result}

    def test_handler_passes_out_param_to_emit_only(self, tmp_path: Path) -> None:
        """'out' param is forwarded to envelope.emit, not to record (which takes no out param)."""
        from coordinator_core.ops.emit_cadence import _emit_cadence

        common_dir = tmp_path / ".git"
        out_path = str(tmp_path / "custom-out.json")
        fake_ctx = _fake_ctx(tmp_path)

        with patch(
            "coordinator_core.ops.emit_cadence.resolve_context",
            return_value=fake_ctx,
        ), patch(
            "coordinator_core.ops.emit_cadence.record",
            return_value={"shard": "x", "machine": "m", "date": "d", "rows": []},
        ), patch(
            "coordinator_core.ops.emit_cadence.envelope.emit",
            return_value={"ok": True},
        ) as mock_emit:
            _run(_emit_cadence({"out": out_path}, repo_root=common_dir))

        mock_emit.assert_called_once_with(fake_ctx, out=out_path)


# ---------------------------------------------------------------------------
# Ordering is load-bearing: record() BEFORE envelope.emit(), same ctx object
# ---------------------------------------------------------------------------

class TestEmitCadenceOrdering:
    """record(ctx) must run strictly before envelope.emit(ctx, ...); both share one ctx."""

    def test_record_called_before_emit_with_shared_ctx(self, tmp_path: Path) -> None:
        """Spy-based ordering assertion: record then emit, same ctx object identity."""
        from coordinator_core.ops.emit_cadence import _emit_cadence

        common_dir = tmp_path / ".git"
        fake_ctx = _fake_ctx(tmp_path)
        call_order: list[str] = []
        seen_ctx_ids: list[int] = []

        def _spy_record(ctx):
            call_order.append("record")
            seen_ctx_ids.append(id(ctx))
            return {"shard": "x", "machine": "m", "date": "d", "rows": []}

        def _spy_emit(ctx, out=None):
            call_order.append("emit")
            seen_ctx_ids.append(id(ctx))
            return {"ok": True}

        with patch(
            "coordinator_core.ops.emit_cadence.resolve_context",
            return_value=fake_ctx,
        ), patch(
            "coordinator_core.ops.emit_cadence.record",
            side_effect=_spy_record,
        ), patch(
            "coordinator_core.ops.emit_cadence.envelope.emit",
            side_effect=_spy_emit,
        ):
            _run(_emit_cadence({}, repo_root=common_dir))

        assert call_order == ["record", "emit"], (
            f"emit.cadence must call record() before envelope.emit(); got order {call_order}"
        )
        assert seen_ctx_ids[0] == seen_ctx_ids[1] == id(fake_ctx), (
            "record() and envelope.emit() must receive the SAME ctx object "
            "(one resolve_context() call, reused — not re-resolved per sub-op)"
        )


# ---------------------------------------------------------------------------
# Real-effect end-to-end: the aggregate actually sees the row this call wrote
# ---------------------------------------------------------------------------

class TestEmitCadenceRealEffectOrdering:
    """A genuine invocation: today's shard row is written AND the emitted artifact
    reflects it — the aggregate saw the row the same invocation just wrote.
    """

    def _make_real_ctx(self, tmp_path: Path) -> EmitContext:
        """Build a real EmitContext rooted at tmp_path (no subprocess/git calls needed by
        record()/emit() themselves — build(ctx) iterates the section registry, which is
        graceful-absent when the emitting repo has none of the section source paths).
        """
        return EmitContext(
            repo_root=tmp_path,
            coordinator_root=tmp_path,
            central_state_root=tmp_path / "state",
            git_branch="main",
            git_sha="abc1234567890123456789012345678901234567",
            git_sha_short="abc12345",
            observed_at="2026-07-11T00:00:00Z",
            hostname="test-host",
            repo_name="test-org/emit-cadence-repo",
        )

    def test_real_invocation_aggregate_sees_freshly_recorded_row(self, tmp_path: Path) -> None:
        """cockpit-emission.json's backlog_history section includes today's row that THIS
        same emit.cadence invocation just wrote to the shard — proving record-before-
        aggregate ordering has a real, observable effect (not just a mock call order).
        """
        from coordinator_core.ops.emit_cadence import _emit_cadence
        from coordinator_core.ops.emit._slug import machine_slug

        main_worktree = tmp_path / "main_worktree"
        (main_worktree / "state").mkdir(parents=True)
        common_dir = main_worktree / ".git"

        real_ctx = self._make_real_ctx(main_worktree)

        with patch(
            "coordinator_core.ops.emit_cadence.resolve_context",
            return_value=real_ctx,
        ):
            result = _run(_emit_cadence({}, repo_root=common_dir))

        # 1. The backlog shard row for today was actually written to disk.
        shard = real_ctx.central_state_root / f"backlog-snapshots.{machine_slug('test-host')}.jsonl"
        assert shard.exists(), "emit.cadence must write the backlog-snapshot shard (record step)"
        shard_lines = [ln for ln in shard.read_text().splitlines() if ln.strip()]
        assert len(shard_lines) == 1
        shard_row = json.loads(shard_lines[0])
        assert shard_row["repo"] == "test-org/emit-cadence-repo"

        # 2. The cockpit-emission.json artifact was written by the SAME invocation.
        emitted_path = Path(result["artifact_emit"]["out"])
        assert emitted_path.exists(), "emit.cadence must write cockpit-emission.json (emit step)"
        envelope_data = json.loads(emitted_path.read_text())

        # 3. The aggregate's top-level backlog_history.series block reflects the row just
        #    recorded — the aggregation ran AFTER the record write and picked it up (same
        #    shard file, same process, same invocation).
        backlog_history = envelope_data.get("backlog_history")
        assert backlog_history is not None, (
            "cockpit-emission.json must have a 'backlog_history' key"
        )
        series = backlog_history.get("series", [])
        repo_series = [s for s in series if s.get("repo") == "test-org/emit-cadence-repo"]
        assert repo_series, (
            "backlog_history.series must include this repo — the aggregate must have seen "
            "the row THIS invocation just recorded"
        )
        points = repo_series[0].get("points", [])
        matches = [p for p in points if p.get("date") == shard_row["date"]]
        assert matches, (
            "backlog_history.series[].points must include today's date — the aggregate must "
            "have seen the row THIS invocation just recorded (record-before-aggregate ordering)"
        )

        assert result["backlog_record"]["rows"][0] == shard_row
