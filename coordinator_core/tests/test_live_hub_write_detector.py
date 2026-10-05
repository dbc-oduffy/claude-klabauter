"""The conftest live-hub write detector flags in-process writes into non-UUID entries and ignores peers."""

from __future__ import annotations

import os
import uuid

import pytest

from coordinator_core import conftest as _cc

_UUID = str(uuid.uuid4())


@pytest.fixture
def hub(tmp_path):
    hub = tmp_path / "coordinator-sessions"
    (hub / "test-session").mkdir(parents=True)
    (hub / _UUID).mkdir()
    return hub


def test_write_into_existing_non_uuid_entry_is_flagged(hub):
    target = hub / "test-session" / "repo-identity-gate.log"
    assert _cc._writes_into_hub_entries([str(target)], str(hub)) == [
        os.path.normcase(os.path.join("test-session", "repo-identity-gate.log"))
    ]


def test_write_into_uuid_entry_is_ignored(hub):
    assert _cc._writes_into_hub_entries([str(hub / _UUID / "meta.json")], str(hub)) == []


def test_write_outside_the_hub_and_to_the_hub_root_is_ignored(hub, tmp_path):
    assert _cc._writes_into_hub_entries([str(tmp_path / "x.log"), str(hub)], str(hub)) == []


def test_top_level_plain_file_counts_as_an_entry(hub):
    assert _cc._writes_into_hub_entries([str(hub / "x.lock")], str(hub)) == ["x.lock"]


def test_audit_hook_records_writes_only_while_armed(hub, monkeypatch):
    monkeypatch.setattr(_cc, "_hub_written", set())
    target = str(hub / "test-session" / "a.log")
    flags = os.O_WRONLY | os.O_CREAT
    monkeypatch.setattr(_cc, "_hub_watch_active", False)
    _cc._hub_audit_hook("open", (target, None, flags))
    assert _cc._hub_written == set()
    monkeypatch.setattr(_cc, "_hub_watch_active", True)
    _cc._hub_audit_hook("open", (target, "r", os.O_RDONLY))
    assert _cc._hub_written == set()
    _cc._hub_audit_hook("open", (target, "a", flags))
    _cc._hub_audit_hook("os.remove", (str(hub / "test-session" / "b.log"), -1))
    assert len(_cc._hub_written) == 2
