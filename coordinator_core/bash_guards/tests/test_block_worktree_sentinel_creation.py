
from __future__ import annotations

import json

from coordinator_core.bash_guards import block_worktree_sentinel_creation as guard
from coordinator_core.bash_guards import block_approval_sentinel_creation as doctrine_guard
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._alternative_liveness import _BACKTICK_RE


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
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_no_sentinel_mention_allows(self):
        assert guard.check(_payload("git status && ls -la")) is None


class TestRedirectionDenies:
    def test_bare_gt_denies(self):
        out = guard.check(_payload("echo ok > %s" % SENTINEL))
        _reason(out)

    def test_attached_gt_denies(self):
        _reason(guard.check(_payload("echo ok >%s" % SENTINEL)))

    def test_append_denies(self):
        _reason(guard.check(_payload("echo ok >> %s" % SENTINEL)))

    def test_fd_prefixed_redirect_denies(self):
        _reason(guard.check(_payload("some-cmd 2> %s" % SENTINEL)))

    def test_redirect_into_subdir_denies(self):
        _reason(guard.check(_payload("echo ok > /some/dir/%s" % SENTINEL)))


class TestFileArgCommandsDeny:
    def test_touch_denies(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        _reason(out)

    def test_touch_with_path_prefix_denies(self):
        _reason(guard.check(_payload("touch /repo/%s" % SENTINEL)))

    def test_cp_dest_denies(self):
        _reason(guard.check(_payload("cp somefile %s" % SENTINEL)))

    def test_cp_source_denies(self):
        _reason(guard.check(_payload("cp %s /tmp/copy" % SENTINEL)))

    def test_mv_denies(self):
        _reason(guard.check(_payload("mv somefile %s" % SENTINEL)))

    def test_install_denies(self):
        _reason(guard.check(_payload("install -m 644 src %s" % SENTINEL)))

    def test_ln_denies(self):
        _reason(guard.check(_payload("ln -s /tmp/x %s" % SENTINEL)))

    def test_tee_denies(self):
        _reason(guard.check(_payload("echo ok | tee %s" % SENTINEL)))

    def test_tee_append_denies(self):
        _reason(guard.check(_payload("echo ok | tee -a %s" % SENTINEL)))


class TestSedInplaceDenies:
    def test_bare_dash_i_denies(self):
        _reason(guard.check(_payload("sed -i 's/a/b/' %s" % SENTINEL)))

    def test_dash_i_with_suffix_denies(self):
        _reason(guard.check(_payload("sed -i.bak 's/a/b/' %s" % SENTINEL)))

    def test_long_form_in_place_denies(self):
        _reason(guard.check(_payload("sed --in-place 's/a/b/' %s" % SENTINEL)))

    def test_sed_without_inplace_flag_allows(self):
        assert guard.check(_payload("sed 's/a/b/' %s" % SENTINEL)) is None


class TestPythonDashCDenies:
    def test_open_write_mode_denies(self):
        code = "open('%s', 'w').close()" % SENTINEL
        out = guard.check(_payload("python3 -c \"%s\"" % code))
        _reason(out)

    def test_python_no_version_suffix_denies(self):
        code = "open('%s', 'w')" % SENTINEL
        _reason(guard.check(_payload("python -c \"%s\"" % code)))

    def test_attached_dash_c_denies(self):
        code = "open('%s', 'w')" % SENTINEL
        _reason(guard.check(_payload('python3 -c"%s"' % code)))

    def test_versioned_python_denies(self):
        code = "open('%s', 'w')" % SENTINEL
        _reason(guard.check(_payload("python3.11 -c \"%s\"" % code)))

    def test_unrelated_python_c_allows(self):
        assert guard.check(_payload("python3 -c \"print('hello world')\"")) is None


class TestQuotingShapes:
    def test_single_quoted_denies(self):
        _reason(guard.check(_payload("touch '%s'" % SENTINEL)))

    def test_double_quoted_denies(self):
        _reason(guard.check(_payload('touch "%s"' % SENTINEL)))

    def test_adjacent_quote_concatenation_denies(self):
        cmd = "touch '.coordinator-override-worktree'\"-guard\""
        _reason(guard.check(_payload(cmd)))


class TestChainedAndEnvPrefixedShapes:
    def test_semicolon_chained_denies(self):
        _reason(guard.check(_payload("cd /tmp; touch %s" % SENTINEL)))

    def test_and_chained_denies(self):
        _reason(guard.check(_payload("cd /tmp && touch %s" % SENTINEL)))

    def test_piped_denies(self):
        _reason(guard.check(_payload("echo ok | tee %s" % SENTINEL)))

    def test_leading_env_assignment_still_denies(self):
        _reason(guard.check(_payload("FOO=bar touch %s" % SENTINEL)))

    def test_second_command_in_chain_denies(self):
        _reason(guard.check(_payload("ls -la && touch %s" % SENTINEL)))

    def test_git_dash_c_prefixed_denies(self):
        _reason(guard.check(_payload("git -C /tmp status; touch %s" % SENTINEL)))


class TestAllowReadsAndRemoval:
    def test_rm_allows(self):
        assert guard.check(_payload("rm %s" % SENTINEL)) is None

    def test_cat_allows(self):
        assert guard.check(_payload("cat %s" % SENTINEL)) is None

    def test_ls_allows(self):
        assert guard.check(_payload("ls -la %s" % SENTINEL)) is None

    def test_stat_allows(self):
        assert guard.check(_payload("stat %s" % SENTINEL)) is None

    def test_test_dash_f_allows(self):
        assert guard.check(_payload("test -f %s" % SENTINEL)) is None

    def test_test_and_rm_allows(self):
        assert guard.check(_payload("test -f %s && rm %s" % (SENTINEL, SENTINEL))) is None

    def test_unrelated_touch_allows(self):
        assert guard.check(_payload("touch somefile.txt")) is None

    def test_near_miss_filename_allows(self):
        assert guard.check(_payload("touch %s-typo" % SENTINEL)) is None


class TestDoesNotCatchTheDoctrineSentinel:
    """This guard protects a DIFFERENT sentinel than
    block_approval_sentinel_creation.py -- confirm neither guard catches
    the other's target basename."""

    def test_worktree_guard_does_not_catch_doctrine_sentinel(self):
        assert guard.check(_payload("touch %s" % DOCTRINE_SENTINEL)) is None

    def test_doctrine_guard_does_not_catch_worktree_sentinel(self):
        assert doctrine_guard.check(_payload("touch %s" % SENTINEL)) is None


class TestNotIdentityGated:
    def test_denies_without_any_identity_fields(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        _reason(out)

    def test_denies_with_subagent_identity(self):
        out = guard.check(
            _payload(
                "touch %s" % SENTINEL,
                agent_id="a0123456789abcdef",
                agent_type="coordinator:executor",
            )
        )
        _reason(out)

    def test_denies_with_em_shaped_payload_no_identity(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        _reason(out)


class TestNoOverride:
    def test_env_var_shaped_override_does_not_allow(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_WORKTREE_GUARD", "1")
        out = guard.check(_payload("touch %s" % SENTINEL))
        _reason(out)


class TestDenyMessageDiscipline:
    def test_deny_reason_never_names_the_sentinel(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        reason = _reason(out)
        assert SENTINEL not in reason

    def test_deny_reason_is_offer_shaped(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        reason = _reason(out)
        assert "scoped" in reason.lower()
        assert "EM" in reason
        assert "PM" in reason


class TestReasonClassSpecificMessages:

    def test_direct_deny_message_unchanged(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        reason = _reason(out)
        assert "this command would create or modify a" in reason
        assert "worktree-ban override" in reason
        assert SENTINEL not in reason

    def test_indirection_deny_does_not_assert_creation(self):
        out = guard.check(_payload("bash bin/install-git-hooks.sh"))
        reason = _reason(out)
        assert "this command would create or modify a" not in reason
        assert "cannot examine" in reason
        assert "NOT because the payload was found" in reason
        assert SENTINEL not in reason

    def test_indirection_deny_surfaces_the_shape(self):
        out = guard.check(_payload("bash bin/install-git-hooks.sh"))
        reason = _reason(out)
        assert "interpreter-invoked script" in reason
        assert "indirection wrapper" in reason

    def test_indirection_deny_offers_a_path_forward(self):
        # `_sentinel_creation_guard.INDIRECTION_REMEDY` constant, which
        out = guard.check(_payload("bash bin/install-git-hooks.sh"))
        reason = _reason(out)
        assert "EM/PM" in reason
        recommended = next(
            c for c in _BACKTICK_RE.findall(reason) if c.startswith("./")
        )
        assert guard.check(_payload(recommended)) is None

    def test_recursive_indirection_deny_still_redacts_the_sentinel(self):
        out = guard.check(_payload('bash -c "touch %s"' % SENTINEL))
        reason = _reason(out)
        assert SENTINEL not in reason
        assert "cannot examine" in reason


class TestIndirectionWrapperShapesDeny:

    def test_bash_dash_c_denies(self):
        _reason(guard.check(_payload('bash -c "touch %s"' % SENTINEL)))

    def test_sh_dash_c_denies(self):
        _reason(guard.check(_payload('sh -c "touch %s"' % SENTINEL)))

    def test_zsh_dash_c_denies(self):
        _reason(guard.check(_payload('zsh -c "touch %s"' % SENTINEL)))

    def test_env_sh_dash_c_denies(self):
        _reason(guard.check(_payload('env sh -c "touch %s"' % SENTINEL)))

    def test_env_var_prefixed_cmd_denies(self):
        _reason(guard.check(_payload("env FOO=1 touch %s" % SENTINEL)))

    def test_bare_var_prefixed_indirection_denies(self):
        _reason(guard.check(_payload('FOO=1 sh -c "touch %s"' % SENTINEL)))

    def test_xargs_denies(self):
        _reason(guard.check(_payload("echo %s | xargs touch" % SENTINEL)))

    def test_dd_of_denies(self):
        _reason(guard.check(_payload("dd if=/dev/null of=%s" % SENTINEL)))

    def test_dd_of_with_conv_denies(self):
        _reason(guard.check(_payload("dd if=/dev/null of=%s conv=notrunc" % SENTINEL)))

    def test_heredoc_fed_bash_denies(self):
        cmd = "bash <<'EOF'\ntouch %s\nEOF" % SENTINEL
        _reason(guard.check(_payload(cmd)))

    def test_heredoc_fed_sh_denies(self):
        cmd = "sh <<'EOF'\ntouch %s\nEOF" % SENTINEL
        _reason(guard.check(_payload(cmd)))

    def test_bash_bare_file_denies(self):
        _reason(guard.check(_payload("bash /tmp/some-script.sh")))

    def test_python_dash_c_nested_in_sh_dash_c_denies(self):
        code = "open('%s', 'w').close()" % SENTINEL
        inner = 'python3 -c "%s"' % code
        cmd = "sh -c '%s'" % inner
        _reason(guard.check(_payload(cmd)))

    def test_unrelated_bash_dash_c_allows(self):
        assert guard.check(_payload('bash -c "echo hello"')) is None

    def test_xargs_read_only_head_allows(self):
        assert guard.check(_payload("echo hello | xargs cat")) is None
        assert guard.check(_payload("git diff --name-only a b | xargs wc -l")) is None

    def test_xargs_writing_or_wrapper_head_still_denied(self):
        _reason(guard.check(_payload("echo hello | xargs cp x")))
        _reason(guard.check(_payload("ls | xargs sh -c 'echo'")))
        _reason(guard.check(_payload("ls | xargs --unknown-flag cat")))

    def test_python_dash_m_allows(self):
        assert guard.check(_payload("python3 -m pytest")) is None


class TestReachableThroughTheDispatchChain:

    @staticmethod
    def _decision(command):
        out = dispatch.evaluate_payload_json(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        )
        return "deny" if (out and '"deny"' in json.dumps(out)) else "allow"

    def test_bare_touch_denied_end_to_end(self):
        assert self._decision("touch %s" % SENTINEL) == "deny"

    def test_cd_prefixed_touch_denied_end_to_end(self):
        assert self._decision("cd /tmp && touch %s" % SENTINEL) == "deny"

    def test_semicolon_chained_touch_denied_end_to_end(self):
        assert self._decision("cd /tmp; touch %s" % SENTINEL) == "deny"

    def test_redirect_denied_end_to_end(self):
        assert self._decision("echo ok > %s" % SENTINEL) == "deny"

    def test_python_dash_c_denied_end_to_end(self):
        code = "open('%s', 'w')" % SENTINEL
        assert self._decision("python3 -c \"%s\"" % code) == "deny"

    def test_git_dash_c_prefixed_touch_denied_end_to_end(self):
        assert self._decision("git -C /tmp status; touch %s" % SENTINEL) == "deny"

    def test_rm_allowed_end_to_end(self):
        assert self._decision("rm %s" % SENTINEL) == "allow"

    def test_cat_allowed_end_to_end(self):
        assert self._decision("cat %s" % SENTINEL) == "allow"

    def test_unrelated_touch_allowed_end_to_end(self):
        assert self._decision("touch somefile.txt") == "allow"

    def test_bash_dash_c_denied_end_to_end(self):
        assert self._decision('bash -c "touch %s"' % SENTINEL) == "deny"

    def test_env_sh_dash_c_denied_end_to_end(self):
        assert self._decision('env sh -c "touch %s"' % SENTINEL) == "deny"

    def test_dd_of_denied_end_to_end(self):
        assert self._decision("dd if=/dev/null of=%s" % SENTINEL) == "deny"

    def test_heredoc_fed_bash_denied_end_to_end(self):
        cmd = "bash <<'EOF'\ntouch %s\nEOF" % SENTINEL
        assert self._decision(cmd) == "deny"

    def test_registered_ahead_of_offer_git_c(self):
        assert self._decision("cd /tmp && touch %s" % SENTINEL) == "deny"


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

    def test_new_item_cmdlet_denies(self):
        out = guard.check(self._ps_payload("New-Item %s" % SENTINEL))
        _reason(out)

    def test_redirect_denies(self):
        out = guard.check(self._ps_payload("echo ok > %s" % SENTINEL))
        _reason(out)

    def test_unrelated_command_allows_with_no_silence(self):
        from coordinator_core.bash_guards import _verdict

        with _verdict.collecting() as silences:
            out = guard.check(self._ps_payload("Get-ChildItem"))
        assert out is None
        assert silences == []

    def test_grammar_gap_shape_records_silent_not_clean(self):
        from coordinator_core.bash_guards import _verdict

        with _verdict.collecting() as silences:
            out = guard.check(self._ps_payload("Remove-Item x &> out.txt"))
        assert out is None
        assert any(s.guard_name == "block_worktree_sentinel_creation" for s in silences)

    def test_non_bash_non_powershell_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None
