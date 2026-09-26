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
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    return tmp_path, settings_home


def _touch_autonomous_sentinel(tmp_path, session_id):
    from coordinator_core.session import autonomous_sentinel

    autonomous_sentinel.sentinel_path(session_id).write_text("autonomous", encoding="utf-8")


def _write_fleet(record):
    from coordinator_core.session.fleet_mode import write_fleet_mode

    assert write_fleet_mode(record)


def _op_payload(session_id: str, file_path: str = "foo.py") -> dict:
    return {
        "session_id": session_id,
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "print('substantive change')\n"},
    }


class TestNudgeEmCodeDispatchOpAutonomous:
    def test_no_fleet_file_no_sentinel_nudge_fires(self, _isolate_sentinel_and_fleet):
        result = nudge_em_code_dispatch.op(_op_payload("s1"))
        assert result is not None

    def test_no_fleet_file_sentinel_present_suppressed(self, _isolate_sentinel_and_fleet):
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s2")
        result = nudge_em_code_dispatch.op(_op_payload("s2"))
        assert result is None

    def test_fleet_autonomous_on_does_not_override_absent_sentinel(
        self, _isolate_sentinel_and_fleet
    ):
        _write_fleet({"autonomous": True})
        result = nudge_em_code_dispatch.op(_op_payload("s3"))
        assert result is not None

    def test_fleet_autonomous_off_does_not_unsuppress_present_sentinel(
        self, _isolate_sentinel_and_fleet
    ):
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s4")
        _write_fleet({"autonomous": False})
        result = nudge_em_code_dispatch.op(_op_payload("s4"))
        assert result is None


class TestNudgeEmCodeDispatchHandlerAutonomous:

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


#: (`_ORANGE_RUNWAY_TOKENS`/`_RED_RUNWAY_TOKENS`, both back from
#: `window - _AUTO_COMPACT_RESERVE_TOKENS`) from that figure via
#: `_ORANGE_RUNWAY_TOKENS`'s own comment), just a window where this file's
_WINDOW_TOKENS = 190_000


def _write_usage(session_id: str, used_percentage: float, now: float):
    from coordinator_core.session.context_usage_sidecar import write_usage

    write_usage(
        session_id,
        {
            "used_percentage": used_percentage,
            "remaining_percentage": 100 - used_percentage,
            "context_window_size": _WINDOW_TOKENS,
        },
        now=now,
    )


