
from __future__ import annotations

import json
import os

import pytest

from coordinator_core.bash_guards import block_approval_sentinel_creation as guard
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._alternative_liveness import _BACKTICK_RE


SENTINEL = ".coordinator-doctrine-edit-approved"


def _payload(command, agent_id=None, agent_type=None, cwd="/repo"):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": cwd,
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


class TestDenyRedirection:
    def test_bare_gt_redirect_denies(self):
        out = guard.check(_payload("echo ok > %s" % SENTINEL))
        _reason(out)

    def test_attached_gt_redirect_denies(self):
        _reason(guard.check(_payload("echo ok >%s" % SENTINEL)))

    def test_double_gt_append_redirect_denies(self):
        _reason(guard.check(_payload("echo ok >> %s" % SENTINEL)))

    def test_fd_prefixed_redirect_denies(self):
        _reason(guard.check(_payload("some-cmd 2> %s" % SENTINEL)))

    def test_redirect_with_path_prefix_denies(self):
        _reason(guard.check(_payload("echo ok > /some/dir/%s" % SENTINEL)))

    def test_heredoc_via_cat_redirect_denies(self):
        cmd = "cat > %s <<'EOF'\nhello\nEOF" % SENTINEL
        _reason(guard.check(_payload(cmd)))


class TestDenyFileArgCommands:
    def test_touch_denies(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        _reason(out)

    def test_touch_with_path_denies(self):
        _reason(guard.check(_payload("touch /repo/%s" % SENTINEL)))

    def test_cp_denies(self):
        _reason(guard.check(_payload("cp somefile %s" % SENTINEL)))

    def test_cp_source_position_denies(self):
        _reason(guard.check(_payload("cp %s /tmp/copy" % SENTINEL)))

    def test_mv_denies(self):
        _reason(guard.check(_payload("mv somefile %s" % SENTINEL)))

    def test_install_denies(self):
        _reason(guard.check(_payload("install -m 644 src %s" % SENTINEL)))

    def test_ln_denies(self):
        _reason(guard.check(_payload("ln -s /tmp/x %s" % SENTINEL)))

    def test_tee_denies(self):
        _reason(guard.check(_payload("echo ok | tee %s" % SENTINEL)))

    def test_tee_append_flag_denies(self):
        _reason(guard.check(_payload("echo ok | tee -a %s" % SENTINEL)))


class TestDenySedInPlace:
    def test_sed_dash_i_denies(self):
        _reason(guard.check(_payload("sed -i 's/a/b/' %s" % SENTINEL)))

    def test_sed_dash_i_attached_suffix_denies(self):
        _reason(guard.check(_payload("sed -i.bak 's/a/b/' %s" % SENTINEL)))

    def test_sed_long_form_in_place_denies(self):
        _reason(guard.check(_payload("sed --in-place 's/a/b/' %s" % SENTINEL)))

    def test_sed_without_inplace_flag_now_denies_under_default_deny_posture(self):
        _reason(guard.check(_payload("sed 's/a/b/' %s" % SENTINEL)))


class TestDenyPythonDashC:
    def test_python3_dash_c_open_write_denies(self):
        code = "open('%s', 'w').write('x')" % SENTINEL
        out = guard.check(_payload("python3 -c \"%s\"" % code))
        _reason(out)

    def test_python_dash_c_denies(self):
        code = "open('%s', 'w').close()" % SENTINEL
        _reason(guard.check(_payload("python -c \"%s\"" % code)))

    def test_python_attached_dash_c_denies(self):
        code = "open('%s','w')" % SENTINEL
        _reason(guard.check(_payload('python3 -c"%s"' % code)))

    def test_python_versioned_binary_denies(self):
        code = "open('%s', 'w')" % SENTINEL
        _reason(guard.check(_payload("python3.11 -c \"%s\"" % code)))

    def test_python_dash_c_unrelated_payload_allows(self):
        assert guard.check(_payload("python3 -c \"print('hello world')\"")) is None


class TestQuotedAndPartiallyQuotedSpellings:
    def test_fully_single_quoted_denies(self):
        _reason(guard.check(_payload("touch '%s'" % SENTINEL)))

    def test_fully_double_quoted_denies(self):
        _reason(guard.check(_payload('touch "%s"' % SENTINEL)))

    def test_adjacent_quote_concatenation_denies(self):
        cmd = "touch '.coordinator-doctrine-edit'\"-approved\""
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
        # guard must still fire (see module docstring "NOT IDENTITY-GATED").
        out = guard.check(_payload("touch %s" % SENTINEL))
        _reason(out)


class TestNoOverride:
    def test_env_var_shaped_override_does_not_allow(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_APPROVAL_SENTINEL", "1")
        out = guard.check(_payload("touch %s" % SENTINEL))
        _reason(out)


class TestDenyMessageDiscipline:

    def test_deny_reason_never_names_the_sentinel(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        reason = _reason(out)
        assert SENTINEL not in reason

    def test_deny_reason_does_not_echo_the_command(self):
        code = "open('%s', 'w').close()" % SENTINEL
        cmd = "python3 -c \"%s\"" % code
        out = guard.check(_payload(cmd))
        reason = _reason(out)
        assert "open(" not in reason
        assert cmd not in reason


class TestReasonClassSpecificMessages:

    def test_direct_deny_message_unchanged(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        reason = _reason(out)
        assert "creates/modifies the PM-approval sentinel" in reason
        assert "Ask the PM to create it" in reason
        assert SENTINEL not in reason

    def test_indirection_deny_does_not_assert_creation(self):
        out = guard.check(_payload("bash bin/install-git-hooks.sh"))
        reason = _reason(out)
        assert "creates/modifies the PM-approval sentinel" not in reason
        assert "cannot examine" in reason
        assert "NOT because the payload was found" in reason
        assert SENTINEL not in reason

    def test_indirection_deny_surfaces_the_shape(self):
        out = guard.check(_payload("bash bin/install-git-hooks.sh"))
        reason = _reason(out)
        assert "interpreter-invoked script" in reason
        assert "indirection wrapper" in reason

    def test_indirection_deny_names_the_guard_and_offers_a_path_forward(self):
        out = guard.check(_payload("bash bin/install-git-hooks.sh"))
        reason = _reason(out)
        assert "approval-sentinel guard" in reason
        assert "EM/PM" in reason
        recommended = next(
            c for c in _BACKTICK_RE.findall(reason) if c.startswith("./")
        )
        assert guard.check(_payload(recommended)) is None

    def test_the_direct_invocation_the_message_recommends_is_actually_allowed(self):
        assert guard.check(_payload("./bin/install-git-hooks.sh")) is None

    def test_recursive_indirection_deny_still_redacts_the_sentinel(self):
        out = guard.check(_payload('bash -c "touch %s"' % SENTINEL))
        reason = _reason(out)
        assert SENTINEL not in reason
        assert "creates/modifies the PM-approval sentinel" in reason


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

    def test_grep_into_xargs_grep_allows(self):
        cmd = 'grep -rln "def main" coordinator/bin | xargs grep -ln "doc-new\\|doc_new"'
        assert guard.check(_payload(cmd)) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "find . -name '*.py' | xargs -0 -n 1 wc -l",
            "ls | xargs -I {} head -1 {}",
            "ls | xargs -I{} stat {}",
            "ls | xargs --max-args=2 -- cat",
            "ls | env xargs grep x",
        ],
    )
    def test_xargs_read_only_head_with_options_allows(self, cmd):
        assert guard.check(_payload(cmd)) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo hello | xargs touch",
            "echo hello | xargs sh -c 'echo x'",
            "echo hello | xargs env cat",
            "echo hello | xargs xargs cat",
            "echo hello | xargs rm",
            "echo hello | xargs --unknown-flag cat",
            "echo hello | xargs -0r cat",
            "echo hello | xargs",
            "echo hello | xargs -n",
        ],
    )
    def test_xargs_other_heads_still_denied(self, cmd):
        _reason(guard.check(_payload(cmd)))

    def test_xargs_read_only_head_redirect_to_sentinel_denies(self):
        _reason(guard.check(_payload("echo hello | xargs cat > %s" % SENTINEL)))

    def test_xargs_read_only_head_inside_sh_dash_c_allows(self):
        assert guard.check(_payload("sh -c 'ls | xargs grep x'")) is None

    def test_xargs_echo_verb_allows(self):
        assert guard.check(_payload('echo "a b c" | xargs -n1 echo')) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo hello | xargs sh -c 'echo x'",
            "echo hello | xargs rm",
            "echo hello | xargs -I{} {} x",
            "echo hello | xargs env touch x",
            "echo hello | xargs",
        ],
    )
    def test_xargs_bypass_shapes_still_denied(self, cmd):
        _reason(guard.check(_payload(cmd)))

    def test_python_dash_m_allows(self):
        assert guard.check(_payload("python3 -m pytest")) is None


