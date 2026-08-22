"""Tests for coordinator_core.bash_guards.block_subagent_stash_creation.

Covers the CREATE-side half of the stash-stack gap: `git stash push` and the
bare `git stash` / flag-only implicit-push form (`git stash -u`), denied for
a subagent (raw `agent_id` present) and allowed for the main-loop EM (no
`agent_id` at all). Also covers the deliberate out-of-scope carve-out for
every other second-level `git stash` subcommand (`list`, `show`, `pop`,
`apply`, `drop`, `clear`, `branch`, `create`, `store`, `save`), the same
shell-shape/heredoc/flag-value-false-positive coverage its sibling
`block_stash_destruction` pins, and that the deny message leads with the two
non-shared-tree alternatives before explaining the refusal.

Pure Python -- no shell spawns, no git repo required.

Spec backlink: coordinator_core/bash_guards/block_subagent_stash_creation.py
"""

from __future__ import annotations

from coordinator_core.bash_guards import block_subagent_stash_creation as guard


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
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": "x"}}) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("", agent_id="a1")) is None

    def test_malformed_tool_input_allows(self):
        assert guard.check({"tool_name": "Bash", "tool_input": "not-a-dict"}) is None

    def test_command_without_stash_word_allows(self):
        assert guard.check(_payload("git status --porcelain", agent_id="a1")) is None


class TestSubagentDeniesPush:
    def test_bare_stash_denies(self):
        _reason(guard.check(_payload("git stash", agent_id="a1")))

    def test_explicit_push_denies(self):
        _reason(guard.check(_payload("git stash push", agent_id="a1")))

    def test_scoped_push_denies(self):
        """Scoped is still global in effect -- see module docstring."""
        _reason(
            guard.check(
                _payload("git stash push -- coordinator/agents/", agent_id="a1")
            )
        )

    def test_flag_only_implicit_push_denies(self):
        _reason(guard.check(_payload("git stash -u", agent_id="a1")))

    def test_push_with_message_denies(self):
        _reason(guard.check(_payload("git stash push -m wip", agent_id="a1")))


class TestEmAllowed:
    """No `agent_id` in the payload -> main-loop EM -> allowed, exactly the
    same discriminator `block_subagent_commit.py` uses."""

    def test_em_bare_stash_allows(self):
        assert guard.check(_payload("git stash")) is None

    def test_em_explicit_push_allows(self):
        assert guard.check(_payload("git stash push")) is None

    def test_em_flag_only_implicit_push_allows(self):
        assert guard.check(_payload("git stash -u")) is None

    def test_em_scoped_push_allows(self):
        assert guard.check(_payload("git stash push -- src/x.py")) is None


class TestSubagentOutOfScopeSubcommandsAllow:
    """Everything that is not push/bare is OUT OF SCOPE for this guard (see
    module docstring "SCOPE") -- denied elsewhere, or never denied at all."""

    def test_pop_allows(self):
        assert guard.check(_payload("git stash pop", agent_id="a1")) is None

    def test_apply_allows(self):
        assert guard.check(_payload("git stash apply stash@{1}", agent_id="a1")) is None

    def test_drop_allows(self):
        assert guard.check(_payload("git stash drop", agent_id="a1")) is None

    def test_clear_allows(self):
        assert guard.check(_payload("git stash clear", agent_id="a1")) is None

    def test_list_allows(self):
        assert guard.check(_payload("git stash list", agent_id="a1")) is None

    def test_show_allows(self):
        assert guard.check(_payload("git stash show -p stash@{1}", agent_id="a1")) is None

    def test_save_allows(self):
        assert guard.check(_payload("git stash save wip", agent_id="a1")) is None

    def test_branch_create_store_allow(self):
        for sub in ("branch topic stash@{0}", "create", "store abc123"):
            assert guard.check(_payload("git stash " + sub, agent_id="a1")) is None

    def test_unrecognized_subcommand_allows(self):
        assert guard.check(_payload("git stash frobnicate", agent_id="a1")) is None


