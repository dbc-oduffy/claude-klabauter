"""
coordinator_core.hooks.test_subagent_zero_tool_use — round-trip tests for the
Stage-1 zero-tool-use write op (hooks.subagent_zero_tool_use).

Covers: verified-zero and verified-N counts durable-written; absent transcript
resolves to UNKNOWN with NO store-file creation (the load-bearing rule from the
Coordinator-claude contract memo); a present-but-unreadable transcript (a directory in the
transcript's place, exercised without any chmod so it holds on Windows too) also
resolves to UNKNOWN with no write; malformed/partial JSONL lines are tolerated and
skipped without aborting the count; exact record field names + order; append
ordering across two calls; registration-quad presence (OP_CLASSIFICATION,
_OP_KEY_SCOPE, OP_MODULE_MAP) for the op key.

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


def _assistant_line(tool_use_count: int, extra_text_blocks: int = 0) -> str:
    """Build a JSONL line shaped like a real transcript assistant record."""
    content = [{"type": "text", "text": "hi"}] * extra_text_blocks
    content += [{"type": "tool_use", "name": "Bash", "input": {}} for _ in range(tool_use_count)]
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}})


# ---------------------------------------------------------------------------
# Verified counts — durable write
# ---------------------------------------------------------------------------

class TestVerifiedCounts:
    def test_verified_zero_writes_record_with_zero_count(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use import _handler
        ctx = _ctx(tmp_path)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_assistant_line(0, extra_text_blocks=2) + "\n")
        sid = "sess-zero-0001"
        result = _run(_handler(
            {
                "session_id": sid,
                "agent_id": "agent-0001",
                "agent_type": "executor",
                "agent_transcript_path": str(transcript),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        store = _store_path(tmp_path, sid)
        assert store.exists()
        record = json.loads(store.read_text().strip())
        assert record["tool_use_count"] == 0

    def test_verified_n_writes_correct_count(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use import _handler
        ctx = _ctx(tmp_path)
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            _assistant_line(2),
            json.dumps({"type": "user", "message": {"content": "ok"}}),
            _assistant_line(3, extra_text_blocks=1),
        ]
        transcript.write_text("\n".join(lines) + "\n")
        sid = "sess-verified-n"
        _run(_handler(
            {
                "session_id": sid,
                "agent_id": "agent-000n",
                "agent_type": "executor",
                "agent_transcript_path": str(transcript),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        store = _store_path(tmp_path, sid)
        record = json.loads(store.read_text().strip())
        assert record["tool_use_count"] == 5


# ---------------------------------------------------------------------------
# UNKNOWN — the load-bearing rule
# ---------------------------------------------------------------------------

class TestUnknownNeverZero:
    def test_absent_transcript_is_unknown_no_store_created(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use import _handler
        ctx = _ctx(tmp_path)
        missing = tmp_path / "does-not-exist.jsonl"
        sid = "sess-absent-0001"
        result = _run(_handler(
            {
                "session_id": sid,
                "agent_id": "agent-absent",
                "agent_type": "executor",
                "agent_transcript_path": str(missing),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        store = _store_path(tmp_path, sid)
        assert not store.exists(), "store file MUST NOT be created on UNKNOWN"
        assert not (_cs_dir(tmp_path) / sid).exists(), (
            "no per-session bookkeeping dir should be created on UNKNOWN"
        )

    def test_unreadable_transcript_directory_in_place_is_unknown_no_write(self, tmp_path: Path) -> None:
        """A directory sitting at agent_transcript_path — platform-neutral 'unreadable'
        substitute for chmod, which is unreliable on Windows."""
        from coordinator_core.hooks.subagent_zero_tool_use import _handler
        ctx = _ctx(tmp_path)
        bogus = tmp_path / "transcript-is-a-dir.jsonl"
        bogus.mkdir()
        sid = "sess-unreadable-01"
        result = _run(_handler(
            {
                "session_id": sid,
                "agent_id": "agent-unreadable",
                "agent_type": "executor",
                "agent_transcript_path": str(bogus),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        store = _store_path(tmp_path, sid)
        assert not store.exists(), "store file MUST NOT be created on UNKNOWN (unreadable)"

    def test_unreadable_via_open_raising_oserror_is_unknown(self, tmp_path: Path, monkeypatch) -> None:
        """Monkeypatched open() raising OSError — a second, fully platform-neutral
        route to the UNKNOWN branch, independent of filesystem permission semantics."""
        import coordinator_core.hooks.subagent_zero_tool_use as mod
        ctx = _ctx(tmp_path)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_assistant_line(1) + "\n")

        real_open = open

        def _raising_open(path, *args, **kwargs):
            if str(path) == str(transcript):
                raise OSError("simulated unreadable transcript")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _raising_open)
        sid = "sess-monkeypatch-01"
        result = _run(mod._handler(
            {
                "session_id": sid,
                "agent_id": "agent-mp",
                "agent_type": "executor",
                "agent_transcript_path": str(transcript),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        store = _store_path(tmp_path, sid)
        assert not store.exists()


# ---------------------------------------------------------------------------
# Malformed / partial JSONL tolerance
# ---------------------------------------------------------------------------

class TestMalformedLinesTolerated:
    def test_malformed_lines_skipped_not_fatal(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use import _handler
        ctx = _ctx(tmp_path)
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            "not json at all {{{",
            json.dumps(["a", "list", "not", "a", "dict"]),
            json.dumps({"type": "assistant"}),  # no "message" key
            json.dumps({"type": "assistant", "message": {"role": "assistant"}}),  # no "content"
            json.dumps({"type": "assistant", "message": {"content": "not-a-list"}}),
            _assistant_line(1),
            "",
        ]
        transcript.write_text("\n".join(lines) + "\n")
        sid = "sess-malformed-01"
        result = _run(_handler(
            {
                "session_id": sid,
                "agent_id": "agent-malformed",
                "agent_type": "executor",
                "agent_transcript_path": str(transcript),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        store = _store_path(tmp_path, sid)
        record = json.loads(store.read_text().strip())
        assert record["tool_use_count"] == 1


# ---------------------------------------------------------------------------
# Record shape — exact field names and order
# ---------------------------------------------------------------------------

class TestRecordShape:
    def test_exact_field_names_and_order(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use import _handler
        ctx = _ctx(tmp_path)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_assistant_line(2) + "\n")
        sid = "sess-shape-01"
        _run(_handler(
            {
                "session_id": sid,
                "agent_id": "agent-shape",
                "agent_type": "executor",
                "agent_transcript_path": str(transcript),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        store = _store_path(tmp_path, sid)
        line = store.read_text().strip()
        pairs = json.loads(line, object_pairs_hook=lambda p: p)
        keys = [k for k, _ in pairs]
        assert keys == ["kind", "session_id", "agent_id", "agent_type", "tool_use_count", "recorded_at"]
        record = dict(pairs)
        assert record["kind"] == "zero-tool-use"
        assert record["session_id"] == sid
        assert record["agent_id"] == "agent-shape"
        assert record["agent_type"] == "executor"
        assert record["tool_use_count"] == 2
        assert record["recorded_at"].endswith("Z")

    def test_compact_single_line_json(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use import _handler
        ctx = _ctx(tmp_path)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_assistant_line(0) + "\n")
        sid = "sess-compact-01"
        _run(_handler(
            {
                "session_id": sid,
                "agent_id": "agent-compact",
                "agent_type": "executor",
                "agent_transcript_path": str(transcript),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        store = _store_path(tmp_path, sid)
        raw = store.read_bytes()
        assert raw.count(b"\n") == 1
        assert b": " not in raw


# ---------------------------------------------------------------------------
# Append ordering across two calls
# ---------------------------------------------------------------------------

class TestAppendOrdering:
    def test_two_calls_append_in_order(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use import _handler
        ctx = _ctx(tmp_path)
        sid = "sess-append-01"

        transcript_a = tmp_path / "a.jsonl"
        transcript_a.write_text(_assistant_line(0) + "\n")
        _run(_handler(
            {
                "session_id": sid,
                "agent_id": "agent-a",
                "agent_type": "executor",
                "agent_transcript_path": str(transcript_a),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))

        transcript_b = tmp_path / "b.jsonl"
        transcript_b.write_text(_assistant_line(4) + "\n")
        _run(_handler(
            {
                "session_id": sid,
                "agent_id": "agent-b",
                "agent_type": "code-reviewer",
                "agent_transcript_path": str(transcript_b),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))

        store = _store_path(tmp_path, sid)
        lines = [ln for ln in store.read_text().splitlines() if ln]
        assert len(lines) == 2
        first, second = (json.loads(ln) for ln in lines)
        assert first["agent_id"] == "agent-a"
        assert first["tool_use_count"] == 0
        assert second["agent_id"] == "agent-b"
        assert second["tool_use_count"] == 4


# ---------------------------------------------------------------------------
# Naming reconciliation (2026-07-26): the store records every verified count,
# not only zero — cross-repo/inbox/2026-07-25-coordinator-claude-em-zero-tool-use-store-
# records-every-count.md. These tests pin BOTH halves of that decision end to
# end: a non-zero count is durable-written AND resolves as "did-work"; a zero
# count is durable-written AND still resolves as "zero-tool-use" via the
# Stage-3 query path (hooks.subagent_zero_tool_use_resolve) — the detection
# behavior itself must be unaffected by the naming/documentation-only change.
# ---------------------------------------------------------------------------

class TestNamingReconciliationEndToEnd:
    def test_nonzero_count_is_recorded_and_resolves_did_work(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use import _handler as write_handler
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler as resolve_handler
        ctx = _ctx(tmp_path)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_assistant_line(3) + "\n")
        sid = "sess-e2e-nonzero"
        _run(write_handler(
            {
                "session_id": sid,
                "agent_id": "agent-e2e-nonzero",
                "agent_type": "executor",
                "agent_transcript_path": str(transcript),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        store = _store_path(tmp_path, sid)
        record = json.loads(store.read_text().strip())
        assert record["tool_use_count"] == 3

        verdict = _run(resolve_handler(
            {"session_id": sid, "agent_id": "agent-e2e-nonzero", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert verdict["verdict"] == "did-work"
        assert verdict["tool_use_count"] == 3

    def test_zero_count_is_recorded_and_still_resolves_zero_tool_use(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_zero_tool_use import _handler as write_handler
        from coordinator_core.hooks.subagent_zero_tool_use_resolve import _handler as resolve_handler
        ctx = _ctx(tmp_path)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_assistant_line(0, extra_text_blocks=1) + "\n")
        sid = "sess-e2e-zero"
        _run(write_handler(
            {
                "session_id": sid,
                "agent_id": "agent-e2e-zero",
                "agent_type": "executor",
                "agent_transcript_path": str(transcript),
                "hook_event_name": "SubagentStop",
            },
            repo_root=ctx.repo_root,
        ))
        store = _store_path(tmp_path, sid)
        record = json.loads(store.read_text().strip())
        assert record["tool_use_count"] == 0
        assert record["kind"] == "zero-tool-use"

        verdict = _run(resolve_handler(
            {"session_id": sid, "agent_id": "agent-e2e-zero", "hook_event_name": "PostToolUse"},
            repo_root=ctx.repo_root,
        ))
        assert verdict["verdict"] == "zero-tool-use"
        assert verdict["tool_use_count"] == 0


# ---------------------------------------------------------------------------
# Registration quad
# ---------------------------------------------------------------------------

class TestRegistrationQuad:
    def test_op_present_in_all_four_surfaces(self) -> None:
        from coordinator_core.hooks import subagent_zero_tool_use  # noqa: F401 — self-registers
        from coordinator_core.ipc import _REGISTRY
        from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
        from coordinator_core.op_scopes import _OP_KEY_SCOPE
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        op_key = "hooks.subagent_zero_tool_use"
        assert op_key in _REGISTRY
        assert OP_CLASSIFICATION[op_key] == OpClass.MUTATING
        assert _OP_KEY_SCOPE[op_key] == "common_dir"
        assert OP_MODULE_MAP[op_key] == "coordinator_core.hooks"
