"""Tests for coordinator_core.bash_guards.block_approval_sentinel_creation.

Covers the DENY set (redirection, `touch`/`cp`/`mv`/`install`/`ln`/`tee`,
`sed -i`, `python -c`), the ALLOW set (reads and `rm`), chaining/env-
assignment shell shapes, and -- critically -- dispatch-level (end-to-end via
`dispatch.evaluate_payload_json`) coverage, since `offer-git-c`'s
allow+updatedInput short-circuit is exactly what hid the analogous ordering
bug in `block_worktree_creation.py`: a guard-level-only suite would have
stayed green while the guard was unreachable for `cd <dir> && <cmd>` shapes.

Pure Python -- no shell spawns, no filesystem writes.

Spec backlink: coordinator_core/bash_guards/block_approval_sentinel_creation.py
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import block_approval_sentinel_creation as guard
from coordinator_core.bash_guards import dispatch


SENTINEL = ".coordinator-doctrine-edit-approved"


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
        # Default-deny posture: source or destination, see module docstring.
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
        # 2026-07-30 inversion: `sed` is not on the narrow safe-argv0
        # allowlist (rm + read-only inspection + read-only git), so ANY
        # mention of the sentinel in a `sed` invocation denies now, -i or
        # not -- the guard no longer tries to reason about whether this
        # particular sed call would actually write. Was ALLOW under the
        # old enumerated-dangerous-command posture; the new default-deny
        # posture is deliberately stricter (a rephrase is the only cost).
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
        # shlex merges adjacent quoted segments with no intervening
        # whitespace into a single token.
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
        # Main-loop EM calls carry no agent_id/agent_type at all -- this
        # guard must still fire (see module docstring "NOT IDENTITY-GATED").
        out = guard.check(_payload("touch %s" % SENTINEL))
        _reason(out)


class TestNoOverride:
    def test_env_var_shaped_override_does_not_allow(self, monkeypatch):
        # This guard deliberately consults NO override env var at all --
        # confirm setting one that would work for a sibling guard has no
        # effect here.
        monkeypatch.setenv("COORDINATOR_OVERRIDE_APPROVAL_SENTINEL", "1")
        out = guard.check(_payload("touch %s" % SENTINEL))
        _reason(out)


class TestDenyMessageDiscipline:
    """2026-07-28 fix: this guard used to interpolate the raw command (up
    to 200 chars) and the literal sentinel basename into its deny text --
    so a denied `python3 -c "open('...', 'w').close()"` echoed working
    exploit code straight back to the agent. Brought in line with
    `block_worktree_sentinel_creation`'s no-echo discipline."""

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
    """2026-07-28 diagnosability fix: an indirection deny (payload
    unexaminable) used to get the SAME fixed "this command would create or
    modify" assertion as a direct deny (payload positively matched), which
    is false on the indirection path -- an operator who greps their own
    script and finds no sentinel reference gets told the opposite of what
    the guard actually determined. Covers both branches' message text and
    confirms no-echo discipline holds on both."""

    def test_direct_deny_message_unchanged(self):
        out = guard.check(_payload("touch %s" % SENTINEL))
        reason = _reason(out)
        assert "creates/modifies the PM-approval sentinel" in reason
        assert "Ask the PM to create it" in reason
        assert SENTINEL not in reason

    def test_indirection_deny_does_not_assert_creation(self):
        # bin/install-git-hooks.sh-shaped: bare interpreter-invoked script,
        # content unexamined -- the exact field-reported false-positive
        # shape (2026-07-28).
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
        assert "run its underlying steps directly" in reason
        assert "EM/PM" in reason

    def test_recursive_indirection_deny_still_redacts_the_sentinel(self):
        # bash -c "touch <sentinel>" -- the flagship confirmed bypass.
        # Under the 2026-07-30 default-deny inversion this is now caught by
        # the DIRECT path, not the indirection-recursion path: the `-c`
        # payload is a single shlex-merged token whose text mentions the
        # sentinel, and the segment's own head command (`bash`) is not on
        # the safe-argv0 allowlist, so `_segment_denies` matches it before
        # `_evaluate_segment_indirection` is ever consulted. The message is
        # therefore the fixed DIRECT-class text, not the indirection
        # "cannot examine" text -- what still matters, and is still pinned
        # here, is that the sentinel basename itself never appears in it.
        out = guard.check(_payload('bash -c "touch %s"' % SENTINEL))
        reason = _reason(out)
        assert SENTINEL not in reason
        assert "creates/modifies the PM-approval sentinel" in reason


