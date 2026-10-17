"""live_plugin_registration is read-only: it reports the coordinator record, never writes it.

Plain `claude` is vanilla on a dev-sentinel box, so a coordinator record there is
drift; a consumer box keeps the marketplace record and a missing one is reported.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.install import plugin_cache_sessions
from coordinator_core.install.live_plugin_registration import (
    STATUS_DRIFT,
    STATUS_LIVE_ROOT_MISSING,
    STATUS_NO_COORDINATOR_RECORD,
    STATUS_NO_ENTRY,
    STATUS_NOT_DEV_CLONE,
    STATUS_UNREADABLE,
    STATUS_VANILLA,
    assert_live_plugin_registration,
    format_report,
    read_plugin_name,
)


@pytest.fixture(autouse=True)
def _no_live_sessions(monkeypatch):
    monkeypatch.setattr(plugin_cache_sessions, "sessions_bound_to", lambda *a, **k: [])


def _session(sid: str = "sess-1") -> plugin_cache_sessions.BoundSession:
    return plugin_cache_sessions.BoundSession(sid, "proj-a0", 4242, 1.0, "/x/cache", "bound")


def _drift_home(tmp_path: Path) -> Path:
    home = _home(tmp_path, None)
    (home / "plugins" / "installed_plugins.json").write_text(
        json.dumps(_coordinator_record(home)), encoding="utf-8"
    )
    return home


def test_drift_with_a_bound_session_names_it_and_the_relaunch(tmp_path, monkeypatch):
    home = _drift_home(tmp_path)
    seen = []

    def fake(path, **kw):
        seen.append((path, kw))
        return [_session()]

    monkeypatch.setattr(plugin_cache_sessions, "sessions_bound_to", fake)
    lines = format_report(assert_live_plugin_registration(home, _clone(tmp_path)))
    text = "\n".join(lines)
    assert lines[0].startswith("WARN") and "removal is the PM's call" in lines[0]
    assert "sess-1" in text and "4242" in text and "relaunch these sessions" in text
    assert seen == [(home / "plugins" / "cache" / "coordinator", {"claude_home": home})]


def test_drift_without_sessions_is_unchanged(tmp_path):
    home = _drift_home(tmp_path)
    lines = format_report(assert_live_plugin_registration(home, _clone(tmp_path)))
    assert len(lines) == 1 and "relaunch" not in lines[0]


def test_non_drift_arms_never_call_the_detector(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("detector called")

    monkeypatch.setattr(plugin_cache_sessions, "sessions_bound_to", boom)
    assert_live_plugin_registration(_home(tmp_path, _OTHER_ONLY), _clone(tmp_path))
    consumer = tmp_path / "c"
    consumer.mkdir()
    assert_live_plugin_registration(
        _drift_home(consumer), _clone(consumer, sentinel=False)
    )
    assert_live_plugin_registration(_home(consumer / "x", None), consumer / "missing")


def _clone(tmp_path: Path, name: str = "coordinator", sentinel: bool = True) -> Path:
    root = tmp_path / "clone" / "coordinator"
    (root / ".claude-plugin").mkdir(parents=True)
    if sentinel:
        (root.parent / ".coordinator-dev-repo").write_text("dev\n", encoding="utf-8")
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "4.0.0"}), encoding="utf-8"
    )
    return root


def _home(tmp_path: Path, records: dict | None) -> Path:
    home = tmp_path / "claude-home"
    (home / "plugins").mkdir(parents=True)
    if records is not None:
        (home / "plugins" / "installed_plugins.json").write_text(json.dumps(records), encoding="utf-8")
    return home


def _coordinator_record(home: Path, name: str = "coordinator") -> dict:
    return {"version": 2, "plugins": {f"{name}@coordinator-claude": [
        {"scope": "user", "installPath": str(home / "plugins" / "cache" / name), "gitCommitSha": "abc"}
    ]}}


_OTHER_ONLY = {"version": 2, "plugins": {"example-game-repo@x": [{"scope": "user", "installPath": "/c/h"}]}}


def _bytes(home: Path) -> bytes | None:
    p = home / "plugins" / "installed_plugins.json"
    return p.read_bytes() if p.is_file() else None


def test_sentinel_box_with_only_another_plugin_is_vanilla_not_the_old_pass(tmp_path):
    home = _home(tmp_path, _OTHER_ONLY)
    before = _bytes(home)

    report = assert_live_plugin_registration(home, _clone(tmp_path))

    lines = format_report(report)
    assert report["status"] == STATUS_VANILLA
    assert not any("already registered" in line for line in lines)
    assert _bytes(home) == before


def test_sentinel_box_without_record_file_is_vanilla(tmp_path):
    home = _home(tmp_path, None)
    assert assert_live_plugin_registration(home, _clone(tmp_path))["status"] == STATUS_VANILLA
    assert _bytes(home) is None


def test_sentinel_box_with_coordinator_record_reports_drift_and_writes_nothing(tmp_path):
    home = _home(tmp_path, None)
    (home / "plugins" / "installed_plugins.json").write_text(
        json.dumps(_coordinator_record(home)), encoding="utf-8"
    )
    before = _bytes(home)

    report = assert_live_plugin_registration(home, _clone(tmp_path))

    lines = format_report(report)
    assert report["status"] == STATUS_DRIFT
    assert len(lines) == 1 and lines[0].startswith("WARN")
    assert "coordinator@coordinator-claude" in lines[0] and "cache" in lines[0]
    assert _bytes(home) == before


def test_sentinel_box_drift_lists_every_matching_key(tmp_path):
    home = _home(tmp_path, {"version": 2, "plugins": {
        "coordinator@a": [{"installPath": "/p/a"}],
        "coordinator@b": [{"installPath": "/p/b"}],
        "example-game-repo@x": [{"installPath": "/p/h"}],
    }})
    report = assert_live_plugin_registration(home, _clone(tmp_path))
    assert [e["key"] for e in report["entries"]] == ["coordinator@a", "coordinator@b"]
    assert len(format_report(report)) == 2


def test_consumer_box_with_only_another_plugin_reports_missing_record(tmp_path):
    home = _home(tmp_path, _OTHER_ONLY)
    before = _bytes(home)

    report = assert_live_plugin_registration(home, _clone(tmp_path, sentinel=False))

    lines = format_report(report)
    assert report["status"] == STATUS_NO_COORDINATOR_RECORD
    assert lines and lines[0].startswith("WARN") and not any(l.startswith("PASS") for l in lines)
    assert "claude plugin install coordinator@" in lines[0]
    assert _bytes(home) == before


def test_consumer_box_without_record_file_reports_missing_record(tmp_path):
    report = assert_live_plugin_registration(_home(tmp_path, None), _clone(tmp_path, sentinel=False))
    assert report["status"] == STATUS_NO_COORDINATOR_RECORD


def test_consumer_box_with_coordinator_record_keeps_the_skip(tmp_path):
    home = _home(tmp_path, None)
    (home / "plugins" / "installed_plugins.json").write_text(
        json.dumps(_coordinator_record(home)), encoding="utf-8"
    )
    before = _bytes(home)

    report = assert_live_plugin_registration(home, _clone(tmp_path, sentinel=False))

    assert report["status"] == STATUS_NOT_DEV_CLONE
    assert all(line.startswith("SKIP") for line in format_report(report))
    assert _bytes(home) == before


def test_empty_record_list_is_not_a_coordinator_record(tmp_path):
    home = _home(tmp_path, {"version": 2, "plugins": {"coordinator@x": []}})
    assert assert_live_plugin_registration(home, _clone(tmp_path))["status"] == STATUS_VANILLA


def test_an_unreadable_record_degrades_rather_than_raising(tmp_path):
    home = _home(tmp_path, None)
    (home / "plugins" / "installed_plugins.json").write_text("{not json", encoding="utf-8")
    assert assert_live_plugin_registration(home, _clone(tmp_path))["status"] == STATUS_UNREADABLE


def test_a_nonexistent_live_root_is_reported(tmp_path):
    home = _home(tmp_path, _coordinator_record(Path("/x")))
    before = _bytes(home)
    report = assert_live_plugin_registration(home, tmp_path / "gone" / "coordinator")
    assert report["status"] == STATUS_LIVE_ROOT_MISSING
    assert _bytes(home) == before


def test_the_plugin_name_is_read_from_the_clone_never_hardcoded(tmp_path):
    clone = _clone(tmp_path, name="renamed-plugin")
    assert read_plugin_name(clone) == "renamed-plugin"
    home = _home(tmp_path, _coordinator_record(Path("/x"), name="renamed-plugin"))
    assert assert_live_plugin_registration(home, clone)["status"] == STATUS_DRIFT
    other = _home(tmp_path / "o", _coordinator_record(Path("/x"), name="coordinator"))
    assert assert_live_plugin_registration(other, clone)["status"] == STATUS_VANILLA


def test_a_clone_without_a_plugin_manifest_asserts_nothing(tmp_path):
    home = _home(tmp_path, {"version": 2, "plugins": {}})
    not_a_plugin = tmp_path / "repo" / "not-a-plugin"
    not_a_plugin.mkdir(parents=True)
    assert assert_live_plugin_registration(home, not_a_plugin)["status"] == STATUS_NO_ENTRY
