"""
Tests for coordinator_core.session.mode_resolution — the resolution seam.

Covers the four-cell matrix (sentinel present/absent x fleet key on/off/
absent) per key, proving the declared precedence wins; the registry
invariant (session_pair=None requires fleet-wins); and the two visibly
distinct error paths (unknown key raises at the call site; unrecognised
fleet-record content degrades silently).
"""

from __future__ import annotations

import dataclasses

import pytest

from coordinator_core.session import mode_resolution
from coordinator_core.session.mode_resolution import (
    MODE_KEYS,
    ModeKey,
    _validate_registry,
    resolve_mode,
)


@pytest.fixture(autouse=True)
def _isolate_sentinel_and_fleet(tmp_path, monkeypatch):
    """Isolate both the autonomous sentinel's temp dir and the fleet record
    location so tests never touch the real machine-wide files."""
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
    return tmp_path, settings_home


def _write_fleet(settings_home, record):
    from coordinator_core.session.fleet_mode import write_fleet_mode

    assert write_fleet_mode(record)


def _touch_autonomous_sentinel(tmp_path, session_id):
    from coordinator_core.session import autonomous_sentinel

    autonomous_sentinel.sentinel_path(session_id).touch()


# --- autonomous: session-wins ------------------------------------------------


class TestAutonomousSessionWins:
    def test_sentinel_present_fleet_absent(self, _isolate_sentinel_and_fleet):
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s1")
        assert resolve_mode("autonomous", "s1") is True

    def test_sentinel_absent_fleet_absent(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("autonomous", "s1") is False

    def test_sentinel_present_fleet_on(self, _isolate_sentinel_and_fleet):
        tmp_path, home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s1")
        _write_fleet(home, {"autonomous": False})
        assert resolve_mode("autonomous", "s1") is True

    def test_sentinel_absent_fleet_on_does_not_override(self, _isolate_sentinel_and_fleet):
        """session-wins proof: a fleet autonomous:on must NOT override an
        absent session sentinel."""
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"autonomous": True})
        assert resolve_mode("autonomous", "s1") is False

    def test_sentinel_absent_fleet_off(self, _isolate_sentinel_and_fleet):
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"autonomous": False})
        assert resolve_mode("autonomous", "s1") is False

    def test_empty_fleet_mapping_reproduces_pre_plan_bool(self, _isolate_sentinel_and_fleet):
        # No fleet file at all -> fleet_mode.read_fleet_mode() degrades to
        # {} -> resolve_mode must reproduce today's sentinel-only bool.
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s2")
        assert resolve_mode("autonomous", "s2") is True
        assert resolve_mode("autonomous", "s3") is False


# --- compaction_warnings: fleet-wins -----------------------------------------


class TestCompactionWarningsFleetWins:
    def test_fleet_absent_defaults_to_standard(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("compaction_warnings", "s1") == "standard"

    def test_fleet_set_to_informational_wins(self, _isolate_sentinel_and_fleet):
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"compaction_warnings": "informational"})
        assert resolve_mode("compaction_warnings", "s1") == "informational"

    def test_fleet_set_to_standard_wins(self, _isolate_sentinel_and_fleet):
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"compaction_warnings": "standard"})
        assert resolve_mode("compaction_warnings", "s1") == "standard"

    def test_fleet_wrong_type_degrades_to_default(self, _isolate_sentinel_and_fleet):
        """A fleet-supplied value outside the declared enum is malformed
        input -- degrades exactly like an empty mapping, never coerced."""
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"compaction_warnings": "loud"})
        assert resolve_mode("compaction_warnings", "s1") == "standard"

    def test_empty_fleet_mapping_reproduces_pre_plan_default(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("compaction_warnings", "anyone") == "standard"


# --- unknown key: raises at the call site ------------------------------------


def test_unknown_key_raises_keyerror(_isolate_sentinel_and_fleet):
    with pytest.raises(KeyError):
        resolve_mode("not_a_real_key", "s1")


def test_unrecognised_key_inside_fleet_record_is_silently_absorbed(_isolate_sentinel_and_fleet):
    """An unrecognised key INSIDE the fleet record is untrusted input,
    already absorbed by fleet_mode's own degradation -- it must never raise
    here, and must not affect resolution of a real key."""
    _home = _isolate_sentinel_and_fleet[1]
    _write_fleet(_home, {"some_future_key": "whatever", "autonomous": False})
    assert resolve_mode("autonomous", "s1") is False


# --- registry invariant -------------------------------------------------------


class TestRegistryInvariant:
    def test_mode_keys_itself_satisfies_the_invariant(self):
        """Every shipped entry with session_pair=None declares fleet-wins."""
        _validate_registry(MODE_KEYS)
        for key, entry in MODE_KEYS.items():
            if entry.session_pair is None:
                assert entry.precedence == "fleet-wins", key

    def test_session_wins_with_no_session_pair_is_refused_at_definition_time(self):
        bad_registry = {
            "bogus": ModeKey(
                session_pair=None,
                precedence="session-wins",
                value_type=bool,
                default=False,
            )
        }
        with pytest.raises(ValueError):
            _validate_registry(bad_registry)

    def test_fleet_wins_with_no_session_pair_is_accepted(self):
        ok_registry = {
            "bogus": ModeKey(
                session_pair=None,
                precedence="fleet-wins",
                value_type=bool,
                default=False,
            )
        }
        _validate_registry(ok_registry)  # must not raise

    def test_mode_key_is_frozen(self):
        entry = MODE_KEYS["autonomous"]
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.precedence = "fleet-wins"
