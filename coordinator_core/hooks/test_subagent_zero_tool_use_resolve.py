"""
coordinator_core.hooks.test_subagent_zero_tool_use_resolve — round-trip tests for
the Stage-3 pull/poll per-agent resolve op (hooks.subagent_zero_tool_use_resolve).

Covers: (a) a matching record with tool_use_count > 0 resolves "did-work"; (b) a
matching record with tool_use_count == 0 resolves "zero-tool-use"; (c) a missing or
unreadable store, a missing agent_id/session_id/repo_root, an absent-for-this-agent
record, and a malformed tool_use_count all resolve "unknown" with a specific reason
naming the store path and cause — never a bare/silent pass. Also covers last-match-
wins on a re-dispatched agent_id, and registration-quad presence for the op key.

All handlers are async; asyncio.run() is used directly in sync test functions — no
pytest-asyncio dependency, matching coordinator_core/hooks/test_subagent_zero_tool_use_surface.py.

Spec backlink: cross-repo/inbox/2026-07-25-doe-claude-em-zero-tool-use-detection-verdict-viable.md
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


def _zt_record(agent_id: str, count) -> str:
    return json.dumps({
        "kind": "zero-tool-use",
        "session_id": "irrelevant-for-fixture",
        "agent_id": agent_id,
        "agent_type": "executor",
        "tool_use_count": count,
        "recorded_at": "2026-07-25T00:00:00Z",
    })


class TestDidWork:
    def test_matching_record_nonzero_count_resolves_did_work(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-didwork-01"
        _write_store(tmp_path, sid, [_zt_record("agent-1", 3)])
        result = _run(_handler(
            {"session_id": sid, "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "did-work"
        assert result["tool_use_count"] == 3
        assert "agent-1" in result["reason"]
        assert "real work" in result["reason"] or "no action needed" in result["reason"]

    def test_ignores_records_for_other_agents(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-didwork-02"
        _write_store(tmp_path, sid, [_zt_record("agent-other", 5), _zt_record("agent-1", 2)])
        result = _run(_handler(
            {"session_id": sid, "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "did-work"
        assert result["tool_use_count"] == 2

    def test_last_matching_record_wins_on_redispatch(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-didwork-03"
        _write_store(tmp_path, sid, [_zt_record("agent-1", 0), _zt_record("agent-1", 4)])
        result = _run(_handler(
            {"session_id": sid, "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "did-work"
        assert result["tool_use_count"] == 4


class TestZeroToolUse:
    def test_matching_record_zero_count_resolves_zero_tool_use(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-zero-01"
        _write_store(tmp_path, sid, [_zt_record("agent-1", 0)])
        result = _run(_handler(
            {"session_id": sid, "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "zero-tool-use"
        assert result["tool_use_count"] == 0
        assert "redispatch" in result["reason"]


class TestUnknownMissingInputs:
    def test_missing_agent_id_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        result = _run(_handler(
            {"session_id": "sess-x", "agent_id": "", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "unknown"
        assert "agent_id" in result["reason"]

    def test_missing_session_id_resolves_unknown_names_agent(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        result = _run(_handler(
            {"session_id": "", "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "unknown"
        assert "agent-1" in result["reason"]
        assert "session_id" in result["reason"]

    def test_missing_repo_root_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        result = _run(_handler(
            {"session_id": "sess-x", "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=None,
        ))
        assert result["verdict"] == "unknown"
        assert "repo_root" in result["reason"]


class TestUnknownStoreOrRecordAbsent:
    def test_missing_store_resolves_unknown_names_path_and_trigger_loss(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-missing-store-01"
        result = _run(_handler(
            {"session_id": sid, "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "unknown"
        expected_path = str(_store_path(tmp_path, sid))
        assert expected_path in result["reason"]
        assert result["store_path"] == expected_path
        assert "trigger-loss" in result["reason"]
        assert "redispatch" in result["reason"] or "verify" in result["reason"]

    def test_store_present_but_no_record_for_this_agent_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-no-agent-record-01"
        _write_store(tmp_path, sid, [_zt_record("agent-other", 1)])
        result = _run(_handler(
            {"session_id": sid, "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "unknown"
        assert "agent-1" in result["reason"]
        assert "trigger-loss" in result["reason"]

    def test_unreadable_store_resolves_unknown_names_reason(self, tmp_path: Path, monkeypatch) -> None:
        from coordinator_core.hooks import subagent_zero_tool_use_resolve as mod
        ctx = _ctx(tmp_path)
        sid = "sess-unreadable-01"
        store = _write_store(tmp_path, sid, [_zt_record("agent-1", 1)])

        real_open = open

        def _boom(path, *args, **kwargs):
            if str(path) == str(store):
                raise OSError("permission denied (simulated)")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _boom)
        result = _run(mod._handler(
            {"session_id": sid, "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "unknown"
        assert str(store) in result["reason"]
        assert "permission denied" in result["reason"]

    def test_malformed_tool_use_count_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-malformed-count-01"
        bad = json.dumps({
            "kind": "zero-tool-use",
            "session_id": "irrelevant",
            "agent_id": "agent-1",
            "agent_type": "executor",
            "tool_use_count": "not-an-int",
            "recorded_at": "2026-07-25T00:00:00Z",
        })
        _write_store(tmp_path, sid, [bad])
        result = _run(_handler(
            {"session_id": sid, "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert result["verdict"] == "unknown"
        assert "non-integer" in result["reason"]


class TestReturnShapePinned:
    def test_exact_top_level_keys(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-shape-pin-01"
        _write_store(tmp_path, sid, [_zt_record("agent-1", 0)])
        result = _run(_handler(
            {"session_id": sid, "agent_id": "agent-1", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert set(result.keys()) == {"verdict", "agent_id", "tool_use_count", "reason", "store_path"}


class TestRegistrationQuad:
    def test_op_present_in_all_four_surfaces(self) -> None:
        from coordinator_core.hooks import subagent_zero_tool_use_resolve  # noqa: F401 — self-registers
        from coordinator_core.ipc import _REGISTRY
        from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
        from coordinator_core.op_scopes import _OP_KEY_SCOPE
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        op_key = "hooks.subagent_zero_tool_use_resolve"
        assert op_key in _REGISTRY
        assert OP_CLASSIFICATION[op_key] == OpClass.COMPUTE_ONLY
        assert _OP_KEY_SCOPE[op_key] == "common_dir"
        assert OP_MODULE_MAP[op_key] == "coordinator_core.hooks"