class TestIndirectionWrapperShapesDenyEndToEnd:
    @staticmethod
    def _decision(command):
        out = dispatch.evaluate_payload_json(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        )
        return "deny" if (out and '"deny"' in json.dumps(out)) else "allow"

    def test_bash_dash_c_denied_end_to_end(self):
        assert self._decision('bash -c "touch %s"' % SENTINEL) == "deny"

    def test_env_sh_dash_c_denied_end_to_end(self):
        assert self._decision('env sh -c "touch %s"' % SENTINEL) == "deny"

    def test_dd_of_denied_end_to_end(self):
        assert self._decision("dd if=/dev/null of=%s" % SENTINEL) == "deny"

    def test_heredoc_fed_bash_denied_end_to_end(self):
        cmd = "bash <<'EOF'\ntouch %s\nEOF" % SENTINEL
        assert self._decision(cmd) == "deny"


class TestFormerlyAllowedCreationShapesNowDeny:

    def test_mkdir_denies(self):
        _reason(guard.check(_payload("mkdir %s" % SENTINEL)))

    def test_mkdir_dash_p_denies(self):
        _reason(guard.check(_payload("mkdir -p %s" % SENTINEL)))

    def test_mkdir_dash_pv_dot_slash_prefixed_denies(self):
        _reason(guard.check(_payload("mkdir -pv ./%s" % SENTINEL)))

    def test_curl_dash_o_denies(self):
        _reason(guard.check(_payload("curl -o %s https://example.com/x" % SENTINEL)))

    def test_wget_dash_o_denies(self):
        _reason(guard.check(_payload("wget -O %s https://example.com/x" % SENTINEL)))

    def test_rsync_denies(self):
        _reason(guard.check(_payload("rsync /tmp/src %s" % SENTINEL)))

    def test_git_checkout_head_dash_dash_denies(self):
        _reason(guard.check(_payload("git checkout HEAD -- %s" % SENTINEL)))

    def test_unzip_dash_d_denies(self):
        _reason(guard.check(_payload("unzip archive.zip -d %s" % SENTINEL)))

    def test_mkdir_denied_end_to_end(self):
        out = dispatch.evaluate_payload_json(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": "mkdir %s" % SENTINEL}})
        )
        assert out and '"deny"' in json.dumps(out)