class TestFlagValueFalsePositives:
    def test_push_with_drop_as_message_denies_as_push_not_drop(self):
        """The command IS a push (denied here), but the deny_kind must be
        `push`, not misread the flag VALUE `drop` as a second-level
        subcommand -- proves `remaining[0]` positional matching, not a
        flag-skipping scan."""
        reason = _reason(
            guard.check(_payload("git stash push -m drop -- src/x.py", agent_id="a1"))
        )
        assert "git stash push" in reason

    def test_non_git_command_mentioning_stash_push_allows(self):
        assert guard.check(_payload("echo git stash push", agent_id="a1")) is None

    def test_grep_for_stash_push_allows(self):
        assert guard.check(_payload("rg 'git stash push' coordinator_core/", agent_id="a1")) is None


class TestShellShapes:
    def test_chained_after_and_denies(self):
        _reason(guard.check(_payload("git stash list && git stash push", agent_id="a1")))

    def test_chained_after_semicolon_denies(self):
        _reason(guard.check(_payload("cd /repo; git stash push", agent_id="a1")))

    def test_leading_env_assignment_denies(self):
        _reason(guard.check(_payload("GIT_TRACE=1 git stash push", agent_id="a1")))

    def test_passthrough_wrapper_denies(self):
        _reason(guard.check(_payload("nice git stash push", agent_id="a1")))

    def test_wrapper_with_own_argv_denies(self):
        _reason(guard.check(_payload("timeout 30 git stash push", agent_id="a1")))
        _reason(guard.check(_payload("ionice -c2 git stash", agent_id="a1")))

    def test_sh_c_payload_denies(self):
        _reason(guard.check(_payload("sh -c 'git stash push'", agent_id="a1")))

    def test_bundled_c_flag_payload_denies(self):
        _reason(guard.check(_payload("bash -ic 'git stash push'", agent_id="a1")))

    def test_git_c_global_option_before_subcommand_denies(self):
        _reason(guard.check(_payload("git -C /repo stash push", agent_id="a1")))

    def test_git_exe_basename_denies(self):
        _reason(guard.check(_payload("git.exe -C /repo stash push", agent_id="a1")))


class TestRedirectionDisplacement:
    """2026-08-22 fix (UNSCOPED-STASH GAP, REOPENED -- same shape as
    `block_subagent_destructive_action.py`'s sibling fix, see
    `_strip_leading_redirection_tokens`'s docstring): `shlex` has no concept
    of shell redirection, so `git stash 2>&1` tokenizes `remaining` to
    `["2>&1"]` -- a token this guard's `_classify_stash_subcommand` did not
    recognize as `None`/`-`-prefixed/`"push"`, so it fell through to allow.
    Confirmed live: a `coordinator:review-integrator` subagent's `git stash
    2>&1 | head -5; echo done` swept 144 files on a shared tree
    (state/bug-backlog/2026-08-21-subagent-unscoped-stash-push-swept-144-f-
    ea557efb4908.yaml). Each case reproduces the EXACT displacement shape,
    not a generic re-test of the existing stash coverage above.
    """

    def test_bare_with_stderr_redirect_denies(self):
        _reason(guard.check(_payload("git stash 2>&1", agent_id="a1")))

    def test_bare_with_stderr_redirect_and_pipe_denies(self):
        # The EXACT shape from the incident transcript (repo path elided).
        _reason(
            guard.check(_payload("cd repo && git stash 2>&1 | head -5; echo done", agent_id="a1"))
        )

    def test_bare_with_stdout_redirect_denies(self):
        _reason(guard.check(_payload("git stash >/dev/null", agent_id="a1")))

    def test_bare_with_stderr_redirect_to_file_denies(self):
        _reason(guard.check(_payload("git stash 2>/dev/null", agent_id="a1")))

    def test_bare_with_separated_redirect_target_denies(self):
        # Whitespace between the operator and its target still yields two
        # `shlex` tokens (`[">", "/dev/null"]`) -- both must be consumed.
        _reason(guard.check(_payload("git stash > /dev/null", agent_id="a1")))

    def test_push_with_redirect_still_denies(self):
        # A real token (`push`) already occupies the first-argument position
        # before the redirect -- denied even pre-fix; kept as a boundary
        # case so a future change can't silently narrow the strip.
        _reason(guard.check(_payload("git stash push 2>&1", agent_id="a1")))

    def test_dash_u_with_redirect_still_denies(self):
        _reason(guard.check(_payload("git stash -u 2>&1", agent_id="a1")))

    def test_powershell_bare_with_redirect_denies(self):
        # `_evaluate_powershell_segments` mirrors the Bash leg's
        # `remaining[0]` read exactly -- same displacement, same fix.
        payload = {
            "tool_name": "PowerShell",
            "tool_input": {"command": "git stash 2>&1"},
            "session_id": "sess1",
            "cwd": "/repo",
            "agent_id": "a1",
        }
        _reason(guard.check(payload))


