"""
test_session_state.py — pytest coverage for coordinator_core.p4.session_state.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § C7, § D9.

Cases named by the plan spine row C7:
  - registered workspace + populated sdir -> registered True, client/port
    plus all four p4_* fields populated, sourced from C1's readers.
  - unregistered (or repo_key omitted) -> registered False, client/port None.
  - sdir omitted -> all four p4_* fields None, no meta.json read attempted.
"""

from __future__ import annotations

import json

from coordinator_core.p4 import session_state, workspace


def test_registered_workspace_with_session_returns_full_state(monkeypatch, tmp_path):
    values = {
        "p4.studio/repo.port": "ssl:p4.example.com:1666",
        "p4.studio/repo.user": "agent",
        "p4.studio/repo.client": "agent-ws",
        "p4.studio/repo.client_root": "X:/p4-workspace",  # abs-path-ok: fixture string, never resolved as a real path
    }
    monkeypatch.setattr(workspace, "registry_get", lambda key: values.get(key))
    meta = {
        "p4_change": 41,
        "p4_base_sha": "abc123",
        "p4_shelved_at": "2026-09-12T00:00:00Z",
        "p4_shelved_sha": "def456",
    }
    (tmp_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    result = session_state._session_state(
        {"repo_key": "studio/repo", "sdir": str(tmp_path)}
    )

    assert result == {
        "registered": True,
        "client": "agent-ws",
        "port": "ssl:p4.example.com:1666",
        "p4_change": 41,
        "p4_base_sha": "abc123",
        "p4_shelved_at": "2026-09-12T00:00:00Z",
        "p4_shelved_sha": "def456",
    }


def test_unregistered_workspace_returns_registered_false(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace, "registry_get", lambda key: None)
    (tmp_path / "meta.json").write_text(json.dumps({}), encoding="utf-8")

    result = session_state._session_state(
        {"repo_key": "studio/repo", "sdir": str(tmp_path)}
    )

    assert result["registered"] is False
    assert result["client"] is None
    assert result["port"] is None


def test_repo_key_omitted_reads_as_unregistered(monkeypatch, tmp_path):
    # No registry_get monkeypatch needed — repo_key is falsy so identity()
    # is never called at all.
    result = session_state._session_state({"sdir": str(tmp_path)})
    assert result["registered"] is False
    assert result["client"] is None
    assert result["port"] is None


def test_sdir_omitted_returns_all_none_p4_fields(monkeypatch):
    values = {
        "p4.studio/repo.port": "ssl:p4.example.com:1666",
        "p4.studio/repo.user": "agent",
        "p4.studio/repo.client": "agent-ws",
        "p4.studio/repo.client_root": "X:/p4-workspace",  # abs-path-ok: fixture string, never resolved as a real path
    }
    monkeypatch.setattr(workspace, "registry_get", lambda key: values.get(key))

    result = session_state._session_state({"repo_key": "studio/repo"})

    assert result["registered"] is True
    assert result["client"] == "agent-ws"
    assert result["p4_change"] is None
    assert result["p4_base_sha"] is None
    assert result["p4_shelved_at"] is None
    assert result["p4_shelved_sha"] is None
