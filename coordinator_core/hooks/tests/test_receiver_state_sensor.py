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

    def _fake_ensure_session(sid: str, cwd=None, **kwargs) -> str:
        # `write_receiver_state` calls `ensure_session` directly (not `session_dir`)
        # to create the session directory — patch it to the same fake base so the
        # write never escapes to the real `.git/coordinator-sessions/` hub.
        sdir = fake_base / sid
        sdir.mkdir(parents=True, exist_ok=True)
        return str(sdir)

    monkeypatch.setattr(session_core, "session_dir", _fake_session_dir)
    monkeypatch.setattr(session_core, "ensure_session", _fake_ensure_session)


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


# Review: overengineering-reviewer (finding #3, EM-ratified) — `TestTurnObligationWrite`
# removed. It existed only to pin `_record_turn_obligation`'s gating behaviour, and that
# function is deleted: the sensor no longer writes its own turn-boundary row to the
# obligations intake (see the module docstring's SECOND WRITE note, now removed). The
# `obligations.for_peer`/`record` contract itself is untouched and still covered by
# `coordinator_core/group_em/tests/`.


class TestRegistrationSuffix:
    def test_op_key_suffix_equals_module_basename(self) -> None:
        from coordinator_core.ipc import _REGISTRY

        assert "hooks.receiver_state_sensor" in _REGISTRY
        assert sensor.__name__.rsplit(".", 1)[-1] == "receiver_state_sensor"


class TestTranscriptClockIsNotFileMtime:
    """The sensor's own copy of the 2026-08-31 defect, driven end to end.

    These assert THROUGH the handler, not against `classify` directly: the
    defect was which number the sensor passed, so a test that supplies the
    number itself could not have failed. Each case forces the file's mtime
    forward the way a bookkeeping rewrite does, and would return the opposite
    verdict on the pre-fix sensor.
    """

    def _tool_use_transcript(self, tmp_path: Path, stamp: str) -> str:
        return _write_transcript(
            tmp_path,
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": stamp,
                        "message": {
                            "stop_reason": "tool_use",
                            "content": [{"type": "tool_use", "name": "Bash"}],
                        },
                    }
                ),
                # Untimestamped bookkeeping rows -- what the harness rewrites
                # onto a stopped session, moving mtime without the session acting.
                json.dumps({"type": "cost-state"}),
                json.dumps({"type": "last-prompt"}),
            ],
        )

    def test_a_stalled_tool_call_is_paused_even_when_mtime_was_rewritten_forward(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import os
        from datetime import datetime, timedelta, timezone

        from coordinator_core.session import core as session_core

        _fake_session_dir(monkeypatch, tmp_path)
        now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
        # The tool call went out 10 minutes ago -- far past the 90s grace.
        stamp = (now - timedelta(seconds=600)).isoformat().replace("+00:00", "Z")
        transcript = self._tool_use_transcript(tmp_path, stamp)
        # ...but a bookkeeping write touched the file 5 seconds ago.
        os.utime(transcript, (now.timestamp() - 5, now.timestamp() - 5))
        monkeypatch.setattr(session_core, "now_epoch", lambda: int(now.timestamp()))

        asyncio.run(
            sensor._handler(
                {"session_id": "sid-skew-1", "transcript_path": transcript},
                repo_root=str(tmp_path),
            )
        )

        record = rs.read_receiver_state("sid-skew-1", str(tmp_path))
        assert record is not None
        # Pre-fix: age = 5s, under the grace, PRODUCING:tool-in-flight.
        assert record["verdict"] == "PAUSED"
        assert record["reason"].startswith("tool-unanswered")

    def test_a_live_tool_call_is_still_producing(self, tmp_path: Path, monkeypatch) -> None:
        """The correction removes unearned freshness; it must not invent
        staleness on a session that genuinely just acted."""
        from datetime import datetime, timedelta, timezone

        from coordinator_core.session import core as session_core

        _fake_session_dir(monkeypatch, tmp_path)
        now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
        stamp = (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        transcript = self._tool_use_transcript(tmp_path, stamp)
        monkeypatch.setattr(session_core, "now_epoch", lambda: int(now.timestamp()))

        asyncio.run(
            sensor._handler(
                {"session_id": "sid-skew-2", "transcript_path": transcript},
                repo_root=str(tmp_path),
            )
        )

        record = rs.read_receiver_state("sid-skew-2", str(tmp_path))
        assert record is not None
        assert record["verdict"] == "PRODUCING"

    def test_mtime_is_still_the_fallback_when_no_line_carries_a_timestamp(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An upper bound is better than no age evidence at all -- refusing it
        would keep PRODUCING:tool-in-flight forever on such a tail."""
        import os
        from datetime import datetime, timezone

        from coordinator_core.session import core as session_core

        _fake_session_dir(monkeypatch, tmp_path)
        now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
        transcript = _write_transcript(
            tmp_path,
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "stop_reason": "tool_use",
                            "content": [{"type": "tool_use", "name": "Bash"}],
                        },
                    }
                )
            ],
        )
        os.utime(transcript, (now.timestamp() - 600, now.timestamp() - 600))
        monkeypatch.setattr(session_core, "now_epoch", lambda: int(now.timestamp()))

        asyncio.run(
            sensor._handler(
                {"session_id": "sid-skew-3", "transcript_path": transcript},
                repo_root=str(tmp_path),
            )
        )

        record = rs.read_receiver_state("sid-skew-3", str(tmp_path))
        assert record is not None
        assert record["verdict"] == "PAUSED"
        assert record["reason"].startswith("tool-unanswered")
