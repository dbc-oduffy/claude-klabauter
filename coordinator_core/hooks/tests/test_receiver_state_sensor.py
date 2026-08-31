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

from coordinator_core.group_em import obligations
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


class TestTurnObligationWrite:
    def test_first_fire_opens_obligation(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        transcript = _write_transcript(
            tmp_path,
            [json.dumps({"type": "system", "subtype": "away_summary", "timestamp": "2026-08-14T00:00:00Z"})],
        )
        asyncio.run(
            sensor._handler(
                {"session_id": "sid-obl1", "transcript_path": transcript},
                repo_root=str(tmp_path),
            )
        )
        intake_path = tmp_path / "state" / "subagent-share" / "sid-obl1" / "obligations-inbound.jsonl"
        assert intake_path.exists()
        rows = [json.loads(line) for line in intake_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["op"] == "open"
        assert rows[0]["session_id"] == "sid-obl1"
        assert rows[0]["obligation_id"] == "sid-obl1"
        assert rows[0]["producer"]

    def test_second_fire_progresses_rather_than_reopens(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        transcript = _write_transcript(
            tmp_path,
            [json.dumps({"type": "system", "subtype": "away_summary", "timestamp": "2026-08-14T00:00:00Z"})],
        )

        def _fire():
            asyncio.run(
                sensor._handler(
                    {"session_id": "sid-obl2", "transcript_path": transcript},
                    repo_root=str(tmp_path),
                )
            )

        _fire()
        # Simulate the peer's ledger now carrying the opened, undischarged
        # record `obligations.for_peer` would report -- the sensor's own
        # decision of open-vs-progress reads through this reader.
        monkeypatch.setattr(
            obligations,
            "for_peer",
            lambda repo_root, session_id: [{"obligation_id": session_id, "discharged_at": None, "fired": False}],
        )
        _fire()

        intake_path = tmp_path / "state" / "subagent-share" / "sid-obl2" / "obligations-inbound.jsonl"
        rows = [json.loads(line) for line in intake_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2
        assert rows[0]["op"] == "open"
        assert rows[1]["op"] == "progress"

    def test_producing_session_writes_nothing_and_stays_none(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The chunk's load-bearing negative spec, as a test rather than a comment.

        A session that has not reached a turn boundary owes this ledger nothing, so no
        intake file appears at all and `for_peer` keeps returning None -- "no ledger
        exists", which the spec holds distinct from "nothing owed". The first
        implementation of this sensor wrote one identical row for every session on
        every fire; that reads as 100% coverage and nil information, and it makes None
        unreachable for every live session. Universal identical content is universal
        zero wearing the other hat.
        """
        _fake_session_dir(monkeypatch, tmp_path)
        transcript = _write_transcript(
            tmp_path,
            [json.dumps({"type": "system", "subtype": "away_summary", "timestamp": "2026-08-14T00:00:00Z"})],
        )
        monkeypatch.setattr(
            sensor.receiver_state,
            "resolve_verdict",
            lambda *a, **k: rs.Verdict(verdict="PRODUCING", reason="delegated"),
        )
        asyncio.run(
            sensor._handler(
                {"session_id": "sid-producing", "transcript_path": transcript},
                repo_root=str(tmp_path),
            )
        )
        intake_path = (
            tmp_path / "state" / "subagent-share" / "sid-producing" / "obligations-inbound.jsonl"
        )
        assert not intake_path.exists()

    def test_unknown_verdict_is_not_promoted_to_owing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Absence of evidence is never evidence of a turn boundary.

        A verdict that could not be established must not manufacture an obligation --
        the same rule the roster applies to an unreadable idle time, at the producer
        side instead of the reader side.
        """
        _fake_session_dir(monkeypatch, tmp_path)
        transcript = _write_transcript(
            tmp_path,
            [json.dumps({"type": "system", "subtype": "away_summary", "timestamp": "2026-08-14T00:00:00Z"})],
        )
        monkeypatch.setattr(
            sensor.receiver_state,
            "resolve_verdict",
            lambda *a, **k: rs.Verdict(verdict="UNKNOWN", reason=None),
        )
        asyncio.run(
            sensor._handler(
                {"session_id": "sid-unclass", "transcript_path": transcript},
                repo_root=str(tmp_path),
            )
        )
        intake_path = (
            tmp_path / "state" / "subagent-share" / "sid-unclass" / "obligations-inbound.jsonl"
        )
        assert not intake_path.exists()

    def test_rows_discriminate_between_peers(self, tmp_path: Path, monkeypatch) -> None:
        """Two stopped peers in different states must not produce identical rows.

        `for_peer` returning the NAMES behind the count is the whole point of the
        reader; names that are the same string for every peer cannot be ranked, which
        is the failure this test exists to keep out.

        PATCHED WITH A REAL `Verdict`, NOT A BARE STRING, and that matters. A string
        return breaks `write_receiver_state` upstream, the handler swallows it
        fail-soft, and NOTHING gets written -- so a negative assertion here would pass
        without the gate under test ever running. Two of these tests did exactly that
        before this was caught.

        ASSERTED ON THE INTAKE ROWS, NOT THROUGH `for_peer`, and do not "fix" that
        back. `for_peer` reads DoE's `next-move-ledger.jsonl`, which exists only once
        their consumer has folded our intake in. In-process it returns None whatever
        this sensor wrote, so an assertion through it would test their fold rather
        than our content -- and would pass identically if this sensor wrote nothing.
        """
        _fake_session_dir(monkeypatch, tmp_path)
        transcript = _write_transcript(
            tmp_path,
            [json.dumps({"type": "system", "subtype": "away_summary", "timestamp": "2026-08-14T00:00:00Z"})],
        )
        def _intake_rows(sid: str) -> list:
            path = tmp_path / "state" / "subagent-share" / sid / "obligations-inbound.jsonl"
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        for sid, reason in (("sid-a", "turn-ended"), ("sid-b", "awaiting-approval")):
            monkeypatch.setattr(
                sensor.receiver_state,
                "resolve_verdict",
                lambda *a, _r=reason, **k: rs.Verdict(verdict="PAUSED", reason=_r),
            )
            asyncio.run(
                sensor._handler(
                    {"session_id": sid, "transcript_path": transcript},
                    repo_root=str(tmp_path),
                )
            )
        rows_a = _intake_rows("sid-a")
        rows_b = _intake_rows("sid-b")
        assert rows_a and rows_b
        assert rows_a[0]["next_action"] != rows_b[0]["next_action"]

    def test_missing_repo_root_is_silent_no_op(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        transcript = _write_transcript(
            tmp_path,
            [json.dumps({"type": "system", "subtype": "away_summary", "timestamp": "2026-08-14T00:00:00Z"})],
        )
        # No repo_root -- must not raise, and obviously cannot write under it.
        result = asyncio.run(
            sensor._handler(
                {"session_id": "sid-obl3", "transcript_path": transcript},
                repo_root=None,
            )
        )
        assert result == {}


class TestRegistrationSuffix:
    def test_op_key_suffix_equals_module_basename(self) -> None:
        from coordinator_core.ipc import _REGISTRY

        assert "hooks.receiver_state_sensor" in _REGISTRY
        assert sensor.__name__.rsplit(".", 1)[-1] == "receiver_state_sensor"