class TestSafeArgv0AllowlistStillWorks:

    def test_rm_of_sentinel_allows(self):
        assert guard.check(_payload("rm %s" % SENTINEL)) is None

    @pytest.mark.parametrize(
        "cmd_tmpl",
        [
            "cat %s",
            "ls -la %s",
            "stat %s",
            "test -f %s",
            "head %s",
            "tail %s",
            "wc -l %s",
            "file %s",
            "grep foo %s",
        ],
    )
    def test_read_only_inspection_commands_allow(self, cmd_tmpl):
        assert guard.check(_payload(cmd_tmpl % SENTINEL)) is None

    @pytest.mark.parametrize("sub", sorted(guard._ApprovalSentinelDetector._SAFE_GIT_SUBCOMMANDS))
    def test_read_only_git_subcommands_allow(self, sub):
        assert guard.check(_payload("git %s %s" % (sub, SENTINEL))) is None

    def test_check_ignore_on_the_sentinel_allows(self):
        """The command that VERIFIES the sentinel is gitignored must not be
        denied as a write. A tracked sentinel arrives with a fresh mtime on
        every checkout, which the read-side guard honours as a live approval
        nobody granted -- `git check-ignore` is how that hole is confirmed
        closed, so denying it is the guard refusing the check on its own
        boundary. Regression pin for the 2026-07-31 false positive."""
        assert guard.check(_payload("git check-ignore -q %s" % SENTINEL)) is None
        assert guard.check(_payload("git check-ignore -v %s" % SENTINEL)) is None

    def test_deny_message_enumerates_every_allowlisted_git_subcommand(self):
        """The deny message used to say "or a read-only `git` subcommand",
        which names a CATEGORY while the code enforces an allowlist -- a
        reader following the message hits a deny the message told them would
        not happen. Pin the message to the actual members so the two cannot
        drift apart again.

        Each member is named `git <subcommand>` (not a bare backtick word) --
        2026-08-03 fix: a bare `` `diff` `` etc. reads, to a liveness prober,
        as a standalone invocable executable named "diff", which does not
        resolve on PATH -- a false DEAD verdict for a real, working `git
        diff`. Pinning the `git `-prefixed form here keeps this test and
        that gate's own expectation from drifting apart again."""
        message = _reason(guard.check(_payload("touch %s" % SENTINEL)))
        for sub in guard._ApprovalSentinelDetector._SAFE_GIT_SUBCOMMANDS:
            assert "`git %s`" % sub in message, (
                "deny message does not name allowlisted git subcommand %r" % sub
            )

    def test_git_checkout_denies(self):
        _reason(guard.check(_payload("git checkout %s" % SENTINEL)))

    def test_git_restore_denies(self):
        _reason(guard.check(_payload("git restore %s" % SENTINEL)))

    def test_git_stash_pop_denies(self):
        _reason(guard.check(_payload("git stash pop %s" % SENTINEL)))

    def test_test_dash_f_and_rm_chain_still_allows(self):
        assert guard.check(_payload("test -f %s && rm %s" % (SENTINEL, SENTINEL))) is None

    def test_cat_of_unrelated_file_allows(self):
        assert guard.check(_payload("cat somefile.txt")) is None

    def test_redirect_into_sentinel_denies_even_from_safe_argv0(self):
        _reason(guard.check(_payload("cat /etc/hostname > %s" % SENTINEL)))