class TestIndirectionWrapperShapesDeny:
    """2026-07-28 fix -- the confirmed live bypass:
    ``bash -c "touch <sentinel>"`` created the sentinel successfully before
    this fix, defeating this guard via one level of interpreter
    indirection. Covers every shape enumerated in the fix dispatch brief.
    """

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

    def test_unrelated_xargs_allows_is_still_denied_outright(self):
        # xargs is denied OUTRIGHT for any payload (content not present in
        # the command text) -- same over-block posture as the sibling
        # destructive-action guard's xargs handling.
        _reason(guard.check(_payload("echo hello | xargs cat")))

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
    """2026-07-30 forge-closure fix. A live probe walked straight around
    the old enumerated-command allowlist (`touch`/`cp`/`mv`/`install`/`ln`/
    `tee`/`sed -i`/`python -c`/`dd of=`) using tools the allowlist never
    considered at all: `mkdir`, `curl -o`, `wget -O`, `rsync`, `git checkout
    HEAD --`, `unzip -d`. Confirmed live via `mkdir
    .coordinator-doctrine-edit-approved` producing a real, honoured
    30-minute approval window before this fix. Every shape here must now
    deny under the default-deny-unless-safe posture."""

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
    """The narrow safe-op allowlist the inversion carves out: removal (`rm`)
    and read-only inspection stay ALLOWED even when they name the sentinel
    directly, and read-only `git` subcommands stay allowed while any
    write-shaped `git` subcommand denies."""

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

    # Driven off the constant, not a hand-copied literal list: the old
    # hardcoded five omitted `rev-parse` and `describe` (allowlisted but
    # never exercised) and would have gone on passing while `check-ignore`
    # was denied. A parametrize over the real set cannot drift from it.
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
        # A shell redirect writes the file regardless of which command
        # precedes it -- `cat` being on the read-only allowlist must not
        # launder a write via `>`.
        _reason(guard.check(_payload("cat /etc/hostname > %s" % SENTINEL)))


