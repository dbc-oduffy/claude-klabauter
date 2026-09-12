"""
test_workspace.py — pytest coverage for coordinator_core.p4.workspace.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § D1, § D9.

Cases named by the plan spine row C1:
  - an absent marker key -> is_p4_repo() False, with no spawn (no runner
    import anywhere in this module — asserted by absence, not mocked).
  - a marker present with no machine-local row -> identity() raises the
    typed P4WorkspaceUnregistered.
  - session_change() reads meta.json's four S3 fields, absent -> None.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.p4 import workspace


def _write_local_md(repo_root, vcs_mirror: str | None) -> None:
    body = "---\n"
    if vcs_mirror is not None:
        body += f"vcs_mirror: {vcs_mirror}\n"
    body += "---\n"
    (repo_root / "coordinator.local.md").write_text(body, encoding="utf-8")


class TestIsP4Repo:
    def test_marker_present_p4(self, tmp_path):
        _write_local_md(tmp_path, "p4")
        assert workspace.is_p4_repo(str(tmp_path)) is True

    def test_marker_absent_returns_false_no_spawn(self, tmp_path):
        # No coordinator.local.md at all.
        assert workspace.is_p4_repo(str(tmp_path)) is False

    def test_marker_key_absent_from_file_returns_false(self, tmp_path):
        _write_local_md(tmp_path, None)
        assert workspace.is_p4_repo(str(tmp_path)) is False

    def test_marker_present_but_not_p4_returns_false(self, tmp_path):
        _write_local_md(tmp_path, "git")
        assert workspace.is_p4_repo(str(tmp_path)) is False

    def test_module_never_imports_runner_at_top_level(self):
        # D1: workspace.py never imports runner.py — a git-only process
        # loading coordinator_core.p4.workspace must never pull in the p4
        # spawn helper.
        assert "runner" not in vars(workspace)


class TestIdentity:
    def test_full_row_present(self, monkeypatch):
        values = {
            "p4.studio/repo.port": "ssl:p4.example.com:1666",
            "p4.studio/repo.user": "agent",
            "p4.studio/repo.client": "agent-ws",
            "p4.studio/repo.client_root": "X:/p4-workspace",  # abs-path-ok: fixture string, never resolved as a real path
        }
        monkeypatch.setattr(workspace, "registry_get", lambda key: values.get(key))
        ident = workspace.identity("studio/repo")
        assert ident.port == "ssl:p4.example.com:1666"
        assert ident.user == "agent"
        assert ident.client == "agent-ws"
        assert ident.client_root == "X:/p4-workspace"

    def test_no_machine_local_row_raises_typed_error(self, monkeypatch):
        monkeypatch.setattr(workspace, "registry_get", lambda key: None)
        with pytest.raises(workspace.P4WorkspaceUnregistered) as exc_info:
            workspace.identity("studio/repo")
        assert exc_info.value.repo_key == "studio/repo"

    def test_partial_row_raises_typed_error(self, monkeypatch):
        values = {
            "p4.studio/repo.port": "ssl:p4.example.com:1666",
            "p4.studio/repo.user": "agent",
            # client, root missing
        }
        monkeypatch.setattr(workspace, "registry_get", lambda key: values.get(key))
        with pytest.raises(workspace.P4WorkspaceUnregistered):
            workspace.identity("studio/repo")


class TestSessionChange:
    def test_all_fields_present(self, tmp_path):
        meta = {
            "p4_change": 41,
            "p4_base_sha": "abc123",
            "p4_shelved_at": "2026-09-12T00:00:00Z",
            "p4_shelved_sha": "def456",
        }
        (tmp_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        result = workspace.session_change(str(tmp_path))
        assert result == {
            "p4_change": 41,
            "p4_base_sha": "abc123",
            "p4_shelved_at": "2026-09-12T00:00:00Z",
            "p4_shelved_sha": "def456",
        }

    def test_absent_meta_json_returns_all_none(self, tmp_path):
        result = workspace.session_change(str(tmp_path))
        assert result == {
            "p4_change": None,
            "p4_base_sha": None,
            "p4_shelved_at": None,
            "p4_shelved_sha": None,
        }

    def test_absent_fields_return_none(self, tmp_path):
        (tmp_path / "meta.json").write_text(json.dumps({}), encoding="utf-8")
        result = workspace.session_change(str(tmp_path))
        assert result["p4_change"] is None
        assert result["p4_base_sha"] is None

    def test_p4_change_written_as_string_still_coerces_to_int(self, tmp_path):
        # session/core.py::update_meta_fields string-coerces every field it
        # writes (D8's open cross-repo question) — the reader must tolerate
        # a decimal-string p4_change either way.
        (tmp_path / "meta.json").write_text(
            json.dumps({"p4_change": "41"}), encoding="utf-8"
        )
        result = workspace.session_change(str(tmp_path))
        assert result["p4_change"] == 41
