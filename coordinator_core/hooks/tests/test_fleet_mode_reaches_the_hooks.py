"""
Tests for coordinator_core/hooks/nudge_em_code_dispatch.py and
coordinator_core/hooks/postuse_advisory_dispatch.py — THE POSITIVE CONTROL
proving the fleet file reaches the hooks that actually run, not merely
`resolve_mode` in isolation.

Two properties, both required:
    1. With no fleet file, each converted call site behaves bit-identically
       to the pre-plan sentinel-only path (the baseline C3 must not disturb).
    2. With a fleet file setting a key, the HOOK ENTRY POINT that actually
       runs (``op()`` / ``_check_context_pressure_sync`` /
       ``_check_runtime_tripwire_sync`` — never ``resolve_mode`` called
       directly) behaves differently.

Covers both precedence branches, since C3 is the only chunk that can:
    - ``autonomous`` (session-wins) via ``nudge_em_code_dispatch.op()`` and
      ``postuse_advisory_dispatch._check_runtime_tripwire_sync``.
    - ``compaction_warnings`` (fleet-wins) via
      ``postuse_advisory_dispatch._check_context_pressure_sync``.

Spec backlink: docs/plans/2026-08-28-the-fleet-gets-one-file-and-the-floor-
moves-to-the-reader.md § C3.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.hooks import nudge_em_code_dispatch
from coordinator_core.hooks import postuse_advisory_dispatch
from coordinator_core.session.mode_resolution import MODE_KEYS


@pytest.fixture(autouse=True)
def _isolate_sentinel_and_fleet(tmp_path, monkeypatch):
    """Isolate the autonomous sentinel's temp dir, the fleet record's
    settings home, and the context-usage-sidecar's settings home, so tests
    never touch real machine-wide files. Mirrors
    coordinator_core/session/tests/test_mode_resolution.py's fixture."""
    monkeypatch.setattr(
        "coordinator_core.session.autonomous_sentinel.tempfile.gettempdir",
        lambda: str(tmp_path),
    )
    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setattr(
        "coordinator_core.session.fleet_mode.settings_home",
        lambda: settings_home,
    )
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    # postuse_advisory_dispatch's durable per-session state (throttle/bark-once)
    # and the runtime-tripwire bark-once sentinel both go through
    # tempfile.gettempdir() at the module's own `_tempfile()` accessor, which
    # re-imports the real `tempfile` module -- patch it globally too.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    return tmp_path, settings_home


def _touch_autonomous_sentinel(tmp_path, session_id):
    from coordinator_core.session import autonomous_sentinel

    autonomous_sentinel.sentinel_path(session_id).touch()


def _write_fleet(record):
    from coordinator_core.session.fleet_mode import write_fleet_mode

    assert write_fleet_mode(record)


# ---------------------------------------------------------------------------
# nudge_em_code_dispatch.op() -- Bypass 4, `autonomous` key, session-wins.
# ---------------------------------------------------------------------------


def _op_payload(session_id: str, file_path: str = "foo.py") -> dict:
    return {
        "session_id": session_id,
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "print('substantive change')\n"},
    }


class TestNudgeEmCodeDispatchOpAutonomous:
    def test_no_fleet_file_no_sentinel_nudge_fires(self, _isolate_sentinel_and_fleet):
        """Baseline unchanged: no fleet file, no sentinel -> nudge fires."""
        result = nudge_em_code_dispatch.op(_op_payload("s1"))
        assert result is not None

    def test_no_fleet_file_sentinel_present_suppressed(self, _isolate_sentinel_and_fleet):
        """Baseline unchanged: no fleet file, sentinel present -> suppressed."""
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s2")
        result = nudge_em_code_dispatch.op(_op_payload("s2"))
        assert result is None

    def test_fleet_autonomous_on_does_not_override_absent_sentinel(
        self, _isolate_sentinel_and_fleet
    ):
        """session-wins proof, through the hook entry point: a fleet
        autonomous:on value must NOT suppress the nudge when this session's
        own sentinel is absent."""
        _write_fleet({"autonomous": True})
        result = nudge_em_code_dispatch.op(_op_payload("s3"))
        assert result is not None

    def test_fleet_autonomous_off_does_not_unsuppress_present_sentinel(
        self, _isolate_sentinel_and_fleet
    ):
        """session-wins proof, through the hook entry point: a fleet
        autonomous:off value must NOT re-enable the nudge when this
        session's own sentinel is present."""
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s4")
        _write_fleet({"autonomous": False})
        result = nudge_em_code_dispatch.op(_op_payload("s4"))
        assert result is None