class TestIndirectionStillCaughtAfterInversion:

    def test_sh_dash_c_mkdir_denies(self):
        _reason(guard.check(_payload('sh -c "mkdir %s"' % SENTINEL)))

    def test_env_mkdir_denies(self):
        _reason(guard.check(_payload("env mkdir %s" % SENTINEL)))

    def test_xargs_mkdir_denies(self):
        _reason(guard.check(_payload("echo %s | xargs mkdir" % SENTINEL)))

    def test_sh_dash_c_mkdir_denied_end_to_end(self):
        out = dispatch.evaluate_payload_json(
            json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": 'sh -c "mkdir %s"' % SENTINEL}}
            )
        )
        assert out and '"deny"' in json.dumps(out)


class TestVariableTaintClosesRoundTwoForge:

    def test_assign_then_mkdir_denies(self):
        _reason(guard.check(_payload("S=%s; mkdir $S" % SENTINEL)))

    def test_assign_then_touch_denies(self):
        _reason(guard.check(_payload("S=%s; touch $S" % SENTINEL)))

    def test_assign_then_ln_denies(self):
        _reason(guard.check(_payload("L=%s; ln -s /tmp/x $L" % SENTINEL)))

    def test_assign_then_redirect_through_safe_head_denies(self):
        _reason(guard.check(_payload("S=%s; cat /etc/hostname > $S" % SENTINEL)))

    def test_brace_form_dereference_denies(self):
        _reason(guard.check(_payload("S=%s; mkdir ${S}" % SENTINEL)))

    def test_double_quoted_dereference_denies(self):
        _reason(guard.check(_payload('S=%s; mkdir "$S"' % SENTINEL)))

    def test_taint_through_export_denies(self):
        _reason(guard.check(_payload("export S=%s; mkdir $S" % SENTINEL)))

    def test_taint_survives_two_intervening_segments(self):
        _reason(
            guard.check(
                _payload(
                    "echo hi; export S=%s; echo mid; mkdir $S" % SENTINEL
                )
            )
        )

    def test_untainted_variable_dereference_still_allows(self):
        assert guard.check(_payload("FOO=bar; mkdir $FOO")) is None

    def test_assign_then_mkdir_denied_end_to_end(self):
        out = dispatch.evaluate_payload_json(
            json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "S=%s; mkdir $S" % SENTINEL},
                }
            )
        )
        assert out and '"deny"' in json.dumps(out)

    def test_known_open_gap_glob_near_miss_still_allows(self):
        # STRING CONSTRUCTION AND GLOB-SHAPED NEAR-MISSES" block: a purely
        assert guard.check(_payload("touch .coordinator-doctrine-edit-approv?d")) is None

    def test_previously_denied_forms_still_deny(self):
        for cmd in (
            "mkdir %s" % SENTINEL,
            "S=%s mkdir $S" % SENTINEL,
            "S=%s\nmkdir $S" % SENTINEL,
        ):
            _reason(guard.check(_payload(cmd)))

    def test_safe_set_allows_still_hold(self):
        assert guard.check(_payload("rm %s" % SENTINEL)) is None
        assert guard.check(_payload("cat %s" % SENTINEL)) is None
        assert guard.check(_payload("touch somefile.txt")) is None