class TestHeredocBodies:
    def test_heredoc_prose_quoting_the_verb_allows(self):
        cmd = (
            "cat <<EOF > incident.md\n"
            "The subagent ran; git stash push and lost a peer's entry.\n"
            "EOF"
        )
        assert guard.check(_payload(cmd, agent_id="a1")) is None

    def test_real_invocation_outside_heredoc_still_denies(self):
        cmd = (
            "cat <<EOF > notes.md\n"
            "some prose\n"
            "EOF\n"
            "git stash push"
        )
        _reason(guard.check(_payload(cmd, agent_id="a1")))


class TestIdentityGate:
    """The entire reason this module denies-by-default on a present
    `agent_id`: unlike `block_subagent_destructive_action`'s undo-side gate,
    this create-side gate must NOT fail open on an unresolvable identity --
    see module docstring "IDENTITY-GATE POSTURE"."""

    def test_denies_with_only_agent_id(self):
        _reason(guard.check(_payload("git stash push", agent_id="a1")))

    def test_denies_with_agent_id_and_agent_type(self):
        _reason(
            guard.check(
                _payload("git stash push", agent_id="a1", agent_type="coordinator:executor")
            )
        )

    def test_em_typed_stash_push_allows(self):
        assert guard.check(_payload("git stash push")) is None

    def test_sibling_guard_still_fires_for_the_em_on_drop(self):
        """Confirms this module's own scope carve-out against its sibling:
        an EM-typed `git stash drop` is out of THIS guard's scope (allowed
        here) but still denied by `block_stash_destruction`."""
        from coordinator_core.bash_guards import block_stash_destruction as sibling

        em_payload = _payload("git stash drop")
        assert guard.check(em_payload) is None
        _reason(sibling.check(em_payload))


class TestDenyMessage:
    def test_leads_with_alternatives(self):
        msg = _reason(guard.check(_payload("git stash push", agent_id="a1")))
        assert "did you mean" in msg.lower()
        alt_pos = msg.lower().index("did you mean")
        blocked_pos = msg.index("BLOCKED")
        assert alt_pos < blocked_pos

    def test_names_git_archive_alternative(self):
        msg = _reason(guard.check(_payload("git stash push", agent_id="a1")))
        assert "git archive" in msg

    def test_names_git_show_alternative(self):
        msg = _reason(guard.check(_payload("git stash push", agent_id="a1")))
        assert "git show" in msg

    def test_echoes_the_command(self):
        assert "git stash push" in _reason(
            guard.check(_payload("git stash push", agent_id="a1"))
        )

    def test_truncates_a_very_long_command(self):
        long_cmd = "git stash push -m " + ("x" * 500)
        assert "..." in _reason(guard.check(_payload(long_cmd, agent_id="a1")))
