"""test_p4_verb_fence.py -- pytest coverage for
coordinator_core.bash_guards.p4_verb_fence (C6, D6/D7/S4).

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
plan-spine row C6.

Cases named by the C6 row body:
  - `submit` denied outright, distinct "no submit provider installed" message.
  - Unparseable p4 invocations denied: `-x`, `P4ALIASES`, `p4vc`, `git p4`.
  - `attrib -r` / `chmod +w` denied.
  - A bare `reconcile -n` denied; a path-scoped `reconcile -n` allowed.
  - D7's full git verb list denied; `git clean -fdx` denied (depot content,
    not merely the read-only bit); `reset --mixed`/`--soft` stay allowed.
  - Example-Game-Repo's own flag-interposed submit form
    (`p4 -p ... -u ... -c ... submit`) denied the same as a bare `p4 submit`.
  - Zero spawns on every path -- asserted with a runner spy that raises if
    ever called.
  - A git-only repo (no `vcs_mirror: p4` marker) pays nothing: `check()`
    returns `None` even for a literal `p4 submit`/`git clean -fdx` command.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import p4_verb_fence
from coordinator_core.p4 import runner as p4_runner

#: Captured before any test monkeypatches `p4_verb_fence._is_p4_gated` --
#: the two "real filesystem walk" cases below need the UNPATCHED function,
#: since the autouse `_p4_gated` fixture below patches that same name for
#: every other test in this module.
_REAL_IS_P4_GATED = p4_verb_fence._is_p4_gated


def _payload(cmd: str, tool_name: str = "Bash", cwd: str = "/repo") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"command": cmd},
        "cwd": cwd,
        "session_id": "sess-1",
    }


@pytest.fixture(autouse=True)
def _p4_gated(monkeypatch):
    """Every test in this module runs as if `cwd` resolved to a p4-marker
    repo, unless a test overrides `_is_p4_gated` itself (the git-only-repo
    class below)."""
    monkeypatch.setattr(p4_verb_fence, "_is_p4_gated", lambda cwd: True)


@pytest.fixture(autouse=True)
def _no_spawns(monkeypatch):
    """Zero-spawn assertion: a runner spy that raises if the one p4 spawn
    helper is EVER invoked while a command runs through `check()`."""

    def _spy(*args, **kwargs):
        raise AssertionError("p4_verb_fence.check() spawned a process via p4.runner.run")

    monkeypatch.setattr(p4_runner, "run", _spy)


def _deny(cmd: str, tool_name: str = "Bash") -> dict:
    result = p4_verb_fence.check(_payload(cmd, tool_name=tool_name))
    assert result is not None, "expected a deny for %r" % cmd
    return result["hookSpecificOutput"]


def _allow(cmd: str, tool_name: str = "Bash") -> None:
    result = p4_verb_fence.check(_payload(cmd, tool_name=tool_name))
    assert result is None, "expected an allow for %r, got %r" % (cmd, result)


class TestSubmitDenied:
    def test_bare_submit_denied(self):
        env = _deny("p4 submit -d 'commit'")
        assert env["permissionDecision"] == "deny"
        assert "no submit provider installed" in env["permissionDecisionReason"]

    def test_flag_interposed_submit_denied(self):
        # Example-Game-Repo's spike broke a phrase matcher on exactly this shape --
        # the verb is never adjacent to the binary.
        env = _deny("p4.exe -p ssl:p4.example.com:1666 -u agent -c agent-ws submit -c 41")
        assert "no submit provider installed" in env["permissionDecisionReason"]


class TestUnparseable:
    def test_dash_x_denied(self):
        _deny("p4 -x argfile.txt status")

    def test_p4aliases_denied(self):
        _deny("P4ALIASES=/tmp/aliases.txt p4 status")

    def test_p4vc_denied(self):
        _deny("p4vc submit")

    def test_git_p4_denied(self):
        _deny("git p4 sync")


class TestReadOnlyStrip:
    def test_attrib_minus_r_denied(self):
        _deny("attrib -r Config/DefaultEngine.ini")

    def test_chmod_plus_w_denied(self):
        _deny("chmod +w Config/DefaultEngine.ini")

    def test_chmod_without_plus_w_allowed(self):
        _allow("chmod 644 Config/DefaultEngine.ini")


class TestReconcileScoping:
    def test_bare_reconcile_dash_n_denied(self):
        env = _deny("p4 reconcile -n")
        assert "scope it to paths" in env["permissionDecisionReason"]

    def test_path_scoped_reconcile_dash_n_allowed(self):
        _allow("p4 reconcile -n -- Content/Maps/Foo.umap")

    def test_unscoped_reconcile_without_n_denied(self):
        _deny("p4 reconcile")


class TestSessionClWrites:
    def test_edit_with_cl_allowed(self):
        _allow("p4 edit -c 41 -- Content/Foo.uasset")

    def test_edit_without_cl_denied(self):
        _deny("p4 edit -- Content/Foo.uasset")

    def test_revert_dash_a_allowed(self):
        _allow("p4 revert -a -c 41 -- Content/Foo.uasset")

    def test_shelve_with_cl_allowed(self):
        _allow("p4 shelve -c 41")


class TestReadVerbsAllowed:
    def test_status_allowed(self):
        _allow("p4 status")

    def test_stream_o_allowed(self):
        _allow("p4 stream -o")

    def test_stream_without_o_denied(self):
        _deny("p4 stream -d //depot/UE5/main")

    def test_set_no_args_allowed(self):
        _allow("p4 set")

    def test_login_dash_s_allowed(self):
        _allow("p4 login -s")


class TestGitWorktreeRewriteDeny:
    def test_checkout_paths_denied(self):
        _deny("git checkout -- Content/Foo.uasset")

    def test_checkout_branch_denied(self):
        _deny("git checkout main")

    def test_clean_fdx_denied(self):
        env = _deny("git clean -fdx")
        assert "git clean" in env["permissionDecisionReason"]

    def test_reset_hard_denied(self):
        _deny("git reset --hard HEAD~1")

    def test_reset_mixed_allowed(self):
        _allow("git reset --mixed HEAD~1")

    def test_reset_soft_allowed(self):
        _allow("git reset --soft HEAD~1")

    def test_reset_no_flag_allowed(self):
        _allow("git reset HEAD~1")

    def test_stash_pop_denied(self):
        _deny("git stash pop")

    def test_submodule_update_denied(self):
        _deny("git submodule update --init")

    def test_submodule_status_allowed(self):
        _allow("git submodule status")

    def test_sparse_checkout_denied(self):
        _deny("git sparse-checkout set Content/")

    def test_read_tree_dash_u_denied(self):
        _deny("git read-tree -u HEAD")

    def test_checkout_index_denied(self):
        _deny("git checkout-index -a")

    def test_bisect_denied(self):
        _deny("git bisect start")

    def test_status_allowed_through_the_git_leg(self):
        _allow("git status")


class TestChainedSegments:
    def test_p4_ok_segment_then_git_deny_segment_denies(self):
        _deny("p4 status && git checkout -- Content/Foo.uasset")

    def test_powershell_ampersand_p4_spelling(self):
        env = _deny("& p4 submit -d 'x'")
        assert "no submit provider installed" in env["permissionDecisionReason"]

    def test_cmd_c_p4_spelling(self):
        env = _deny('cmd /c p4 submit -d "x"', tool_name="Bash")
        assert "no submit provider installed" in env["permissionDecisionReason"]


class TestPowerShellDialect:
    def test_submit_denied_under_powershell_tool_name(self):
        env = _deny("p4 submit -d 'x'", tool_name="PowerShell")
        assert "no submit provider installed" in env["permissionDecisionReason"]

    def test_status_allowed_under_powershell(self):
        _allow("p4 status", tool_name="PowerShell")

    def test_git_checkout_denied_under_powershell(self):
        _deny("git checkout -- Content/Foo.uasset", tool_name="PowerShell")


class TestGitOnlyRepoPaysNothing:
    def test_no_marker_allows_p4_submit_untouched(self, monkeypatch):
        monkeypatch.setattr(p4_verb_fence, "_is_p4_gated", lambda cwd: False)
        _allow("p4 submit -d 'commit'")

    def test_no_marker_allows_git_clean_fdx(self, monkeypatch):
        monkeypatch.setattr(p4_verb_fence, "_is_p4_gated", lambda cwd: False)
        _allow("git clean -fdx")

    def test_marker_gate_is_a_real_filesystem_walk(self, tmp_path):
        # Exercises `_find_repo_root_no_spawn` + `is_p4_repo` for real,
        # rather than through the autouse monkeypatch (see
        # `_REAL_IS_P4_GATED` above).
        result = _REAL_IS_P4_GATED(str(tmp_path))
        assert result is False

    def test_marker_present_gates_true(self, tmp_path):
        (tmp_path / "coordinator.local.md").write_text(
            "---\nvcs_mirror: p4\n---\n", encoding="utf-8"
        )
        nested = tmp_path / "sub" / "dir"
        nested.mkdir(parents=True)
        assert _REAL_IS_P4_GATED(str(nested)) is True


class TestNonCommandToolNamesIgnored:
    def test_non_matching_tool_name_returns_none(self):
        result = p4_verb_fence.check(
            {"tool_name": "Read", "tool_input": {"file_path": "x"}, "cwd": "/repo"}
        )
        assert result is None

    def test_empty_command_returns_none(self):
        _allow("")