class TestTransitiveTaintClosesRoundThreeForge:
    """2026-07-30 round-three forge-closure fix. Round two's taint only
    tainted a variable whose OWN value directly contained the sentinel
    basename -- a single hop. A variable assigned from ANOTHER
    already-tainted variable (`B=$A`) never itself contained the basename
    as a substring, so a chain of any length evaded round two entirely
    while resolving to the real sentinel at runtime. Covers the two
    confirmed-live chaining forms, a longer chain, and a chain crossing an
    unrelated intervening segment -- plus the string-CONSTRUCTION near-miss
    (basename never contiguous anywhere in the command text), which is a
    different class of gap and is pinned here as knowingly-ALLOWED per the
    module docstring's "KNOWN OPEN GAP" block, not chased into a fix."""

    def test_single_hop_chain_denies(self):
        _reason(
            guard.check(
                _payload("A=%s; B=$A; mkdir $B" % SENTINEL)
            )
        )

    def test_two_hop_chain_denies(self):
        _reason(
            guard.check(
                _payload("A=%s; B=$A; C=$B; mkdir $C" % SENTINEL)
            )
        )

    def test_longer_chain_denies(self):
        _reason(
            guard.check(
                _payload(
                    "A=%s; B=$A; C=$B; D=$C; E=$D; F=$E; mkdir $F" % SENTINEL
                )
            )
        )

    def test_chain_crossing_unrelated_intervening_segment_denies(self):
        _reason(
            guard.check(
                _payload(
                    "A=%s; B=$A; echo unrelated; ls -la; mkdir $B" % SENTINEL
                )
            )
        )

    def test_string_construction_near_miss_is_knowingly_allowed(self):
        # STRING CONSTRUCTION AND GLOB-SHAPED NEAR-MISSES" block, under
        # "VARIABLE-ASSEMBLED BASENAMES": neither `S` nor `S2` is ever
        cmd = 'S=".coordinator-doctrine-edit-"; S2="${S}approved"; mkdir $S2'
        assert guard.check(_payload(cmd)) is None

    def test_previously_denied_taint_forms_still_deny(self):
        for cmd in (
            "S=%s; mkdir $S" % SENTINEL,
            "S=%s; touch $S" % SENTINEL,
            "L=%s; ln -s /tmp/x $L" % SENTINEL,
            "S=%s; cat /etc/hostname > $S" % SENTINEL,
            "S=%s; mkdir ${S}" % SENTINEL,
            'S=%s; mkdir "$S"' % SENTINEL,
            "export S=%s; mkdir $S" % SENTINEL,
        ):
            _reason(guard.check(_payload(cmd)))

    def test_safe_set_allows_still_hold(self):
        assert guard.check(_payload("rm %s" % SENTINEL)) is None
        assert guard.check(_payload("cat %s" % SENTINEL)) is None
        assert guard.check(_payload("touch somefile.txt")) is None


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

    def test_rm_allowed_end_to_end(self):
        assert self._decision("rm %s" % SENTINEL) == "allow"

    def test_cat_allowed_end_to_end(self):
        assert self._decision("cat %s" % SENTINEL) == "allow"

    def test_unrelated_touch_allowed_end_to_end(self):
        assert self._decision("touch somefile.txt") == "allow"


