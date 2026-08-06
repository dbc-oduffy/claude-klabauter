"""Tests for coordinator_core.bash_guards.block_stash_destruction.

Covers the DENY (`drop`/`clear`) vs ALLOW (everything else) second-level `git
stash` subcommand split, the deliberate NON-default-deny posture on an
unrecognized token, the `pop`/`apply` carve-out, chaining/env-assignment/
wrapper/`sh -c` shell shapes, the flag-value false-positive class positional
matching exists to avoid (`git stash push -m drop -- <paths>`), heredoc-body
stripping, and that the guard is NOT identity-gated -- it fires with or
without `agent_id`/`agent_type` present, unlike its identity-gated sibling
`block_subagent_destructive_action`, which exempts the main-loop EM and is the
entire reason this module exists.

Pure Python -- no shell spawns, no git repo required.

Spec backlink: coordinator_core/bash_guards/block_stash_destruction.py
"""

from __future__ import annotations

from coordinator_core.bash_guards import block_stash_destruction as guard


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
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        assert guard.check({"tool_name": "Bash", "tool_input": "not-a-dict"}) is None

    def test_command_without_stash_word_allows(self):
        assert guard.check(_payload("git status --porcelain")) is None


class TestDenySet:
    def test_bare_drop_denies(self):
        _reason(guard.check(_payload("git stash drop")))

    def test_drop_with_explicit_index_denies(self):
        _reason(guard.check(_payload("git stash drop stash@{2}")))

    def test_clear_denies(self):
        _reason(guard.check(_payload("git stash clear")))

    def test_drop_with_quiet_flag_after_subcommand_denies(self):
        _reason(guard.check(_payload("git stash drop -q stash@{0}")))

    def test_deny_kind_names_the_subcommand(self):
        assert "git stash drop" in _reason(guard.check(_payload("git stash drop")))
        assert "git stash clear" in _reason(guard.check(_payload("git stash clear")))


class TestAllowSet:
    """Everything that is not `drop`/`clear`. See the module docstring's
    DELIBERATE ALLOW-LIST -- this guard is deliberately NOT default-deny on an
    unrecognized second-level token, because bare `git stash` and the flag-only
    implicit-push form are git-stash's most common shapes and a default-deny
    rule would block the EM from stashing at all."""

    def test_bare_stash_allows(self):
        assert guard.check(_payload("git stash")) is None

    def test_implicit_push_flag_only_form_allows(self):
        assert guard.check(_payload("git stash -u")) is None

    def test_scoped_push_allows(self):
        assert guard.check(_payload("git stash push -- coordinator/agents/")) is None

    def test_list_allows(self):
        assert guard.check(_payload("git stash list")) is None

    def test_show_allows(self):
        assert guard.check(_payload("git stash show -p stash@{1}")) is None

    def test_save_allows(self):
        assert guard.check(_payload("git stash save wip")) is None

    def test_branch_create_store_allow(self):
        for sub in ("branch topic stash@{0}", "create", "store abc123"):
            assert guard.check(_payload("git stash " + sub)) is None

    def test_unrecognized_subcommand_allows_not_default_deny(self):
        assert guard.check(_payload("git stash frobnicate")) is None


class TestPopApplyCarveOut:
    """`pop`/`apply` stay ALLOWED here on purpose -- see the module docstring's
    "WHY DROP/CLEAR AND NOT POP/APPLY". They remain denied for SUBAGENTS by
    `block_subagent_destructive_action`; this main-loop leg must not take the
    EM's own restore path away from it. Guarding these as tests so a later
    "tighten it up" pass has to argue with the rationale rather than silently
    widen the deny set."""

    def test_pop_allows(self):
        assert guard.check(_payload("git stash pop")) is None

    def test_pop_with_index_allows(self):
        assert guard.check(_payload("git stash pop stash@{3}")) is None

    def test_apply_allows(self):
        assert guard.check(_payload("git stash apply stash@{1}")) is None


