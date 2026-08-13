"""
coordinator_core.hooks.test_subagent_zero_tool_use_surface — round-trip tests for
the Stage-2 pure-read surfacing op (hooks.subagent_zero_tool_use_surface).

Covers: missing store → empty-result shape (store_present: false); a populated store
with N zero-tool-use records → all N returned in append order; a store containing a
foreign `kind` value alongside zero-tool-use records → the foreign-kind record is
filtered out (not counted in skipped_lines, since it parsed correctly); malformed
lines are skipped and counted in skipped_lines; the exact pinned return shape
({"records", "record_count", "skipped_lines", "store_present"}); registration-quad
presence for the op key.

All handlers are async; asyncio.run() is used directly in sync test functions — no
pytest-asyncio dependency, matching coordinator_core/tests/test_hooks_bookkeeping.py.

Spec backlink: cross-repo/inbox/2026-07-25-coordinator-claude-em-zero-tool-use-detection-engine-op-contract.md
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


def _run(coro):
    return asyncio.run(coro)


class _FakeCtx:
    def __init__(self, repo_root: str) -> None:
        self.repo_root = repo_root


def _cs_dir(repo_root: Path) -> Path:
    return repo_root / ".git" / "coordinator-sessions"


def _ctx(tmp_path: Path) -> _FakeCtx:
    _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    return _FakeCtx(str(tmp_path / ".git"))


def _store_path(tmp_path: Path, sid: str) -> Path:
    return _cs_dir(tmp_path) / sid / "subagent-zero-tool-use.jsonl"


def _write_store(tmp_path: Path, sid: str, lines: list[str]) -> Path:
    store = _store_path(tmp_path, sid)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("\n".join(lines) + ("\n" if lines else ""))
    return store


def _zt_record(agent_id: str, count: int) -> str:
    return json.dumps({
        "kind": "zero-tool-use",
        "session_id": "irrelevant-for-fixture",
        "agent_id": agent_id,
        "agent_type": "executor",
        "tool_use_count": count,
        "recorded_at": "2026-07-25T00:00:00Z",
    })


class TestMissingStore:
    def test_missing_store_returns_empty_result_shape(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_surface import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-missing-01"
        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PreToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result == {
            "records": [],
            "record_count": 0,
            "skipped_lines": 0,
            "store_present": False,
        }

    def test_missing_repo_root_returns_empty_result(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_surface import _handler
        result = _run(_handler(
            {"session_id": "sess-no-repo", "hook_event_name": "PreToolUse"},
            repo_root=None,
        ))
        assert result["store_present"] is False
        assert result["records"] == []

    def test_missing_session_id_returns_empty_result(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_surface import _handler
        ctx = _ctx(tmp_path)
        result = _run(_handler(
            {"session_id": "", "hook_event_name": "PreToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["store_present"] is False


class TestPopulatedStore:
    def test_n_records_returned_in_append_order(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_surface import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-populated-01"
        _write_store(tmp_path, sid, [
            _zt_record("agent-1", 0),
            _zt_record("agent-2", 3),
            _zt_record("agent-3", 1),
        ])
        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PreToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["store_present"] is True
        assert result["record_count"] == 3
        assert result["skipped_lines"] == 0
        assert [r["agent_id"] for r in result["records"]] == ["agent-1", "agent-2", "agent-3"]

    def test_foreign_kind_filtered_out_not_counted_as_skipped(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_surface import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-foreign-kind-01"
        foreign = json.dumps({
            "kind": "some-other-detector",
            "session_id": "irrelevant",
            "payload": "unrelated",
        })
        _write_store(tmp_path, sid, [
            _zt_record("agent-1", 0),
            foreign,
            _zt_record("agent-2", 2),
        ])
        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PreToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["record_count"] == 2
        assert result["skipped_lines"] == 0, "a valid-but-foreign-kind line is not a parse failure"
        assert [r["agent_id"] for r in result["records"]] == ["agent-1", "agent-2"]

    def test_malformed_lines_skipped_and_counted(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_surface import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-malformed-surface-01"
        _write_store(tmp_path, sid, [
            "not json {{{",
            _zt_record("agent-1", 1),
            json.dumps(["a", "list", "not", "a", "dict"]),
            "",
            _zt_record("agent-2", 0),
        ])
        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PreToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["record_count"] == 2
        assert result["skipped_lines"] == 2
        assert [r["agent_id"] for r in result["records"]] == ["agent-1", "agent-2"]


class TestReturnShapePinned:
    def test_exact_top_level_keys(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_surface import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-shape-pin-01"
        _write_store(tmp_path, sid, [_zt_record("agent-1", 0)])
        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PreToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert set(result.keys()) == {"records", "record_count", "skipped_lines", "store_present"}


class TestRegistrationQuad:
    def test_op_present_in_all_four_surfaces(self) -> None:
        from coordinator_core.hooks import subagent_zero_tool_use_surface  # noqa: F401 — self-registers
        from coordinator_core.ipc import _REGISTRY
        from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
        from coordinator_core.op_scopes import _OP_KEY_SCOPE
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        op_key = "hooks.subagent_zero_tool_use_surface"
        assert op_key in _REGISTRY
        assert OP_CLASSIFICATION[op_key] == OpClass.COMPUTE_ONLY
        assert _OP_KEY_SCOPE[op_key] == "common_dir"
        assert OP_MODULE_MAP[op_key] == "coordinator_core.hooks"
