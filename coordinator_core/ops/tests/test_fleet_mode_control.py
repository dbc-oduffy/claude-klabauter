"""
coordinator_core.ops.tests.test_fleet_mode_control -- tests for
coordinator_core.ops.fleet.mode_control ("fleet.mode_set" /
"fleet.mode_show").

Isolation is via ``COORDINATOR_SETTINGS_HOME`` (rung 0 of
``_settings_home.settings_home()``'s own precedence) pointed at a fresh
``tmp_path`` per test -- matching
``coordinator_core/session/tests/test_fleet_mode.py``'s own isolation
discipline for the record layer this module sits on top of.

Spec backlink: state/dispatch-briefs/2026-08-28-the-fleet-gets-one-file-and-the-floor-moves-to-the-reader/C4.md
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.fleet import mode_control


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return tmp_path


# --- set/show round-trip ---------------------------------------------------


def test_set_then_show_round_trip_bool_key(isolated_home):
    record = mode_control.set_fleet_mode_key("autonomous", "on")
    assert record["autonomous"] is True

    rendered = mode_control.show_fleet_mode()
    entry = _entry(rendered, "autonomous")
    assert entry["fleet_value"] is True
    assert entry["precedence"] == "session-wins"


def test_set_then_show_round_trip_enum_key(isolated_home):
    record = mode_control.set_fleet_mode_key("compaction_warnings", "informational")
    assert record["compaction_warnings"] == "informational"

    rendered = mode_control.show_fleet_mode()
    entry = _entry(rendered, "compaction_warnings")
    assert entry["fleet_value"] == "informational"
    assert entry["precedence"] == "fleet-wins"
    assert entry["variant_that_fires"] == "informational"


def test_set_preserves_other_keys(isolated_home):
    mode_control.set_fleet_mode_key("autonomous", "on")
    mode_control.set_fleet_mode_key("compaction_warnings", "informational")
    record = mode_control.set_fleet_mode_key("autonomous", "off")
    assert record == {"autonomous": False, "compaction_warnings": "informational"}


def test_bool_accepts_true_false_tokens(isolated_home):
    record = mode_control.set_fleet_mode_key("autonomous", "true")
    assert record["autonomous"] is True
    record = mode_control.set_fleet_mode_key("autonomous", "FALSE")
    assert record["autonomous"] is False


# --- unknown key / bad value rejection --------------------------------------


def test_set_unknown_key_rejected_with_known_key_list(isolated_home):
    with pytest.raises(ValueError) as exc_info:
        mode_control.set_fleet_mode_key("not_a_real_key", "on")
    message = str(exc_info.value)
    assert "not_a_real_key" in message
    assert "autonomous" in message
    assert "compaction_warnings" in message


def test_set_bad_bool_value_rejected(isolated_home):
    with pytest.raises(ValueError) as exc_info:
        mode_control.set_fleet_mode_key("autonomous", "maybe")
    assert "autonomous" in str(exc_info.value)


def test_set_bad_enum_value_rejected(isolated_home):
    with pytest.raises(ValueError) as exc_info:
        mode_control.set_fleet_mode_key("compaction_warnings", "silent")
    message = str(exc_info.value)
    assert "compaction_warnings" in message
    assert "standard" in message
    assert "informational" in message


def test_bad_set_does_not_write_anything(isolated_home):
    with pytest.raises(ValueError):
        mode_control.set_fleet_mode_key("not_a_real_key", "on")
    assert mode_control.read_fleet_mode() == {}


# --- show self-explains precedence and variant-selector floor --------------


def test_show_names_precedence_rule_per_key(isolated_home):
    rendered = mode_control.show_fleet_mode()
    keys = {entry["key"]: entry for entry in rendered["keys"]}
    assert keys["autonomous"]["precedence"] == "session-wins"
    assert "session" in keys["autonomous"]["wins"]
    assert keys["compaction_warnings"]["precedence"] == "fleet-wins"
    assert "fleet" in keys["compaction_warnings"]["wins"]


def test_show_names_variant_that_fires_and_declares_unsuppressible(isolated_home):
    rendered = mode_control.show_fleet_mode()
    entry = _entry(rendered, "compaction_warnings")
    assert entry["is_variant_selector"] is True
    assert entry["suppressible"] is False
    # Absent fleet value still names a concrete variant that will fire.
    assert entry["variant_that_fires"] in ("standard", "informational")


def test_show_variant_that_fires_tracks_the_set_value(isolated_home):
    mode_control.set_fleet_mode_key("compaction_warnings", "informational")
    entry = _entry(mode_control.show_fleet_mode(), "compaction_warnings")
    assert entry["variant_that_fires"] == "informational"


def test_show_absent_file_still_renders_every_known_key(isolated_home):
    rendered = mode_control.show_fleet_mode()
    seen = {entry["key"] for entry in rendered["keys"]}
    assert seen == set(mode_control.known_keys())
    for entry in rendered["keys"]:
        assert entry["fleet_value"] is None


# --- no subprocess, no session enumeration ----------------------------------


def test_set_spawns_no_subprocess(isolated_home, monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("mode_control must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    mode_control.set_fleet_mode_key("autonomous", "on")


def test_show_spawns_no_subprocess(isolated_home, monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("mode_control must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    mode_control.show_fleet_mode()


def test_module_resolves_no_session_list():
    # Static assertion, matching C4's own negative-spec: the module names
    # no session-registry / peer-address / messaging surface at all.
    import inspect

    source = inspect.getsource(mode_control)
    forbidden_terms = ("session_registry", "resolve_peer", "dispatch_message", "SessionRegistry")
    for term in forbidden_terms:
        assert term not in source, f"mode_control.py must not reference {term!r}"


# --- op handlers -------------------------------------------------------------


def test_op_handler_set_requires_key_and_value(isolated_home):
    with pytest.raises(ValueError):
        mode_control._fleet_mode_set({}, repo_root=None)
    with pytest.raises(ValueError):
        mode_control._fleet_mode_set({"key": "autonomous"}, repo_root=None)


def test_op_handler_set_and_show_round_trip(isolated_home):
    result = mode_control._fleet_mode_set({"key": "autonomous", "value": "on"}, repo_root=None)
    assert result["autonomous"] is True

    shown = mode_control._fleet_mode_show({}, repo_root=None)
    entry = _entry(shown, "autonomous")
    assert entry["fleet_value"] is True


def _entry(rendered: dict, key: str) -> dict:
    for entry in rendered["keys"]:
        if entry["key"] == key:
            return entry
    raise AssertionError(f"key {key!r} not found in show_fleet_mode() output")
