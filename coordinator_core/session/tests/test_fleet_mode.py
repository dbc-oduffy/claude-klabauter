"""
coordinator_core.session.tests.test_fleet_mode — tests for
coordinator_core.session.fleet_mode, the fleet record: one home, atomic
writes, and degradation to today's behaviour.

Isolation is via ``COORDINATOR_SETTINGS_HOME`` (rung 0 of
``_settings_home.settings_home()``'s own precedence) pointed at a fresh
``tmp_path`` per test — no git repo needed, unlike the session-scoped
``grant``/``shape`` fixtures, because this record is settings-home-rooted,
not session-rooted.

Spec backlink: state/dispatch-briefs/2026-08-28-the-fleet-gets-one-file-and-the-floor-moves-to-the-reader/C1.md
"""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.session import fleet_mode


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return tmp_path


def test_fleet_mode_path_is_settings_home_rooted(isolated_home):
    path = fleet_mode.fleet_mode_path()
    assert path == isolated_home / "fleet-mode.json"
    assert path.parent == isolated_home


def test_round_trip_write_then_read(isolated_home):
    record = {"mode": "solo", "set_by": "pm", "set_at": "2026-08-29T00:00:00Z"}
    assert fleet_mode.write_fleet_mode(record) is True
    assert fleet_mode.read_fleet_mode() == record


def test_round_trip_overwrite(isolated_home):
    fleet_mode.write_fleet_mode({"mode": "solo"})
    fleet_mode.write_fleet_mode({"mode": "fleet"})
    assert fleet_mode.read_fleet_mode() == {"mode": "fleet"}


def test_read_absent_file_returns_empty_mapping(isolated_home):
    assert fleet_mode.fleet_mode_path().exists() is False
    assert fleet_mode.read_fleet_mode() == {}


def test_read_unreadable_file_returns_empty_mapping(isolated_home, monkeypatch):
    fleet_mode.write_fleet_mode({"mode": "solo"})

    real_read_text = fleet_mode.Path.read_text

    def _raise_oserror(self, *args, **kwargs):
        raise OSError("simulated unreadable file")

    monkeypatch.setattr(fleet_mode.Path, "read_text", _raise_oserror)
    assert fleet_mode.read_fleet_mode() == {}
    monkeypatch.setattr(fleet_mode.Path, "read_text", real_read_text)


def test_read_malformed_json_returns_empty_mapping(isolated_home):
    path = fleet_mode.fleet_mode_path()
    path.write_text("{not valid json", encoding="utf-8")
    assert fleet_mode.read_fleet_mode() == {}


def test_read_valid_json_wrong_shape_returns_empty_mapping(isolated_home):
    path = fleet_mode.fleet_mode_path()
    path.write_text(json.dumps(["a", "list", "not", "a", "dict"]), encoding="utf-8")
    assert fleet_mode.read_fleet_mode() == {}


def test_read_unknown_key_still_returns_the_mapping_as_is(isolated_home):
    # An unknown key is a caller-side degradation (the caller does not
    # recognize the key and falls back to today's behaviour) -- the reader
    # itself has no schema to enforce, so it still returns the parsed dict.
    record = {"totally_unrecognized_key": "value"}
    fleet_mode.write_fleet_mode(record)
    assert fleet_mode.read_fleet_mode() == record


def test_write_atomicity_no_partial_file_observable(isolated_home, monkeypatch):
    fleet_mode.write_fleet_mode({"mode": "solo"})

    real_replace = os.replace
    seen_tmp_files = []

    def _spy_replace(src, dst):
        # At the instant os.replace is invoked, the destination must still
        # hold the OLD complete record (or be absent on first write) --
        # never a partially-written new one, and the source must be a
        # separate tmp file that is a fully-formed JSON document.
        seen_tmp_files.append(src)
        with open(src, "r", encoding="utf-8") as fh:
            json.loads(fh.read())  # must parse whole -- no partial content
        return real_replace(src, dst)

    monkeypatch.setattr(fleet_mode.os, "replace", _spy_replace)
    assert fleet_mode.write_fleet_mode({"mode": "fleet"}) is True
    assert len(seen_tmp_files) == 1
    assert fleet_mode.read_fleet_mode() == {"mode": "fleet"}


def test_write_requires_dict(isolated_home):
    with pytest.raises(TypeError):
        fleet_mode.write_fleet_mode(["not", "a", "dict"])  # type: ignore[arg-type]


def test_write_non_serializable_dict_value_leaves_no_tmp_file(isolated_home):
    """A dict record passes the isinstance(dict) gate but can still fail
    json.dump on a non-serializable value (e.g. a raw datetime). This must
    degrade to False, not propagate, and must not leak the mkstemp tmp file
    into settings_home() (Review: code-reviewer, finding 1)."""
    import datetime

    record = {"set_at": datetime.datetime(2026, 8, 29)}
    assert fleet_mode.write_fleet_mode(record) is False
    leftover = [
        p for p in isolated_home.iterdir() if p.name.startswith("fleet-mode.json.")
    ]
    assert leftover == []
    assert fleet_mode.fleet_mode_path().exists() is False