class TestContextPressureCompactionWarningsFleetWins:
    """`compaction_warnings` is a VARIANT SELECTOR, never an off switch:
    for every value the key admits, the function still returns non-empty
    advisory text at the 40% and 43% bands."""

    def test_no_fleet_file_standard_variant_in_the_red_band(self, _isolate_sentinel_and_fleet):
        now = 1_000_000.0
        _write_usage("cp1", 50.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp1", "/does/not/matter/transcript.jsonl"
        )
        assert "HANDOFF NOW" in text
        assert "INFORMATIONAL" not in text

    def test_fleet_informational_selects_variant_in_the_red_band(self, _isolate_sentinel_and_fleet):
        _write_fleet({"compaction_warnings": "informational"})
        now = 1_000_000.0
        _write_usage("cp2", 50.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp2", "/does/not/matter/transcript.jsonl"
        )
        assert text
        assert "INFORMATIONAL" in text
        assert "commit and checkpoint now" in text

    def test_cloud_box_gets_the_informational_variant_with_no_config_at_all(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        # ONE POSITIONAL `env`, matching the real signature: the registry
        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            lambda env=None: "informational",
        )
        _write_usage("cp-cloud", 50.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-cloud", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text
        assert "HANDOFF NOW" not in text, (
            "a cloud box must not be told to run a ceremony it cannot run"
        )

    def test_an_explicit_fleet_value_still_beats_the_environment(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            lambda env=None: "informational",
        )
        _write_fleet({"compaction_warnings": "standard"})
        _write_usage("cp-override", 50.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-override", "/does/not/matter/transcript.jsonl"
        )
        assert "HANDOFF NOW" in text

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
        assert text

    def test_fleet_malformed_value_never_returns_empty_at_band(self, _isolate_sentinel_and_fleet):
        _write_fleet({"compaction_warnings": "silent"})
        now = 1_000_000.0
        _write_usage("cp5", 48.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp5", "/does/not/matter/transcript.jsonl"
        )
        assert text
        assert "HANDOFF NOW" in text

    def test_absent_key_never_returns_empty_at_band(self, _isolate_sentinel_and_fleet):
        now = 1_000_000.0
        _write_usage("cp6", 40.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp6", "/does/not/matter/transcript.jsonl"
        )
        assert text

    def test_autonomous_sentinel_still_selects_informational_baseline(
        self, _isolate_sentinel_and_fleet
    ):
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


class TestBatonAffordanceIsNamedInBothBands:

    def _with_baton(self, monkeypatch, tmp_path, sid):
        baton = tmp_path / f"{sid}-baton.json"
        baton.write_text("{}")
        monkeypatch.setattr(
            "coordinator_core.session_baton.store.baton_path",
            lambda s, cwd=None: baton,
        )
        return baton

    def test_orange_band_names_the_baton(self, _isolate_sentinel_and_fleet, monkeypatch):
        tmp_path = _isolate_sentinel_and_fleet[0]
        baton = self._with_baton(monkeypatch, tmp_path, "cp-40")
        _write_usage("cp-40", 41.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-40", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text
        assert str(baton) in text
        assert "baton.carry_forward" in text

    def test_red_band_informational_names_the_baton(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        tmp_path = _isolate_sentinel_and_fleet[0]
        baton = self._with_baton(monkeypatch, tmp_path, "cp-43")
        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            lambda env=None: "informational",
        )
        _write_usage("cp-43", 50.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-43", "/does/not/matter/transcript.jsonl"
        )
        assert str(baton) in text

    def test_no_baton_on_disk_means_no_clause_not_a_broken_promise(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        monkeypatch.setattr(
            "coordinator_core.session_baton.store.baton_path",
            lambda s, cwd=None: None,
        )
        _write_usage("cp-nobaton", 41.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-nobaton", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text
        assert "baton" not in text.lower()

    def test_the_40_band_still_reads_no_mode_keys(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        def _explode(*a, **k):
            raise AssertionError("the 40 band must not resolve a mode key")

        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution.resolve_mode", _explode
        )
        _write_usage("cp-nomode", 41.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-nomode", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text


_NO_FLEET_FILE = object()

_FLEET_VALUE_CASES = [
    pytest.param(_NO_FLEET_FILE, id="no_fleet_file"),
    pytest.param("standard", id="standard"),
    pytest.param("informational", id="informational"),
    pytest.param("silent", id="out_of_enum_string"),
    pytest.param(True, id="bool_true"),
    pytest.param(1, id="int_one"),
    pytest.param(None, id="null"),
    pytest.param(["informational"], id="list"),
    pytest.param({"value": "informational"}, id="dict"),
]

#: to the STANDARD variant at the red band -- never silently coerced to
#: `informational`. `_NO_FLEET_FILE`/"standard" already assert STANDARD via
_DEGRADES_TO_STANDARD_IDS = {
    "out_of_enum_string",
    "bool_true",
    "int_one",
    "null",
    "list",
    "dict",
}


class TestCompactionWarningsFullCrossProduct:

    def _resolve(self, fleet_value, session_id, percentage):
        if fleet_value is not _NO_FLEET_FILE:
            _write_fleet({"compaction_warnings": fleet_value})
        _write_usage(session_id, percentage, 1_000_000.0)
        return postuse_advisory_dispatch._check_context_pressure_sync(
            session_id, "/does/not/matter/transcript.jsonl"
        )

    @pytest.mark.parametrize("fleet_value", _FLEET_VALUE_CASES)
    def test_orange_band_never_empty(self, _isolate_sentinel_and_fleet, fleet_value, request):
        session_id = f"xp-orange-{request.node.callspec.id}"
        text = self._resolve(fleet_value, session_id, 41.0)
        assert text

    @pytest.mark.parametrize("fleet_value", _FLEET_VALUE_CASES)
    def test_red_band_never_empty(self, _isolate_sentinel_and_fleet, fleet_value, request):
        session_id = f"xp-red-{request.node.callspec.id}"
        text = self._resolve(fleet_value, session_id, 48.0)
        assert text
        if request.node.callspec.id in _DEGRADES_TO_STANDARD_IDS:
            assert "HANDOFF NOW" in text
            assert "INFORMATIONAL" not in text