class TestPowerShellDialect:
    """C4e (2026-08-07, guard-dialect-coverage.md row 22): PowerShell-syntax
    coverage via `_dialect`/`_sentinel_creation_guard.SentinelCreationDetector
    .evaluate_for_dialect`. This guard's own `_ApprovalSentinelDetector`
    default-deny `_segment_denies` override still applies on the PowerShell
    leg (polymorphic dispatch through the shared engine's dialect-aware
    entry point) -- a safe head (`git status`) allows, an unsafe head
    mentioning the sentinel denies. A guard declaring `Dialect.POWERSHELL`
    must reach a correct verdict or record SILENT -- never a bare clean
    (AC3)."""

    @staticmethod
    def _ps_payload(command):
        return {
            "tool_name": "PowerShell",
            "tool_input": {"command": command},
            "session_id": "sess1",
            "cwd": "/repo",
        }

    def test_set_content_cmdlet_denies(self):
        out = guard.check(self._ps_payload("Set-Content %s" % SENTINEL))
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_redirect_denies(self):
        out = guard.check(self._ps_payload("echo ok > %s" % SENTINEL))
        assert out is not None

    def test_default_deny_posture_still_applies_unsafe_head(self):
        """`Copy-Item` is not in `_SAFE_ARGV0` -- mentioning the sentinel
        anywhere in its tokens denies, same default-deny inversion as the
        bash leg."""
        out = guard.check(self._ps_payload("Copy-Item %s C:\\tmp\\x" % SENTINEL))
        assert out is not None

    def test_safe_git_status_allows_with_no_silence(self):
        from coordinator_core.bash_guards import _verdict

        with _verdict.collecting() as silences:
            out = guard.check(self._ps_payload("git status"))
        assert out is None
        assert silences == []

    def test_grammar_gap_shape_records_silent_not_clean(self):
        from coordinator_core.bash_guards import _verdict

        with _verdict.collecting() as silences:
            out = guard.check(self._ps_payload("Remove-Item x &> out.txt"))
        assert out is None
        assert any(
            s.guard_name == "block_approval_sentinel_creation" for s in silences
        )

    def test_non_bash_non_powershell_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_stale_taint_state_not_leaked_from_prior_bash_call(self):
        bash_payload = _payload("S=%s; touch $S" % SENTINEL)
        assert guard.check(bash_payload) is not None

        ps_out = guard.check(self._ps_payload("git status"))
        assert ps_out is None