class TestIndirectionStillCaughtAfterInversion:
    """Indirection wrappers around a now-denied creation verb (`mkdir`,
    rather than the old `touch`) must still deny post-inversion -- the
    default-deny posture denies these even more readily than before, since
    the wrapped payload's own text still mentions the sentinel and the
    wrapper binary (`sh`/`env`/`xargs`) is never on the safe-argv0
    allowlist."""

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
    """2026-07-30 round-two forge-closure fix. A live probe found that a
    variable assigned in one segment and dereferenced in a later segment of
    the SAME command string evaded every rule above, because they are all
    per-segment with no cross-segment data flow -- while working perfectly
    when bash actually executes it (bash resolves the variable within the
    same invocation). Covers the four confirmed-live bypasses plus the
    taint-propagation shapes (`${VAR}` brace form, double-quoted
    dereference, `export`, and a multi-segment gap) the fix must also
    close."""

    def test_assign_then_mkdir_denies(self):
        _reason(guard.check(_payload("S=%s; mkdir $S" % SENTINEL)))

    def test_assign_then_touch_denies(self):
        _reason(guard.check(_payload("S=%s; touch $S" % SENTINEL)))

    def test_assign_then_ln_denies(self):
        _reason(guard.check(_payload("L=%s; ln -s /tmp/x $L" % SENTINEL)))

    def test_assign_then_redirect_through_safe_head_denies(self):
        # `cat` is on the safe-argv0 allowlist -- the redirect target is
        # what writes the file, not the (safe) head command.
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
        # A variable never assigned a value mentioning the sentinel must
        # not be treated as tainted merely because it is dereferenced next
        # to a command this guard would otherwise scrutinize.
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
        # Documented in the module docstring's "KNOWN OPEN GAP -- DYNAMIC
        # STRING CONSTRUCTION AND GLOB-SHAPED NEAR-MISSES" block: a purely
        # lexical matcher cannot statically evaluate a shell glob, so a
        # single-character-wildcard near-miss of the basename is NOT
        # detected -- pinned here as a KNOWN gap, not an untested oversight.
        assert guard.check(_payload("touch .coordinator-doctrine-edit-approv?d")) is None

    def test_previously_denied_forms_still_deny(self):
        # Every shape the brief calls out as "already closed" before this
        # fix must remain denied -- the taint fix must not regress them.
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
        # A=<sentinel>; B=$A; mkdir $B
        # shell-doc-ok: the line above transcribes the exact shell payload
        # under test; it is the case's only statement of what is asserted.
        _reason(
            guard.check(
                _payload("A=%s; B=$A; mkdir $B" % SENTINEL)
            )
        )

    def test_two_hop_chain_denies(self):
        # A=<sentinel>; B=$A; C=$B; mkdir $C
        # shell-doc-ok: the line above transcribes the exact shell payload
        # under test; it is the case's only statement of what is asserted.
        _reason(
            guard.check(
                _payload("A=%s; B=$A; C=$B; mkdir $C" % SENTINEL)
            )
        )

    def test_longer_chain_denies(self):
        # A chain of five hops -- the fixed-point loop must not stop early.
        _reason(
            guard.check(
                _payload(
                    "A=%s; B=$A; C=$B; D=$C; E=$D; F=$E; mkdir $F" % SENTINEL
                )
            )
        )

    def test_chain_crossing_unrelated_intervening_segment_denies(self):
        # The chain's second hop is separated from the first by an
        # unrelated command -- taint must still flow across it.
        _reason(
            guard.check(
                _payload(
                    "A=%s; B=$A; echo unrelated; ls -la; mkdir $B" % SENTINEL
                )
            )
        )

    def test_string_construction_near_miss_is_knowingly_allowed(self):
        # Documented in the module docstring's "KNOWN OPEN GAP -- DYNAMIC
        # STRING CONSTRUCTION AND GLOB-SHAPED NEAR-MISSES" block, under
        # "VARIABLE-ASSEMBLED BASENAMES": neither `S` nor `S2` is ever
        # assigned a value containing the sentinel basename as a
        # contiguous substring -- the basename only becomes complete once
        # bash concatenates the two fragments at runtime, which is string
        # construction, not variable chaining, and is explicitly NOT
        # closed by the transitive-taint fix. Pinned here as a KNOWN gap,
        # not an untested oversight.
        cmd = 'S=".coordinator-doctrine-edit-"; S2="${S}approved"; mkdir $S2'
        assert guard.check(_payload(cmd)) is None

    def test_previously_denied_taint_forms_still_deny(self):
        # Every shape round two closed must remain denied under the
        # transitive extension.
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
    """Guard-level tests are not sufficient for this guard.

    ``guard.check()`` would deny ``cd /tmp && touch
    .coordinator-doctrine-edit-approved`` from the first commit, yet the
    same command could be ALLOWED end-to-end if this guard were registered
    after ``offer-git-c``: that check rewrites ``cd <dir> && git <sub>``
    into ``git -C <dir> <sub>`` and returns allow+updatedInput, which
    short-circuits every later guard. This exact bug was found and fixed in
    ``block_worktree_creation.py`` -- these tests go through
    ``dispatch.evaluate_payload_json`` so a future reordering that puts a
    rewrite/offer check ahead of this guard fails loudly here instead of
    silently disarming the ban.
    """

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
        # abs-path-ok: a literal PowerShell command-line argument the
        # detector tokenizes as text -- not a filesystem path this test
        # resolves or depends on.
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
        """Regression for the module-level singleton `_detector`: a prior
        bash-dialect call that tainted a variable must not leave
        `_tainted_vars` populated for a LATER PowerShell-dialect call on the
        same long-lived detector instance."""
        bash_payload = _payload("S=%s; touch $S" % SENTINEL)
        assert guard.check(bash_payload) is not None  # bash leg taints S

        ps_out = guard.check(self._ps_payload("git status"))
        assert ps_out is None