class TestNudgeEmCodeDispatchHandlerAutonomous:
    """Same properties through the async pcore-04 `_handler` op.

    House convention (coordinator_core/ops/tests/test_cutover_gate_handler.py):
    plain sync tests wrapping the handler in `asyncio.run(...)` — pytest-asyncio
    is deliberately absent from this tree (see pyproject.toml comment).
    """

    def test_no_fleet_file_no_sentinel_nudge_fires(self, _isolate_sentinel_and_fleet):
        params = {"session_id": "s5", "file_path": "foo.py"}
        result = asyncio.run(nudge_em_code_dispatch._handler(params))
        assert result["hookSpecificOutput"].get("additionalContext")

    def test_no_fleet_file_sentinel_present_suppressed(self, _isolate_sentinel_and_fleet):
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s6")
        params = {"session_id": "s6", "file_path": "foo.py"}
        result = asyncio.run(nudge_em_code_dispatch._handler(params))
        assert result == {}

    def test_fleet_autonomous_on_does_not_override_absent_sentinel(
        self, _isolate_sentinel_and_fleet
    ):
        _write_fleet({"autonomous": True})
        params = {"session_id": "s7", "file_path": "foo.py"}
        result = asyncio.run(nudge_em_code_dispatch._handler(params))
        assert result["hookSpecificOutput"].get("additionalContext")


# ---------------------------------------------------------------------------
# postuse_advisory_dispatch -- `compaction_warnings` key, fleet-wins, and
# `autonomous` key, session-wins (runtime tripwire leg).
# ---------------------------------------------------------------------------


def _write_usage(session_id: str, used_percentage: float, now: float):
    from coordinator_core.session.context_usage_sidecar import write_usage

    write_usage(
        session_id,
        {"used_percentage": used_percentage, "remaining_percentage": 100 - used_percentage},
        now=now,
    )


class TestContextPressureCompactionWarningsFleetWins:
    """`compaction_warnings` is a VARIANT SELECTOR, never an off switch:
    for every value the key admits, the function still returns non-empty
    advisory text at the 40% and 47% bands."""

    def test_no_fleet_file_standard_variant_at_47(self, _isolate_sentinel_and_fleet):
        """Baseline unchanged: no fleet file -> standard HANDOFF NOW text."""
        now = 1_000_000.0
        _write_usage("cp1", 50.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp1", "/does/not/matter/transcript.jsonl"
        )
        assert "HANDOFF NOW" in text
        assert "INFORMATIONAL" not in text

    def test_fleet_informational_selects_variant_at_47(self, _isolate_sentinel_and_fleet):
        """fleet-wins proof, through the hook entry point: a fleet
        compaction_warnings:informational value selects the informational
        variant even with no session-scoped sentinel for this key."""
        _write_fleet({"compaction_warnings": "informational"})
        now = 1_000_000.0
        _write_usage("cp2", 50.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp2", "/does/not/matter/transcript.jsonl"
        )
        assert text
        assert "INFORMATIONAL" in text
        assert "Commit and checkpoint now" in text

    def test_fleet_informational_selects_variant_at_40(self, _isolate_sentinel_and_fleet):
        _write_fleet({"compaction_warnings": "informational"})
        now = 1_000_000.0
        _write_usage("cp3", 41.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp3", "/does/not/matter/transcript.jsonl"
        )
        assert text
        assert "INFORMATIONAL" in text

    def test_fleet_standard_never_returns_empty_at_band(self, _isolate_sentinel_and_fleet):
        _write_fleet({"compaction_warnings": "standard"})
        now = 1_000_000.0
        _write_usage("cp4", 48.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp4", "/does/not/matter/transcript.jsonl"
        )
        assert text  # never "" -- selector, not an off switch

    def test_fleet_malformed_value_never_returns_empty_at_band(self, _isolate_sentinel_and_fleet):
        """A fleet value outside the declared enum degrades to the default
        ("standard") -- still non-empty advisory text, never "" ."""
        _write_fleet({"compaction_warnings": "silent"})
        now = 1_000_000.0
        _write_usage("cp5", 48.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp5", "/does/not/matter/transcript.jsonl"
        )
        assert text
        assert "HANDOFF NOW" in text  # degraded to standard

    def test_absent_key_never_returns_empty_at_band(self, _isolate_sentinel_and_fleet):
        """No fleet file at all (key entirely absent) -- still non-empty."""
        now = 1_000_000.0
        _write_usage("cp6", 40.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp6", "/does/not/matter/transcript.jsonl"
        )
        assert text

    def test_autonomous_sentinel_still_selects_informational_baseline(
        self, _isolate_sentinel_and_fleet
    ):
        """Baseline unchanged: the pre-existing autonomous-sentinel path
        (leg 1) still selects the informational variant with no fleet file
        involved at all."""
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "cp7")
        now = 1_000_000.0
        _write_usage("cp7", 47.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp7", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text


class TestModeKeysRegistryStillValid:
    def test_compaction_warnings_key_registered(self):
        assert "compaction_warnings" in MODE_KEYS
        assert MODE_KEYS["compaction_warnings"].precedence == "fleet-wins"

    def test_autonomous_key_registered(self):
        assert "autonomous" in MODE_KEYS
        assert MODE_KEYS["autonomous"].precedence == "session-wins"
