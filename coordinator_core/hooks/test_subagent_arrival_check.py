"""
coordinator_core.hooks.test_subagent_arrival_check — round-trip tests for the
pull/poll subagent-arrival op (hooks.subagent_arrival_check).

Covers: arrived (stop_reason short-circuit and debounce-cleared), running (shape
mismatch, and the flap case — shape matches but record younger than the debounce),
unknown (absent transcript, malformed/unparseable final line, truncated/empty
file, missing inputs), path derivation, and registration-quad presence for the op
key. All fixtures are hand-built JSONL — no live ~/.claude transcript data is read.

All handlers are async; asyncio.run() is used directly in sync test functions — no
pytest-asyncio dependency, matching coordinator_core/hooks/test_subagent_zero_tool_use_resolve.py.

Spec backlink: cross-repo/inbox/2026-07-25-coordinator-claude-em-zero-tool-use-detection-engine-op-contract.md
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import time
from pathlib import Path

# Fixed mtimes for the ambiguity tests below — real dispatch files land seconds
# apart, so newest-wins needs an unambiguous ordering to assert on.
_PAST = time.time() - 3600
_FUTURE = time.time() + 3600


def _run(coro):
    return asyncio.run(coro)


def _now_iso(offset_seconds: float = 0.0) -> str:
    ts = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=offset_seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _assistant_record(*, stop_reason=None, timestamp=None, with_tool_use=False) -> str:
    content = [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}] if with_tool_use else [
        {"type": "text", "text": "done"}
    ]
    message = {"role": "assistant", "content": content}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    record = {"type": "assistant", "message": message}
    if timestamp is not None:
        record["timestamp"] = timestamp
    return json.dumps(record)


def _user_tool_result_record() -> str:
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": ""}]},
    })


def _write_transcript(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return path


def _write_meta(transcript_path: Path, *, name: str, team_name: str) -> Path:
    """Write the `.meta.json` sidecar the harness drops next to each subagent transcript."""
    sidecar = transcript_path.with_suffix(".meta.json")
    sidecar.write_text(json.dumps({
        "agentType": name, "description": name, "name": name,
        "spawnDepth": 0, "model": "sonnet", "taskKind": "in_process_teammate",
        "teamName": team_name,
    }))
    return sidecar


def _subagent_path(parent_transcript_path: Path, agent_id: str) -> Path:
    directory = parent_transcript_path.parent
    stem = parent_transcript_path.stem
    return directory / stem / "subagents" / f"agent-{agent_id}.jsonl"


class TestPathDerivation:
    def test_derives_path_from_parent_transcript_and_agent_id(self) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _derive_subagent_transcript_path
        derived = _derive_subagent_transcript_path("/home/u/.claude/projects/p/abc123.jsonl", "agent-7")
        assert derived == "/home/u/.claude/projects/p/abc123/subagents/agent-agent-7.jsonl"


class TestArrived:
    def test_stop_reason_end_turn_shortcircuits_debounce(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record(stop_reason="end_turn", timestamp=_now_iso(0))])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "arrived"
        assert "end_turn" in result["reason"]

    def test_stop_reason_stop_sequence_shortcircuits_debounce(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record(stop_reason="stop_sequence", timestamp=_now_iso(0))])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "arrived"

    def test_no_stop_reason_but_old_timestamp_clears_debounce(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record(timestamp=_now_iso(200))])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "arrived"
        assert "debounce" in result["reason"]

    def test_debounce_seconds_param_is_ignored(self, tmp_path: Path) -> None:
        """A caller can no longer defeat the debounce by passing an override.

        FINDING (accepted): `debounce_seconds` used to be a per-call parameter,
        which let a caller pass e.g. 0 and lose the streaming-race protection —
        every mid-turn flap would then read as a false "arrived". The param is
        gone; a `debounce_seconds` key in params is simply not read, so a
        record well inside the real 120s debounce still resolves "running"
        regardless of what the caller passes.
        """
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record(timestamp=_now_iso(10))])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1", "debounce_seconds": "0"}))
        assert result["state"] == "running"


class TestRunningShapeMismatch:
    def test_tool_use_block_present_resolves_running(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record(with_tool_use=True, timestamp=_now_iso(0))])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "running"

    def test_user_tool_result_record_resolves_running(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_user_tool_result_record()])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "running"


class TestRunningFlap:
    def test_shape_matches_but_record_younger_than_debounce_resolves_running(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record(timestamp=_now_iso(1))])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "running"
        assert "flap" in result["reason"] or "debounce" in result["reason"]

    def test_shape_matches_no_timestamp_resolves_running_not_arrived(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record()])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "running"


class TestUnknown:
    def test_absent_transcript_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "unknown"
        assert "absent" in result["reason"] or "unreadable" in result["reason"] or "empty" in result["reason"]

    def test_empty_transcript_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "unknown"

    def test_truncated_blank_only_transcript_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        sub.parent.mkdir(parents=True, exist_ok=True)
        sub.write_text("\n\n   \n")
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "unknown"

    def test_malformed_json_final_line_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record(stop_reason="end_turn"), "{not valid json"])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "unknown"

    def test_final_line_valid_json_but_not_object_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [json.dumps([1, 2, 3])])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "unknown"

    def test_missing_agent_id_resolves_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        result = _run(_handler({"transcript_path": str(parent), "agent_id": ""}))
        assert result["state"] == "unknown"
        assert "agent_id" in result["reason"]

    def test_missing_transcript_path_resolves_unknown_names_agent(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        result = _run(_handler({"transcript_path": "", "agent_id": "a1"}))
        assert result["state"] == "unknown"
        assert "a1" in result["agent_id"]
        assert "transcript_path" in result["reason"]

    def test_unreadable_transcript_resolves_unknown(self, tmp_path: Path, monkeypatch) -> None:
        from coordinator_core.hooks import subagent_arrival_check as mod
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record(stop_reason="end_turn")])

        real_open = open

        def _boom(path, *args, **kwargs):
            if str(path) == str(sub):
                raise OSError("permission denied (simulated)")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _boom)
        result = _run(mod._handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "unknown"


class TestTailCap:
    def test_final_record_exceeding_tail_cap_resolves_unknown_with_own_reason(self, tmp_path: Path) -> None:
        """FINDING (accepted): a final record larger than the ~256KB tail cap used
        to be reported as "last transcript line is not valid JSON" — true about
        the fragment the tail read produced, false about the transcript (the
        record itself may well be valid JSON; it's just too big to have been
        read whole). The cap-reached case now gets its own reason string,
        distinct from a genuine parse failure, while still resolving "unknown"
        (never "arrived") either way. Fixture built programmatically at test
        time — no 256KB file is committed to the repo.
        """
        from coordinator_core.hooks.subagent_arrival_check import _handler, _TAIL_CAP_BYTES
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        sub.parent.mkdir(parents=True, exist_ok=True)

        oversized_text = "x" * (_TAIL_CAP_BYTES + 4096)
        oversized_record = json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": oversized_text}], "stop_reason": "end_turn"},
        })
        assert len(oversized_record.encode("utf-8")) > _TAIL_CAP_BYTES

        sub.write_text(_assistant_record(stop_reason="end_turn") + "\n" + oversized_record + "\n")

        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "unknown"
        assert "tail" in result["reason"] and "cap" in result["reason"]
        assert "not valid JSON" not in result["reason"]


class TestBoundaryShapes:
    """Review: coordinator:code-reviewer — Finding 1 (accepted) and Finding 4
    (accepted). Finding 1: `buf.rstrip(b"\\n")` only stripped a trailing run of
    pure newline bytes, so a trailing blank line carrying any other whitespace
    (e.g. "...}\\n   \\n") left an embedded \\n that falsely signalled a found
    record boundary and truncated the real last record mid-read. Finding 4: no
    fixture exercised a multi-chunk tail read (a real record spanning more than
    one `_TAIL_CHUNK_BYTES` chunk) combined with trailing blank content, nor
    CRLF line endings, nor a multi-byte UTF-8 character split across a chunk
    boundary — all named as risk areas in the module's own docstring/charter.
    """

    def test_multi_chunk_record_with_whitespace_trailing_blank_line_round_trips(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler, _TAIL_CHUNK_BYTES
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        sub.parent.mkdir(parents=True, exist_ok=True)

        big_text = "x" * (_TAIL_CHUNK_BYTES * 2)
        record = json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": big_text}], "stop_reason": "end_turn"},
        })
        assert len(record.encode("utf-8")) > _TAIL_CHUNK_BYTES

        # Trailing blank line carries whitespace, not just a bare newline.
        sub.write_text(record + "\n" + "   \n")

        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "arrived"
        assert "end_turn" in result["reason"]

    def test_crlf_transcript_resolves_correctly(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        sub.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            _assistant_record(with_tool_use=True, timestamp=_now_iso(0)),
            _assistant_record(stop_reason="end_turn"),
        ]
        sub.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))

        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "arrived"

    def test_multibyte_utf8_character_straddling_chunk_boundary_round_trips(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler, _TAIL_CHUNK_BYTES
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        sub.parent.mkdir(parents=True, exist_ok=True)

        marker = "λ"  # 2-byte UTF-8 character (0xCE 0xBB)
        base = json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "@MARK@"}], "stop_reason": "end_turn"},
        })
        idx = base.index("@MARK@")
        prefix, suffix = base[:idx], base[idx + len("@MARK@"):]

        pad_before = "x" * 9000
        pad_after_len = (_TAIL_CHUNK_BYTES - 2) - len(suffix.encode("utf-8"))
        assert pad_after_len > 0
        pad_after = "x" * pad_after_len

        record = prefix + pad_before + marker + pad_after + suffix
        record_bytes = record.encode("utf-8")

        marker_first_byte_index = len((prefix + pad_before).encode("utf-8"))
        file_size = len(record_bytes) + 1  # + trailing "\n"
        assert marker_first_byte_index == file_size - _TAIL_CHUNK_BYTES - 1, (
            "test fixture arithmetic is off — marker no longer straddles the chunk boundary"
        )

        sub.write_bytes(record_bytes + b"\n")

        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert result["state"] == "arrived"
        assert "end_turn" in result["reason"]


class TestReturnShapePinned:
    def test_exact_top_level_keys(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a1")
        _write_transcript(sub, [_assistant_record(stop_reason="end_turn")])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a1"}))
        assert set(result.keys()) == {"state", "agent_id", "subagent_transcript_path", "reason"}
        assert result["subagent_transcript_path"] == str(sub)


class TestNamedTeammateIdNamespace:
    """The EM-side canonical id `<name>@session-<short8>` must reach the real file.

    Before this resolution existed, every named teammate derived
    `subagents/agent-<name>@session-<short8>.jsonl` — a filename that cannot exist —
    so the op answered "unknown" for all of them and the caller nudged agents that
    had already returned. Bare-hex dispatches were unaffected, which is why the gap
    survived a suite with no named-teammate case in it.
    """

    def test_canonical_id_resolves_to_the_real_subagent_transcript(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "b3052ce5-1111-2222-3333-444444444444.jsonl"
        real = _subagent_path(parent, "awiki-skills-1b-aa3eadeb0f1c2d3e")
        _write_transcript(real, [_assistant_record(stop_reason="end_turn", timestamp=_now_iso(0))])
        _write_meta(real, name="wiki-skills-1b", team_name="session-b3052ce5")

        result = _run(_handler({
            "transcript_path": str(parent),
            "agent_id": "wiki-skills-1b@session-b3052ce5",
        }))
        assert result["state"] == "arrived"
        assert result["subagent_transcript_path"] == str(real)
        # The id is echoed as the caller supplied it, not rewritten to the file's form.
        assert result["agent_id"] == "wiki-skills-1b@session-b3052ce5"

    def test_canonical_id_running_agent_still_reads_running(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "b3052ce5.jsonl"
        real = _subagent_path(parent, "arun-me-00112233445566aa")
        _write_transcript(real, [_assistant_record(with_tool_use=True, timestamp=_now_iso(0))])
        _write_meta(real, name="run-me", team_name="session-b3052ce5")
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "run-me@session-b3052ce5"}))
        assert result["state"] == "running"

    def test_sidecar_confirmed_match_beats_a_filename_only_match(self, tmp_path: Path) -> None:
        """Two files match the filename shape; only one's sidecar names this team."""
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "b3052ce5.jsonl"
        wrong = _subagent_path(parent, "adupe-ffffffffffffffff")
        _write_transcript(wrong, [_assistant_record(with_tool_use=True, timestamp=_now_iso(0))])
        _write_meta(wrong, name="dupe", team_name="session-99999999")
        right = _subagent_path(parent, "adupe-00000000000000aa")
        _write_transcript(right, [_assistant_record(stop_reason="end_turn", timestamp=_now_iso(0))])
        _write_meta(right, name="dupe", team_name="session-b3052ce5")
        # Make the WRONG one newest, so a bare newest-mtime rule would pick it.
        os.utime(wrong, (_FUTURE, _FUTURE))

        result = _run(_handler({"transcript_path": str(parent), "agent_id": "dupe@session-b3052ce5"}))
        assert result["subagent_transcript_path"] == str(right)
        assert result["state"] == "arrived"

    def test_redispatched_name_without_sidecars_picks_the_newest(self, tmp_path: Path) -> None:
        """Same name twice in one session: the canonical id cannot tell them apart."""
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "b3052ce5.jsonl"
        older = _subagent_path(parent, "atwice-1111111111111111")
        _write_transcript(older, [_assistant_record(stop_reason="end_turn", timestamp=_now_iso(0))])
        newer = _subagent_path(parent, "atwice-2222222222222222")
        _write_transcript(newer, [_assistant_record(with_tool_use=True, timestamp=_now_iso(0))])
        os.utime(older, (_PAST, _PAST))
        os.utime(newer, (_FUTURE, _FUTURE))

        result = _run(_handler({"transcript_path": str(parent), "agent_id": "twice@session-b3052ce5"}))
        assert result["subagent_transcript_path"] == str(newer)
        assert result["state"] == "running"

    def test_bare_hex_id_still_uses_the_direct_derivation(self, tmp_path: Path) -> None:
        """Regression guard: the unnamed fast path must not route through the probe."""
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "abc123.jsonl"
        sub = _subagent_path(parent, "a50f96a8930246124")
        _write_transcript(sub, [_assistant_record(stop_reason="end_turn")])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "a50f96a8930246124"}))
        assert result["subagent_transcript_path"] == str(sub)
        assert result["state"] == "arrived"

    def test_canonical_id_with_no_matching_file_stays_unknown(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "b3052ce5.jsonl"
        _write_transcript(_subagent_path(parent, "aother-1111111111111111"), [_assistant_record()])
        result = _run(_handler({"transcript_path": str(parent), "agent_id": "absent@session-b3052ce5"}))
        assert result["state"] == "unknown"
        assert result["subagent_transcript_path"].endswith("agent-absent@session-b3052ce5.jsonl")

    def test_traversal_shaped_name_never_resolves_outside_the_subagents_directory(
        self, tmp_path: Path
    ) -> None:
        """A `/`-bearing name fails the canonical regex, so the probe never runs.

        Even if it had run, the probe filters `os.listdir` output rather than
        building a path from the name — this pins the outer of those two guards.
        """
        from coordinator_core.hooks.subagent_arrival_check import _handler
        parent = tmp_path / "b3052ce5.jsonl"
        (tmp_path / "b3052ce5" / "subagents").mkdir(parents=True)
        # An arrival-shaped record OUTSIDE the subagents dir. If a traversal landed
        # here the op would answer "arrived" — so "unknown" is the load-bearing
        # assertion, not merely a null result.
        outside = tmp_path / "b3052ce5" / "agent-escaped.jsonl"
        outside.write_text(_assistant_record(stop_reason="end_turn") + "\n")

        for hostile in ("../agent-escaped",
                        "..",
                        "sub/../../agent-escaped",
                        "escaped@session-../../.."):
            result = _run(_handler({"transcript_path": str(parent), "agent_id": hostile}))
            assert result["state"] == "unknown", hostile
            assert result["subagent_transcript_path"] == ""


class TestRegistrationQuad:
    def test_op_present_in_all_four_surfaces(self) -> None:
        from coordinator_core.hooks import subagent_arrival_check  # noqa: F401 — self-registers
        from coordinator_core.ipc import _REGISTRY
        from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
        from coordinator_core.op_scopes import _OP_KEY_SCOPE
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        op_key = "hooks.subagent_arrival_check"
        assert op_key in _REGISTRY
        assert OP_CLASSIFICATION[op_key] == OpClass.COMPUTE_ONLY
        assert _OP_KEY_SCOPE[op_key] == "none"
        assert OP_MODULE_MAP[op_key] == "coordinator_core.hooks"
