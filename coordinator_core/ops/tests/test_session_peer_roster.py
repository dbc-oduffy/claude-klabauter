"""
coordinator_core.ops.tests.test_session_peer_roster — JSON-RPC veneer tests
for "session.peer_roster".

Spec backlink: `state/handoffs/2026-08-13-live-peer-roster.md` §§ 1-2, 4
"""

from __future__ import annotations

import os
import re

from coordinator_core.ops.session_peer_roster import _session_peer_roster
from coordinator_core.session import harness_registry as hr


def _record(name, socket, cwd="/repo/claude-klabauter"):
    return hr.RegistryRecord(
        pid=1, start_epoch=1000.0, cwd=cwd, name=name, messaging_socket_path=socket
    )


def test_default_repo_root_uses_cwd(monkeypatch):
    snap = {
        "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter"),
        "sid-b": _record("other-12", "/sock/b.sock", cwd="/repo/other"),
    }
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)
    monkeypatch.setattr(os, "getcwd", lambda: "/repo/claude-klabauter")

    result = _session_peer_roster({})
    ids = {row["session_id"] for row in result["rows"]}
    assert ids == {"sid-a"}


def test_explicit_repo_root_param_filters(monkeypatch):
    snap = {
        "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter"),
        "sid-b": _record("other-12", "/sock/b.sock", cwd="/repo/other"),
    }
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)

    result = _session_peer_roster({"repo_root": "/repo/other"})
    ids = {row["session_id"] for row in result["rows"]}
    assert ids == {"sid-b"}


def test_row_shape(monkeypatch):
    snap = {"sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter")}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: ("sid-a", snap["sid-a"]))

    result = _session_peer_roster({"repo_root": "/repo/claude-klabauter"})
    row = result["rows"][0]
    assert set(row) == {
        "session_id",
        "address",
        "name",
        "ref",
        "cwd",
        "status",
        "running_seconds",
        "is_self",
        "self_determination",
        "messaging_available",
    }
    assert row["is_self"] is True
    # Ref-qualified unconditionally -- see reachability's module docstring;
    # a bare name is not a sendable address even without a collision.
    assert re.fullmatch(r"claude-klabauter-57 \[[0-9a-f]{6,12}\]", row["address"])
    assert row["self_determination"] == "resolved"
    assert row["messaging_available"] is True


def test_null_address_rows_say_the_harness_inbox_is_unbound(monkeypatch):
    """The 17-null-address roster case: every row's `address` is `None`
    for one harness-wide reason, and the row itself now carries it."""
    snap = {
        "sid-a": _record("claude-klabauter-57", None, cwd="/repo/claude-klabauter"),
        "sid-b": _record("claude-klabauter-58", None, cwd="/repo/claude-klabauter"),
    }
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)

    rows = _session_peer_roster({"repo_root": "/repo/claude-klabauter"})["rows"]
    assert len(rows) == 2
    assert all(row["address"] is None for row in rows)
    assert all(row["messaging_available"] is False for row in rows)


def test_empty_registry_yields_empty_rows_no_raise():
    result = _session_peer_roster({"repo_root": "/repo/claude-klabauter"})
    assert result == {"rows": []} or result["rows"] == []
