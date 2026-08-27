"""
coordinator_core.hooks.tests.test_receiver_state_sensor — tests for the thin op
wrapper over session.receiver_state.

Default tier. Fixture transcripts synthesized in-test.

Spec backlink: docs/plans/2026-08-14-receiver-state-sensor.md § C3
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from coordinator_core.hooks import receiver_state_sensor as sensor
from coordinator_core.session import receiver_state as rs


def _fake_session_dir(monkeypatch, tmp_path: Path):
    from coordinator_core.session import core as session_core

    fake_base = tmp_path / "coordinator-sessions"

    def _fake_session_dir(sid: str, cwd=None) -> str:
        return str(fake_base / sid)

    monkeypatch.setattr(session_core, "session_dir", _fake_session_dir)


def _write_transcript(tmp_path: Path, lines: list[str]) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


class TestHandlerReturnsNoAdvisory:
    def test_missing_session_id_returns_no_advisory(self) -> None:
        result = asyncio.run(sensor._handler({}, repo_root=None))
        assert result == {}

    def test_always_returns_no_advisory_shape(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        transcript = _write_transcript(
            tmp_path, [json.dumps({"type": "system", "subtype": "away_summary", "timestamp": "2026-08-14T00:00:00Z"})]
        )
        result = asyncio.run(
            sensor._handler(
                {"session_id": "sid-h1", "transcript_path": transcript},
                repo_root=str(tmp_path),
            )
        )
        assert result == {}


class TestHandlerWritesSiblingFile:
    def test_writes_receiver_state_json(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        transcript = _write_transcript(
            tmp_path,
            [json.dumps({"type": "system", "subtype": "away_summary", "timestamp": "2026-08-14T00:00:00Z"})],
        )
        asyncio.run(
            sensor._handler(
                {"session_id": "sid-h2", "transcript_path": transcript},
                repo_root=str(tmp_path),
            )
        )
        record = rs.read_receiver_state("sid-h2", str(tmp_path))
        assert record is not None
        assert record["verdict"] == "PAUSED"
        assert record["reason"] == "away"

    def test_missing_transcript_path_writes_unknown(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        asyncio.run(
            sensor._handler(
                {"session_id": "sid-h3", "transcript_path": ""},
                repo_root=str(tmp_path),
            )
        )
        record = rs.read_receiver_state("sid-h3", str(tmp_path))
        assert record is not None
        assert record["verdict"] == "UNKNOWN"

    def test_handler_never_raises_on_internal_failure(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated sensor failure")

        monkeypatch.setattr(sensor, "_run_sensor", _boom)
        # Must not raise — fail-soft (AC12).
        result = asyncio.run(
            sensor._handler(
                {"session_id": "sid-h4", "transcript_path": "whatever"},
                repo_root=str(tmp_path),
            )
        )
        assert result == {}


class TestRegistrationSuffix:
    def test_op_key_suffix_equals_module_basename(self) -> None:
        from coordinator_core.ipc import _REGISTRY

        assert "hooks.receiver_state_sensor" in _REGISTRY
        assert sensor.__name__.rsplit(".", 1)[-1] == "receiver_state_sensor"
