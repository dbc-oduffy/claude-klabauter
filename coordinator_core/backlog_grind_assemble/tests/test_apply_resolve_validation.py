from __future__ import annotations

import pytest

from coordinator_core.backlog_grind_assemble import apply as bga_apply
from coordinator_core.backlog_grind_assemble import directives as bga_directives
from coordinator_core.resolve_validation_cmd import ResolvedCommand


class TestResolveValidationCmdRegistration:
    def test_registered_in_closed_dispatch_table(self):
        assert (
            bga_apply._CLI_DISPATCH[bga_directives._RESOLVE_VALIDATION_CMD_CLI]
            is bga_apply._dispatch_resolve_validation_cmd
        )

    def test_shares_one_cli_constant_with_directives_module(self):
        assert bga_apply._RESOLVE_VALIDATION_CMD_CLI is bga_directives._RESOLVE_VALIDATION_CMD_CLI


class TestDispatchResolveValidationCmd:
    def test_full_resolution_rc0_reports_full_coverage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            bga_apply,
            "cs_resolve_full_test_cmd",
            lambda repo_root: ResolvedCommand("pytest -x", 0),
        )
        result = bga_apply._dispatch_resolve_validation_cmd([], tmp_path)
        assert result == {
            "cli": bga_directives._RESOLVE_VALIDATION_CMD_CLI,
            "exit_code": 0,
            "status": "resolved",
            "coverage": "full",
            "resolved_cmd": "pytest -x",
        }

    def test_fast_tier_fallback_rc3_surfaces_the_downgrade_not_silently(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            bga_apply,
            "cs_resolve_full_test_cmd",
            lambda repo_root: ResolvedCommand("pytest -m fast", 3),
        )
        result = bga_apply._dispatch_resolve_validation_cmd([], tmp_path)
        assert result["exit_code"] == 3
        assert result["coverage"] == "fast-tier-only"
        assert result["resolved_cmd"] == "pytest -m fast"
        assert result["fallback"] is True

    def test_unconfigured_rc2_reports_skipped_not_failed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            bga_apply,
            "cs_resolve_full_test_cmd",
            lambda repo_root: ResolvedCommand(None, 2),
        )
        result = bga_apply._dispatch_resolve_validation_cmd([], tmp_path)
        assert result["exit_code"] == 2
        assert result["status"] == "skipped"
        assert result["resolved_cmd"] is None

    def test_hard_environment_failure_rc127_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            bga_apply,
            "cs_resolve_full_test_cmd",
            lambda repo_root: ResolvedCommand(None, 127),
        )
        with pytest.raises(RuntimeError, match="rc=127"):
            bga_apply._dispatch_resolve_validation_cmd([], tmp_path)

    def test_calls_in_process_with_repo_root_as_string(self, tmp_path, monkeypatch):
        seen = {}

        def _fake(repo_root):
            seen["repo_root"] = repo_root
            return ResolvedCommand("cmd", 0)

        monkeypatch.setattr(bga_apply, "cs_resolve_full_test_cmd", _fake)
        bga_apply._dispatch_resolve_validation_cmd([], tmp_path)
        assert seen["repo_root"] == str(tmp_path)


class TestBuildResolveValidationCmd:
    def test_builds_bare_directive_with_empty_args(self):
        directive = bga_directives.build_resolve_validation_cmd(id="d-resolve")
        assert directive == {
            "id": "d-resolve",
            "cli": bga_directives._RESOLVE_VALIDATION_CMD_CLI,
            "args": [],
            "depends_on": None,
        }

    def test_optional_depends_on_threads_through(self):
        directive = bga_directives.build_resolve_validation_cmd(
            id="d-resolve", depends_on="jp-1"
        )
        assert directive["depends_on"] == "jp-1"