class TestItem33ReadableScriptOverride:
    """Item 33 (bounded, 2026-09-26): a bare `bash|sh|python3 <path>`
    invocation reads the script and applies the guard's own mention/taint
    scan instead of denying unconditionally. `machine-local` (the reported
    false positive) is the motivating clean case; every adversarial shape
    the row's brief names must still deny."""

    def _write(self, tmp_path, name, content):
        path = tmp_path / name
        path.write_text(content)
        return path

    def test_clean_script_allows(self, tmp_path):
        self._write(tmp_path, "forwarder.sh", "#!/bin/bash\nexec python3 \"$@\"\n")
        out = guard.check(_payload("bash forwarder.sh", cwd=str(tmp_path)))
        assert out is None

    def test_clean_script_allows_for_sh(self, tmp_path):
        self._write(tmp_path, "forwarder.sh", "echo hello\n")
        out = guard.check(_payload("sh forwarder.sh", cwd=str(tmp_path)))
        assert out is None

    def test_clean_script_allows_for_python3(self, tmp_path):
        self._write(tmp_path, "forwarder.py", "print('hello world')\n")
        out = guard.check(_payload("python3 forwarder.py", cwd=str(tmp_path)))
        assert out is None

    def test_script_mentioning_basename_denies(self, tmp_path):
        self._write(tmp_path, "evil.sh", "touch %s\n" % SENTINEL)
        out = guard.check(_payload("bash evil.sh", cwd=str(tmp_path)))
        _reason(out)

    def test_script_mentioning_basename_via_python3_denies(self, tmp_path):
        self._write(tmp_path, "evil.py", "open('%s', 'w').close()\n" % SENTINEL)
        out = guard.check(_payload("python3 evil.py", cwd=str(tmp_path)))
        _reason(out)

    def test_chained_script_mentioning_basename_denies(self, tmp_path):
        self._write(
            tmp_path,
            "chain.sh",
            "bash other.sh\ntouch %s\n" % SENTINEL,
        )
        out = guard.check(_payload("bash chain.sh", cwd=str(tmp_path)))
        _reason(out)

    def test_script_written_earlier_in_same_command_still_denies(self, tmp_path):
        # Content itself is clean, but it was just written by an EARLIER
        # segment of the SAME command -- the guard cannot trust that
        # content is what actually runs (class docstring "ITEM 33
        # NARROWING").
        cmd = "printf 'echo hello\\n' > w.sh; bash w.sh"
        out = guard.check(_payload(cmd, cwd=str(tmp_path)))
        _reason(out)

    def test_redirect_written_script_still_denies(self, tmp_path):
        cmd = "echo 'echo hello' > w.sh && bash w.sh"
        out = guard.check(_payload(cmd, cwd=str(tmp_path)))
        _reason(out)

    def test_backgrounded_earlier_segment_still_denies(self, tmp_path):
        self._write(tmp_path, "writer.sh", "echo write\n")
        self._write(tmp_path, "s.sh", "echo clean\n")
        cmd = "bash writer.sh & bash s.sh"
        out = guard.check(_payload(cmd, cwd=str(tmp_path)))
        _reason(out)

    def test_symlink_to_clean_script_still_denies(self, tmp_path):
        target = self._write(tmp_path, "real.sh", "echo clean\n")
        link = tmp_path / "link.sh"
        os.symlink(target, link)
        out = guard.check(_payload("bash link.sh", cwd=str(tmp_path)))
        _reason(out)

    def test_oversize_script_still_denies(self, tmp_path):
        self._write(
            tmp_path,
            "big.sh",
            "echo clean\n" + ("#" * (guard._MAX_SCRIPT_READ_BYTES + 1)),
        )
        out = guard.check(_payload("bash big.sh", cwd=str(tmp_path)))
        _reason(out)

    def test_missing_script_still_denies(self, tmp_path):
        out = guard.check(_payload("bash nowhere.sh", cwd=str(tmp_path)))
        _reason(out)

    def test_heredoc_fed_bash_still_denies(self, tmp_path):
        cmd = "bash <<'EOF'\necho hello\nEOF"
        out = guard.check(_payload(cmd, cwd=str(tmp_path)))
        _reason(out)

    def test_nested_dash_c_script_file_not_read_stays_denied(self, tmp_path):
        # ONE level deep only: a `bash <path>` nested inside a `-c` payload
        # is a different depth and keeps the inherited unconditional deny
        # (class docstring "ITEM 33 NARROWING": "ONE level deep").
        self._write(tmp_path, "clean.sh", "echo hello\n")
        cmd = "sh -c 'bash clean.sh'"
        out = guard.check(_payload(cmd, cwd=str(tmp_path)))
        _reason(out)

    def test_zsh_bare_file_stays_denied_regardless_of_content(self, tmp_path):
        # zsh is intentionally excluded from `_READABLE_SCRIPT_INTERPRETERS`
        # -- item 33 names only `bash|sh|python3`.
        self._write(tmp_path, "clean.sh", "echo hello\n")
        out = guard.check(_payload("zsh clean.sh", cwd=str(tmp_path)))
        _reason(out)

    def test_python_dash_m_still_allows_with_readable_script_override_present(
        self, tmp_path
    ):
        # Regression pin alongside the pre-existing
        # `test_python_dash_m_allows` -- `-m <module>` must never be
        # misread as a script PATH by the new override.
        out = guard.check(_payload("python3 -m pytest", cwd=str(tmp_path)))
        assert out is None

    def test_guard_level_clean_script_allows_regardless_of_cwd_spelling(
        self, tmp_path
    ):
        # Same clean-script case as `test_clean_script_allows`, pinned at
        # this guard's own `check()` (not the full dispatch chain, which
        # has its own, unrelated sibling guards over the same bare-file
        # shape -- out of scope for this row).
        self._write(tmp_path, "forwarder.sh", "exec python3 \"$@\"\n")
        out = guard.check(_payload("bash forwarder.sh", cwd=str(tmp_path)))
        assert out is None