class TestFlagValueFalsePositives:
    """The class that `remaining[0]` positional matching exists to avoid: a
    flag VALUE that reads as a deny verb. A scan that skipped leading flags to
    find the "real" subcommand would deny all of these."""

    def test_push_with_drop_as_message_allows(self):
        assert guard.check(_payload("git stash push -m drop -- src/x.py")) is None

    def test_push_with_clear_in_quoted_message_allows(self):
        assert guard.check(_payload('git stash push -m "clear the decks" -- src/x.py')) is None

    def test_push_with_pathspec_named_drop_allows(self):
        assert guard.check(_payload("git stash push -- src/drop_handler.py")) is None

    def test_non_git_command_mentioning_stash_drop_allows(self):
        assert guard.check(_payload("echo git stash drop")) is None

    def test_grep_for_stash_drop_allows(self):
        assert guard.check(_payload("rg 'git stash drop' coordinator_core/")) is None


class TestShellShapes:
    def test_chained_after_and_denies(self):
        _reason(guard.check(_payload("git stash list && git stash drop")))

    def test_chained_after_semicolon_denies(self):
        _reason(guard.check(_payload("cd /repo; git stash drop")))

    def test_leading_env_assignment_denies(self):
        _reason(guard.check(_payload("GIT_TRACE=1 git stash drop")))

    def test_passthrough_wrapper_denies(self):
        _reason(guard.check(_payload("nice git stash drop")))

    def test_wrapper_with_own_argv_denies(self):
        _reason(guard.check(_payload("timeout 30 git stash drop")))
        _reason(guard.check(_payload("ionice -c2 git stash clear")))

    def test_sh_c_payload_denies(self):
        _reason(guard.check(_payload("sh -c 'git stash drop'")))

    def test_bundled_c_flag_payload_denies(self):
        _reason(guard.check(_payload("bash -ic 'git stash clear'")))

    def test_git_c_global_option_before_subcommand_denies(self):
        _reason(guard.check(_payload("git -C /repo stash drop")))

    def test_git_exe_basename_denies(self):
        _reason(guard.check(_payload("git.exe -C /repo stash drop")))


class TestHeredocBodies:
    """A heredoc body is stdin DATA, never shell command text. Persisting a
    document whose prose quotes `git stash drop` must not deny."""

    def test_heredoc_prose_quoting_the_verb_allows(self):
        cmd = (
            "cat <<EOF > incident.md\n"
            "The EM ran; git stash drop and lost a peer's entry.\n"
            "EOF"
        )
        assert guard.check(_payload(cmd)) is None

    def test_real_invocation_outside_heredoc_still_denies(self):
        cmd = (
            "cat <<EOF > notes.md\n"
            "some prose\n"
            "EOF\n"
            "git stash drop"
        )
        _reason(guard.check(_payload(cmd)))

    def test_real_invocation_after_heredoc_denies_when_separator_is_explicit(self):
        """The same shape with a `;` separator -- the segmentation the shared
        tokenizer DOES handle -- denies today, showing the guard's own
        classification is sound and the gap above is purely the newline seam."""
        cmd = (
            "cat <<EOF > notes.md\n"
            "some prose\n"
            "EOF\n"
            "; git stash drop"
        )
        _reason(guard.check(_payload(cmd)))


class TestNotIdentityGated:
    """The entire reason this module exists: `block_subagent_destructive_
    action` fails OPEN when no subagent identity resolves, exempting the
    main-loop EM."""

    def test_denies_with_no_identity_fields_at_all(self):
        _reason(guard.check(_payload("git stash drop")))

    def test_denies_with_subagent_identity_present(self):
        _reason(guard.check(_payload("git stash drop", agent_id="a1", agent_type="executor")))

    def test_sibling_guard_exempts_the_em_on_the_same_command(self):
        from coordinator_core.bash_guards import block_subagent_destructive_action as sibling

        em_payload = _payload("git stash drop")
        assert sibling.check(em_payload) is None, (
            "sibling is expected to allow an EM-typed stash drop -- if this "
            "starts denying, the gap this module closes may have moved"
        )
        _reason(guard.check(em_payload))


class TestDenyMessage:
    def test_names_the_no_reflog_fact(self):
        msg = _reason(guard.check(_payload("git stash drop")))
        assert "reflog" in msg

    def test_offers_the_read_without_dropping_alternative(self):
        msg = _reason(guard.check(_payload("git stash drop")))
        assert "git show stash@{N}" in msg

    def test_echoes_the_command(self):
        assert "git stash clear" in _reason(guard.check(_payload("git stash clear")))

    def test_truncates_a_very_long_command(self):
        long_cmd = "git stash drop " + ("x" * 500)
        assert "..." in _reason(guard.check(_payload(long_cmd)))
