"""Tests for coordinator_core.bash_guards.block_fleet_delegation_creation.

Covers the DENY set (redirection, `touch`/`cp`/`mv`/`install`/`ln`/`tee`,
`sed -i`, `python -c`, `dd of=`), the ALLOW set (reads, `rm`, the narrow
read-only `git` subcommand set), variable taint (direct and transitive),
one indirection-wrapper shape, and the guard's declared interface
(`CLASS`/`MATCHERS`/`PRIORITY`).

Pure Python -- no shell spawns, no filesystem writes.

Spec backlink: coordinator_core/bash_guards/block_fleet_delegation_creation.py
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_fleet_delegation_creation as guard


TARGET = "fleet-delegation.json"


def _payload(command):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }


def _reason(out):
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


class TestInterface:
    def test_class_is_hard_deny(self):
        assert guard.CLASS == "hard-deny"

    def test_matchers_is_command_tool_names(self):
        from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

        assert guard.MATCHERS is COMMAND_TOOL_NAMES

    def test_priority_declared(self):
        assert isinstance(guard.PRIORITY, int)


class TestNonBashOrEmpty:
    def test_non_command_tool_allows(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_no_mention_allows(self):
        assert guard.check(_payload("git status && ls -la")) is None


class TestDenyRedirection:
    def test_bare_gt_redirect_denies(self):
        _reason(guard.check(_payload("echo ok > %s" % TARGET)))

    def test_attached_gt_redirect_denies(self):
        _reason(guard.check(_payload("echo ok >%s" % TARGET)))

    def test_append_redirect_denies(self):
        _reason(guard.check(_payload("echo ok >> %s" % TARGET)))

    def test_fd_prefixed_redirect_denies(self):
        _reason(guard.check(_payload("some-cmd 2> %s" % TARGET)))

    def test_redirect_with_path_prefix_denies(self):
        _reason(guard.check(_payload("echo ok > /some/dir/%s" % TARGET)))

    def test_redirect_into_settings_home_path_denies(self):
        _reason(
            guard.check(
                _payload("echo '{}' > $HOME/.coordinator-claude-settings/%s" % TARGET)
            )
        )


class TestDenyFileArgCommands:
    def test_touch_denies(self):
        _reason(guard.check(_payload("touch %s" % TARGET)))

    def test_cp_denies_as_source(self):
        _reason(guard.check(_payload("cp %s /tmp/x" % TARGET)))

    def test_cp_denies_as_destination(self):
        _reason(guard.check(_payload("cp /tmp/x %s" % TARGET)))

    def test_mv_denies(self):
        _reason(guard.check(_payload("mv /tmp/x %s" % TARGET)))

    def test_install_denies(self):
        _reason(guard.check(_payload("install /tmp/x %s" % TARGET)))

    def test_ln_denies(self):
        _reason(guard.check(_payload("ln -s /tmp/x %s" % TARGET)))

    def test_tee_denies(self):
        _reason(guard.check(_payload("echo ok | tee %s" % TARGET)))


class TestDenySedInplace:
    def test_sed_dash_i_denies(self):
        _reason(guard.check(_payload("sed -i 's/a/b/' %s" % TARGET)))

    def test_sed_long_inplace_denies(self):
        _reason(guard.check(_payload("sed --in-place 's/a/b/' %s" % TARGET)))


class TestDenyPythonDashC:
    def test_bare_dash_c_denies(self):
        cmd = "python3 -c \"open('%s', 'w').write('{}')\"" % TARGET
        _reason(guard.check(_payload(cmd)))

    def test_attached_dash_c_denies(self):
        cmd = "python3 -copen('%s', 'w')" % TARGET
        _reason(guard.check(_payload(cmd)))


class TestDenyDdOf:
    def test_dd_of_denies(self):
        _reason(guard.check(_payload("dd if=/dev/zero of=%s" % TARGET)))


class TestVariableTaint:
    def test_direct_taint_mkdir_shape_denies(self):
        cmd = "S=%s; touch $S" % TARGET
        _reason(guard.check(_payload(cmd)))

    def test_transitive_taint_denies(self):
        cmd = "A=%s; B=$A; C=$B; touch $C" % TARGET
        _reason(guard.check(_payload(cmd)))

    def test_tainted_redirect_denies(self):
        cmd = "S=%s; cat /etc/hostname > $S" % TARGET
        _reason(guard.check(_payload(cmd)))


class TestIndirection:
    def test_bash_dash_c_wrapped_touch_denies(self):
        cmd = "bash -c \"touch %s\"" % TARGET
        _reason(guard.check(_payload(cmd)))

    def test_xargs_denies_outright(self):
        cmd = "echo %s | xargs touch" % TARGET
        _reason(guard.check(_payload(cmd)))


class TestAllowReadsAndRemoval:
    def test_cat_allows(self):
        assert guard.check(_payload("cat %s" % TARGET)) is None

    def test_ls_allows(self):
        assert guard.check(_payload("ls %s" % TARGET)) is None

    def test_stat_allows(self):
        assert guard.check(_payload("stat %s" % TARGET)) is None

    def test_test_dash_f_allows(self):
        assert guard.check(_payload("test -f %s && echo yes" % TARGET)) is None

    def test_head_allows(self):
        assert guard.check(_payload("head %s" % TARGET)) is None

    def test_tail_allows(self):
        assert guard.check(_payload("tail %s" % TARGET)) is None

    def test_wc_allows(self):
        assert guard.check(_payload("wc -l %s" % TARGET)) is None

    def test_file_allows(self):
        assert guard.check(_payload("file %s" % TARGET)) is None

    def test_grep_allows(self):
        assert guard.check(_payload("grep expires_at %s" % TARGET)) is None

    def test_rm_allows(self):
        assert guard.check(_payload("rm %s" % TARGET)) is None

    def test_rm_f_allows(self):
        assert guard.check(_payload("rm -f %s" % TARGET)) is None


class TestAllowSafeGitSubcommands:
    @pytest.mark.parametrize(
        "sub",
        [
            "status", "diff", "log", "show", "ls-files", "rev-parse",
            "describe", "check-ignore", "check-attr",
        ],
    )
    def test_safe_git_subcommand_allows(self, sub):
        assert guard.check(_payload("git %s %s" % (sub, TARGET))) is None


class TestDenyUnsafeGitSubcommands:
    def test_git_checkout_denies(self):
        _reason(guard.check(_payload("git checkout HEAD -- %s" % TARGET)))

    def test_git_stash_pop_denies_when_mentioning_target(self):
        # `git stash pop` can restore a file at the grant-record path --
        # not on the safe-subcommand allowlist, so it falls through to the
        # mention-based deny like any other unrecognized git subcommand.
        _reason(guard.check(_payload("git stash pop -- %s" % TARGET)))
