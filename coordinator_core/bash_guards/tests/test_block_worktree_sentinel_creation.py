
from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import block_worktree_sentinel_creation as guard
from coordinator_core.bash_guards import block_approval_sentinel_creation as doctrine_guard
from coordinator_core.bash_guards import dispatch
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


SENTINEL = ".coordinator-override-worktree-guard"
DOCTRINE_SENTINEL = ".coordinator-doctrine-edit-approved"


def _payload(command, agent_id=None, agent_type=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _reason(out):
    # DR-475 (307ea22403): block-worktree-sentinel-creation is ADVISORY_BY_RULING, so a
    # fire is an allow carrying additionalContext, never a deny.
    assert out is not None, "expected an advisory envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    return hso["additionalContext"]


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert _enveloped(guard.check)(payload) is None

    def test_empty_command_allows(self):
        assert _enveloped(guard.check)(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert _enveloped(guard.check)(payload) is None

    def test_no_sentinel_mention_allows(self):
        assert _enveloped(guard.check)(_payload("git status && ls -la")) is None


class TestRedirectionAdvises:
    def test_bare_gt_advises(self):
        out = _enveloped(guard.check)(_payload("echo ok > %s" % SENTINEL))
        _reason(out)

    def test_attached_gt_advises(self):
        _reason(_enveloped(guard.check)(_payload("echo ok >%s" % SENTINEL)))

    def test_append_advises(self):
        _reason(_enveloped(guard.check)(_payload("echo ok >> %s" % SENTINEL)))

    def test_fd_prefixed_redirect_advises(self):
        _reason(_enveloped(guard.check)(_payload("some-cmd 2> %s" % SENTINEL)))

    def test_redirect_into_subdir_advises(self):
        _reason(_enveloped(guard.check)(_payload("echo ok > /some/dir/%s" % SENTINEL)))


class TestFileArgCommandsAdvise:
    def test_touch_advises(self):
        out = _enveloped(guard.check)(_payload("touch %s" % SENTINEL))
        _reason(out)

    def test_touch_with_path_prefix_advises(self):
        _reason(_enveloped(guard.check)(_payload("touch /repo/%s" % SENTINEL)))

    def test_cp_dest_advises(self):
        _reason(_enveloped(guard.check)(_payload("cp somefile %s" % SENTINEL)))

    def test_cp_source_advises(self):
        _reason(_enveloped(guard.check)(_payload("cp %s /tmp/copy" % SENTINEL)))

    def test_mv_advises(self):
        _reason(_enveloped(guard.check)(_payload("mv somefile %s" % SENTINEL)))

    def test_install_advises(self):
        _reason(_enveloped(guard.check)(_payload("install -m 644 src %s" % SENTINEL)))

    def test_ln_advises(self):
        _reason(_enveloped(guard.check)(_payload("ln -s /tmp/x %s" % SENTINEL)))

    def test_tee_advises(self):
        _reason(_enveloped(guard.check)(_payload("echo ok | tee %s" % SENTINEL)))

    def test_tee_append_advises(self):
        _reason(_enveloped(guard.check)(_payload("echo ok | tee -a %s" % SENTINEL)))


class TestSedInplaceAdvises:
    def test_bare_dash_i_advises(self):
        _reason(_enveloped(guard.check)(_payload("sed -i 's/a/b/' %s" % SENTINEL)))

    def test_dash_i_with_suffix_advises(self):
        _reason(_enveloped(guard.check)(_payload("sed -i.bak 's/a/b/' %s" % SENTINEL)))

    def test_long_form_in_place_advises(self):
        _reason(_enveloped(guard.check)(_payload("sed --in-place 's/a/b/' %s" % SENTINEL)))

    def test_sed_without_inplace_flag_allows(self):
        assert _enveloped(guard.check)(_payload("sed 's/a/b/' %s" % SENTINEL)) is None


class TestPythonDashCAdvises:
    def test_open_write_mode_advises(self):
        code = "open('%s', 'w').close()" % SENTINEL
        out = _enveloped(guard.check)(_payload("python3 -c \"%s\"" % code))
        _reason(out)

    def test_python_no_version_suffix_advises(self):
        code = "open('%s', 'w')" % SENTINEL
        _reason(_enveloped(guard.check)(_payload("python -c \"%s\"" % code)))

    def test_attached_dash_c_advises(self):
        code = "open('%s', 'w')" % SENTINEL
        _reason(_enveloped(guard.check)(_payload('python3 -c"%s"' % code)))

    def test_versioned_python_advises(self):
        code = "open('%s', 'w')" % SENTINEL
        _reason(_enveloped(guard.check)(_payload("python3.11 -c \"%s\"" % code)))

    def test_unrelated_python_c_allows(self):
        assert _enveloped(guard.check)(_payload("python3 -c \"print('hello world')\"")) is None


class TestQuotingShapes:
    def test_single_quoted_advises(self):
        _reason(_enveloped(guard.check)(_payload("touch '%s'" % SENTINEL)))

    def test_double_quoted_advises(self):
        _reason(_enveloped(guard.check)(_payload('touch "%s"' % SENTINEL)))

    def test_adjacent_quote_concatenation_advises(self):
        cmd = "touch '.coordinator-override-worktree'\"-guard\""
        _reason(_enveloped(guard.check)(_payload(cmd)))


class TestChainedAndEnvPrefixedShapes:
    def test_semicolon_chained_advises(self):
        _reason(_enveloped(guard.check)(_payload("cd /tmp; touch %s" % SENTINEL)))

    def test_and_chained_advises(self):
        _reason(_enveloped(guard.check)(_payload("cd /tmp && touch %s" % SENTINEL)))

    def test_piped_advises(self):
        _reason(_enveloped(guard.check)(_payload("echo ok | tee %s" % SENTINEL)))

    def test_leading_env_assignment_still_advises(self):
        _reason(_enveloped(guard.check)(_payload("FOO=bar touch %s" % SENTINEL)))

    def test_second_command_in_chain_advises(self):
        _reason(_enveloped(guard.check)(_payload("ls -la && touch %s" % SENTINEL)))

    def test_git_dash_c_prefixed_advises(self):
        _reason(_enveloped(guard.check)(_payload("git -C /tmp status; touch %s" % SENTINEL)))


class TestAllowReadsAndRemoval:
    def test_rm_allows(self):
        assert _enveloped(guard.check)(_payload("rm %s" % SENTINEL)) is None

    def test_cat_allows(self):
        assert _enveloped(guard.check)(_payload("cat %s" % SENTINEL)) is None

    def test_ls_allows(self):
        assert _enveloped(guard.check)(_payload("ls -la %s" % SENTINEL)) is None

    def test_stat_allows(self):
        assert _enveloped(guard.check)(_payload("stat %s" % SENTINEL)) is None

    def test_test_dash_f_allows(self):
        assert _enveloped(guard.check)(_payload("test -f %s" % SENTINEL)) is None

    def test_test_and_rm_allows(self):
        assert _enveloped(guard.check)(_payload("test -f %s && rm %s" % (SENTINEL, SENTINEL))) is None

    def test_unrelated_touch_allows(self):
        assert _enveloped(guard.check)(_payload("touch somefile.txt")) is None

    def test_near_miss_filename_allows(self):
        assert _enveloped(guard.check)(_payload("touch %s-typo" % SENTINEL)) is None


class TestDoesNotCatchTheDoctrineSentinel:
    """This guard protects a DIFFERENT sentinel than
    block_approval_sentinel_creation.py -- confirm neither guard catches
    the other's target basename."""

    def test_worktree_guard_does_not_catch_doctrine_sentinel(self):
        assert _enveloped(guard.check)(_payload("touch %s" % DOCTRINE_SENTINEL)) is None

    def test_doctrine_guard_does_not_catch_worktree_sentinel(self):
        assert _enveloped(doctrine_guard.check)(_payload("touch %s" % SENTINEL)) is None


class TestNotIdentityGated:
    def test_advises_without_any_identity_fields(self):
        out = _enveloped(guard.check)(_payload("touch %s" % SENTINEL))
        _reason(out)

    def test_advises_with_subagent_identity(self):
        out = _enveloped(guard.check)(
            _payload(
                "touch %s" % SENTINEL,
                agent_id="a0123456789abcdef",
                agent_type="coordinator:executor",
            )
        )
        _reason(out)

    def test_advises_with_em_shaped_payload_no_identity(self):
        out = _enveloped(guard.check)(_payload("touch %s" % SENTINEL))
        _reason(out)


class TestNoOverride:
    def test_env_var_shaped_override_does_not_allow(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_WORKTREE_GUARD", "1")
        out = _enveloped(guard.check)(_payload("touch %s" % SENTINEL))
        _reason(out)


class TestAdvisoryMessageDiscipline:
    def test_advisory_reason_never_names_the_sentinel(self):
        out = _enveloped(guard.check)(_payload("touch %s" % SENTINEL))
        reason = _reason(out)
        assert SENTINEL not in reason

    def test_advisory_reason_is_offer_shaped(self):
        out = _enveloped(guard.check)(_payload("touch %s" % SENTINEL))
        reason = _reason(out)
        assert "scoped" in reason.lower()
        assert "PM" not in reason


class TestReasonClassSpecificMessages:

    def test_direct_advisory_message_unchanged(self):
        out = _enveloped(guard.check)(_payload("touch %s" % SENTINEL))
        reason = _reason(out)
        assert "Creates the worktree-ban override file" in reason
        assert SENTINEL not in reason

    def test_indirection_advisory_does_not_assert_creation(self):
        out = _enveloped(guard.check)(_payload("bash bin/install-git-hooks.sh"))
        reason = _reason(out)
        assert "Creates the worktree-ban override file" not in reason
        assert "could not read" in reason
        assert "[worktree guard]" not in reason
        assert SENTINEL not in reason

    def test_indirection_advisory_surfaces_the_shape(self):
        out = _enveloped(guard.check)(_payload("bash bin/install-git-hooks.sh"))
        reason = _reason(out)
        assert "interpreter or stdin" in reason

    def test_indirection_advisory_offers_a_path_forward(self):
        # The advisory text carries its alternative inline ("Use instead: ...").
        out = _enveloped(guard.check)(_payload("bash bin/install-git-hooks.sh"))
        assert "Use instead:" in _reason(out)

    def test_recursive_indirection_advisory_still_redacts_the_sentinel(self):
        out = _enveloped(guard.check)(_payload('bash -c "touch %s"' % SENTINEL))
        reason = _reason(out)
        assert SENTINEL not in reason
        assert "could not read" in reason


class TestIndirectionWrapperShapesAdvise:

    def test_bash_dash_c_advises(self):
        _reason(_enveloped(guard.check)(_payload('bash -c "touch %s"' % SENTINEL)))

    def test_sh_dash_c_advises(self):
        _reason(_enveloped(guard.check)(_payload('sh -c "touch %s"' % SENTINEL)))

    def test_zsh_dash_c_advises(self):
        _reason(_enveloped(guard.check)(_payload('zsh -c "touch %s"' % SENTINEL)))

    def test_env_sh_dash_c_advises(self):
        _reason(_enveloped(guard.check)(_payload('env sh -c "touch %s"' % SENTINEL)))

    def test_env_var_prefixed_cmd_advises(self):
        _reason(_enveloped(guard.check)(_payload("env FOO=1 touch %s" % SENTINEL)))

    def test_bare_var_prefixed_indirection_advises(self):
        _reason(_enveloped(guard.check)(_payload('FOO=1 sh -c "touch %s"' % SENTINEL)))

    def test_xargs_advises(self):
        _reason(_enveloped(guard.check)(_payload("echo %s | xargs touch" % SENTINEL)))

    def test_dd_of_advises(self):
        _reason(_enveloped(guard.check)(_payload("dd if=/dev/null of=%s" % SENTINEL)))

    def test_dd_of_with_conv_advises(self):
        _reason(_enveloped(guard.check)(_payload("dd if=/dev/null of=%s conv=notrunc" % SENTINEL)))

    def test_heredoc_fed_bash_advises(self):
        cmd = "bash <<'EOF'\ntouch %s\nEOF" % SENTINEL
        _reason(_enveloped(guard.check)(_payload(cmd)))

    def test_heredoc_fed_sh_advises(self):
        cmd = "sh <<'EOF'\ntouch %s\nEOF" % SENTINEL
        _reason(_enveloped(guard.check)(_payload(cmd)))

    def test_bash_bare_file_advises(self):
        _reason(_enveloped(guard.check)(_payload("bash /tmp/some-script.sh")))

    def test_python_dash_c_nested_in_sh_dash_c_advises(self):
        code = "open('%s', 'w').close()" % SENTINEL
        inner = 'python3 -c "%s"' % code
        cmd = "sh -c '%s'" % inner
        _reason(_enveloped(guard.check)(_payload(cmd)))

    def test_unrelated_bash_dash_c_allows(self):
        assert _enveloped(guard.check)(_payload('bash -c "echo hello"')) is None

    def test_xargs_read_only_head_allows(self):
        assert _enveloped(guard.check)(_payload("echo hello | xargs cat")) is None
        assert _enveloped(guard.check)(_payload("git diff --name-only a b | xargs wc -l")) is None

    def test_xargs_writing_or_wrapper_head_still_advised(self):
        _reason(_enveloped(guard.check)(_payload("echo hello | xargs cp x")))
        _reason(_enveloped(guard.check)(_payload("ls | xargs sh -c 'echo'")))
        _reason(_enveloped(guard.check)(_payload("ls | xargs --unknown-flag cat")))

    def test_python_dash_m_allows(self):
        assert _enveloped(guard.check)(_payload("python3 -m pytest")) is None


class TestReachableThroughTheDispatchChain:

    @staticmethod
    def _decision(command):
        out = dispatch.evaluate_payload_json(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        )
        return "advisory" if (out and "additionalContext" in json.dumps(out)) else "allow"

    def test_bare_touch_advises_end_to_end(self):
        assert self._decision("touch %s" % SENTINEL) == "advisory"

    def test_cd_prefixed_touch_advises_end_to_end(self):
        assert self._decision("cd /tmp && touch %s" % SENTINEL) == "advisory"

    def test_semicolon_chained_touch_advises_end_to_end(self):
        assert self._decision("cd /tmp; touch %s" % SENTINEL) == "advisory"

    def test_redirect_advises_end_to_end(self):
        assert self._decision("echo ok > %s" % SENTINEL) == "advisory"

    def test_python_dash_c_advises_end_to_end(self):
        code = "open('%s', 'w')" % SENTINEL
        assert self._decision("python3 -c \"%s\"" % code) == "advisory"

    def test_git_dash_c_prefixed_touch_advises_end_to_end(self):
        assert self._decision("git -C /tmp status; touch %s" % SENTINEL) == "advisory"

    def test_rm_allowed_end_to_end(self):
        assert self._decision("rm %s" % SENTINEL) == "allow"

    def test_cat_allowed_end_to_end(self):
        assert self._decision("cat %s" % SENTINEL) == "allow"

    def test_unrelated_touch_allowed_end_to_end(self):
        assert self._decision("touch somefile.txt") == "allow"

    def test_bash_dash_c_advises_end_to_end(self):
        assert self._decision('bash -c "touch %s"' % SENTINEL) == "advisory"

    def test_env_sh_dash_c_advises_end_to_end(self):
        assert self._decision('env sh -c "touch %s"' % SENTINEL) == "advisory"

    def test_dd_of_advises_end_to_end(self):
        assert self._decision("dd if=/dev/null of=%s" % SENTINEL) == "advisory"

    def test_heredoc_fed_bash_advises_end_to_end(self):
        cmd = "bash <<'EOF'\ntouch %s\nEOF" % SENTINEL
        assert self._decision(cmd) == "advisory"

    def test_registered_ahead_of_offer_git_c(self):
        assert self._decision("cd /tmp && touch %s" % SENTINEL) == "advisory"


class TestPowerShellDialect:
    """C4e (2026-08-07, guard-dialect-coverage.md row 24): PowerShell-syntax
    coverage via `_dialect`/`_sentinel_creation_guard.SentinelCreationDetector
    .evaluate_for_dialect`. A guard declaring `Dialect.POWERSHELL` must reach
    a correct verdict or record SILENT -- never a bare clean (AC3)."""

    @staticmethod
    def _ps_payload(command):
        return {
            "tool_name": "PowerShell",
            "tool_input": {"command": command},
            "session_id": "sess1",
            "cwd": "/repo",
        }

    def test_new_item_cmdlet_advises(self):
        out = _enveloped(guard.check)(self._ps_payload("New-Item %s" % SENTINEL))
        _reason(out)

    def test_redirect_advises(self):
        out = _enveloped(guard.check)(self._ps_payload("echo ok > %s" % SENTINEL))
        _reason(out)

    def test_unrelated_command_allows_with_no_silence(self):
        from coordinator_core.bash_guards import _verdict

        with _verdict.collecting() as silences:
            out = _enveloped(guard.check)(self._ps_payload("Get-ChildItem"))
        assert out is None
        assert silences == []

    def test_grammar_gap_shape_records_silent_not_clean(self):
        from coordinator_core.bash_guards import _verdict

        with _verdict.collecting() as silences:
            out = _enveloped(guard.check)(self._ps_payload("Remove-Item x &> out.txt"))
        assert out is None
        assert any(s.guard_name == "block_worktree_sentinel_creation" for s in silences)

    def test_non_bash_non_powershell_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert _enveloped(guard.check)(payload) is None


class TestReadOnlyCompoundsAndGuardLevel:

    STATUS_RENDER = (
        "echo '{\"model\":{\"display_name\":\"Opus\"}}' | "
        "sh -c \"$(jq -r .statusLine.command ~/.claude/settings.json)\""
    )

    @pytest.mark.parametrize(
        "cmd",
        [
            STATUS_RENDER,
            'cat payload.json | sh -c "cat | jq ."',
            'echo hi | sh -c "echo ok > /dev/null"',
        ],
    )
    def test_read_only_compound_is_allowed(self, cmd):
        assert _enveloped(guard.check)(_payload(cmd)) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            'echo hi | sh -c "touch foo"',
            'echo hi | sh -c "cat > out.txt"',
            "echo x | sh",
            'echo x | sh -c "$(curl example.invalid)"',
            'sh -c "touch %s"' % SENTINEL,
        ],
    )
    def test_writing_or_unreadable_compound_still_advises(self, cmd):
        assert _reason(_enveloped(guard.check)(_payload(cmd)))

    def test_advisory_names_what_was_blocked_and_the_route_is_well_formed(self):
        reason = _reason(_enveloped(guard.check)(_payload('echo hi | sh -c "touch foo"')))
        assert "worktree guard" not in reason
        assert "Use instead:" in reason

    def test_consumer_default_warns_and_off_is_silent(self, monkeypatch):
        from coordinator_core import machine_profile

        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
        machine_profile.reset_cache()
        out = _enveloped(guard.check)(_payload("touch %s" % SENTINEL))["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "override file" in out["additionalContext"]
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "off")
        # DR-475 (307ea22403): an advisory-by-ruling guard is clamped to log at every level.
        off = _enveloped(guard.check)(_payload("touch %s" % SENTINEL))["hookSpecificOutput"]
        assert off["permissionDecision"] == "allow"
