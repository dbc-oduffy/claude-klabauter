"""
test_register.py — pytest coverage for coordinator_core.p4.register.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § C7.

Cases named by the plan spine row C7:
  - shape-only repo_key validation accepts "p4-studio/game-main" and refuses
    a derived or multi-segment key.
  - a workspace with a pre-existing .p4ignore records its path.
  - a workspace without one records the no-`-a` condition.
  - the authored .gitignore excludes all four UE derived directories.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.p4 import register
from coordinator_core.p4.runner import P4Result


class TestValidateRepoKey:
    def test_accepts_single_slash_lowercase(self):
        assert register._validate_repo_key("p4-studio/game-main") == "p4-studio/game-main"

    def test_refuses_multi_segment_key(self):
        with pytest.raises(register.P4RegisterError):
            register._validate_repo_key("p4-studio/game/main")

    def test_refuses_zero_segment_key(self):
        with pytest.raises(register.P4RegisterError):
            register._validate_repo_key("game-main")

    def test_refuses_uppercase(self):
        with pytest.raises(register.P4RegisterError):
            register._validate_repo_key("P4-Studio/Game-Main")

    def test_refuses_non_string(self):
        with pytest.raises(register.P4RegisterError):
            register._validate_repo_key(None)


class TestValidateToolName:
    def test_absent_is_legal(self):
        assert register._validate_tool_name(None, "p4_submit_tool") is None

    def test_accepts_qualified_name(self):
        assert (
            register._validate_tool_name("p4mcp__submit", "p4_submit_tool")
            == "p4mcp__submit"
        )

    def test_refuses_unqualified_name(self):
        with pytest.raises(register.P4RegisterError):
            register._validate_tool_name("submit", "p4_submit_tool")


def _client_spec_result(root: str, host: str) -> P4Result:
    stdout = f"Root:\t{root}\nAltRoots:\nHost:\t{host}\n"
    return P4Result(ok=True, stdout=stdout)


class TestRegisterWorkspace:
    def _patch_confirm_and_registry(self, monkeypatch, repo_root, recorded):
        monkeypatch.setattr(
            register.runner,
            "run",
            lambda port, user, client, args, **kw: _client_spec_result(
                repo_root, register.socket.gethostname()
            ),
        )
        monkeypatch.setattr(
            register,
            "registry_set",
            lambda key, value: recorded.__setitem__(key, value),
        )

    def test_workspace_with_p4ignore_records_its_path(self, monkeypatch, tmp_path):
        (tmp_path / ".p4ignore").write_text("*.uasset\n", encoding="utf-8")
        recorded: dict = {}
        self._patch_confirm_and_registry(monkeypatch, str(tmp_path), recorded)

        result = register._register_workspace(
            {
                "repo_key": "p4-studio/game-main",
                "repo_root": str(tmp_path),
                "port": "ssl:p4.example.com:1666",
                "user": "agent",
                "client": "agent-ws",
            }
        )

        assert result["ok"] is True
        assert f"p4.p4-studio/game-main.p4ignore_path" in recorded
        assert "p4.p4-studio/game-main.p4ignore_absent" not in recorded
        p4ignore_text = (tmp_path / ".p4ignore").read_text(encoding="utf-8")
        assert ".git/" in p4ignore_text.splitlines()
        assert "*.uasset" in p4ignore_text.splitlines()

    def test_workspace_without_p4ignore_records_no_dash_a_condition(
        self, monkeypatch, tmp_path
    ):
        recorded: dict = {}
        self._patch_confirm_and_registry(monkeypatch, str(tmp_path), recorded)

        result = register._register_workspace(
            {
                "repo_key": "p4-studio/game-main",
                "repo_root": str(tmp_path),
                "port": "ssl:p4.example.com:1666",
                "user": "agent",
                "client": "agent-ws",
            }
        )

        assert result["ok"] is True
        assert recorded.get("p4.p4-studio/game-main.p4ignore_absent") == "true"
        assert "p4.p4-studio/game-main.p4ignore_path" not in recorded

    def test_authored_gitignore_excludes_all_four_derived_dirs(
        self, monkeypatch, tmp_path
    ):
        recorded: dict = {}
        self._patch_confirm_and_registry(monkeypatch, str(tmp_path), recorded)

        register._register_workspace(
            {
                "repo_key": "p4-studio/game-main",
                "repo_root": str(tmp_path),
                "port": "ssl:p4.example.com:1666",
                "user": "agent",
                "client": "agent-ws",
            }
        )

        gitignore_lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
        for derived in ("Intermediate/", "Saved/", "DerivedDataCache/", "Binaries/"):
            assert derived in gitignore_lines

    def test_invalid_repo_key_refuses_before_any_write(self, monkeypatch, tmp_path):
        recorded: dict = {}
        self._patch_confirm_and_registry(monkeypatch, str(tmp_path), recorded)

        result = register._register_workspace(
            {
                "repo_key": "game-main",
                "repo_root": str(tmp_path),
                "port": "ssl:p4.example.com:1666",
                "user": "agent",
                "client": "agent-ws",
            }
        )

        assert result["ok"] is False
        assert not recorded
        assert not (tmp_path / ".p4ignore").exists()
        assert not (tmp_path / ".gitignore").exists()

    def test_vcs_mirror_written_to_coordinator_local_md_not_registry(
        self, monkeypatch, tmp_path
    ):
        recorded: dict = {}
        self._patch_confirm_and_registry(monkeypatch, str(tmp_path), recorded)

        register._register_workspace(
            {
                "repo_key": "p4-studio/game-main",
                "repo_root": str(tmp_path),
                "port": "ssl:p4.example.com:1666",
                "user": "agent",
                "client": "agent-ws",
            }
        )

        local_md = (tmp_path / "coordinator.local.md").read_text(encoding="utf-8")
        assert "vcs_mirror: p4" in local_md
        assert not any("vcs_mirror" in k for k in recorded)

    def test_repo_key_is_readable_from_coordinator_local_md(
        self, monkeypatch, tmp_path
    ):
        """Every identity lookup is keyed `p4.<repo_key>.*`, so a caller that
        cannot obtain the key cannot reach the machine-local store at all.
        The key is repo-true and identical on every box — and it cannot live
        in the registry it is needed to read."""
        recorded: dict = {}
        self._patch_confirm_and_registry(monkeypatch, str(tmp_path), recorded)

        register._register_workspace(
            {
                "repo_key": "p4-studio/game-main",
                "repo_root": str(tmp_path),
                "port": "ssl:p4.example.com:1666",
                "user": "agent",
                "client": "agent-ws",
            }
        )

        local_md = (tmp_path / "coordinator.local.md").read_text(encoding="utf-8")
        assert "p4_repo_key: p4-studio/game-main" in local_md

    def test_gitattributes_conflict_refuses_loudly(self, monkeypatch, tmp_path):
        (tmp_path / ".gitattributes").write_text(
            "*.cpp text eol=lf\n", encoding="utf-8"
        )
        recorded: dict = {}
        self._patch_confirm_and_registry(monkeypatch, str(tmp_path), recorded)

        result = register._register_workspace(
            {
                "repo_key": "p4-studio/game-main",
                "repo_root": str(tmp_path),
                "port": "ssl:p4.example.com:1666",
                "user": "agent",
                "client": "agent-ws",
            }
        )

        assert result["ok"] is False
        gitattrs_text = (tmp_path / ".gitattributes").read_text(encoding="utf-8")
        assert "* -text" not in gitattrs_text

    def test_gitattributes_appended_when_no_conflict(self, monkeypatch, tmp_path):
        recorded: dict = {}
        self._patch_confirm_and_registry(monkeypatch, str(tmp_path), recorded)

        result = register._register_workspace(
            {
                "repo_key": "p4-studio/game-main",
                "repo_root": str(tmp_path),
                "port": "ssl:p4.example.com:1666",
                "user": "agent",
                "client": "agent-ws",
            }
        )

        assert result["ok"] is True
        gitattrs_lines = (tmp_path / ".gitattributes").read_text(encoding="utf-8").splitlines()
        assert "* -text" in gitattrs_lines


class TestRootContainsCaseHandling:
    """Review: code-reviewer F2. A `p4 client -o` `Root:`/`AltRoots:` value
    routinely differs in casing from the OS-reported repo directory on
    Windows (hand-authored client specs vs. tool-cloned repos)."""

    def test_case_differing_root_is_accepted(self, tmp_path):
        repo_root_resolved = tmp_path.resolve()
        differently_cased = str(repo_root_resolved).swapcase()

        result = register._root_contains(differently_cased, repo_root_resolved)

        import os as _os

        if _os.name == "nt":
            assert result is True
        else:
            # POSIX filesystems are case-sensitive by default -- this
            # fix is Windows-only by design (`os.path.normcase` is
            # identity on POSIX), so a differently-cased root is
            # legitimately still refused there.
            assert result is False

    def test_sibling_directory_sharing_a_name_prefix_is_still_refused(self, tmp_path):
        real_repo = tmp_path / "claude-klabauter"
        real_repo.mkdir()
        sibling = tmp_path / "claude-klabauter-other"
        sibling.mkdir()

        assert register._root_contains(str(sibling), real_repo.resolve()) is False
