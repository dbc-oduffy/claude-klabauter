"""Smoke tests for coordinator_core.bash_guards.block_subagent_destructive_action.

Not a full golden-parity corpus (that's a later W3b/plan-AC2 deliverable) --
representative payloads exercising Layer 1 surface-gating, the DUAL
OR-resolver identity axis, the AMBIGUOUS fail-closed path, and a
representative slice of the Layer-2 git deny/allow ladder (including the
near-miss --force-with-lease boundary the recipe explicitly flags).

Uses `monkeypatch` on the module's `resolve_effective_types` import to avoid
needing a real git repo + back-pointer chain on disk for AMBIGUOUS/back-pointer
cases; PRIMARY-leg (`agent_type`) cases exercise the real resolver end-to-end
since they need no git_root at all.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard


def _payload(command, agent_id="deadbeef0123", agent_type=None, session_id="sess1"):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": None,
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def test_non_bash_tool_allows():
    payload = {"tool_name": "Write", "tool_input": {"command": "git rebase -i"}}
    assert guard.check(payload) is None


def test_no_agent_id_allows_even_destructive_git():
    # Top-level EM call -- no agent_id in payload at all.
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git rebase -i HEAD~3"},
        "session_id": "sess1",
    }
    assert guard.check(payload) is None


def test_benign_command_allows_before_identity_cost():
    payload = _payload("ls -la && grep foo bar.txt", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_rebase_denies():
    payload = _payload("git rebase -i HEAD~3", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git rebase" in reason
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_git_status_allows_safe_forward():
    payload = _payload("git status", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_push_force_denies():
    payload = _payload("git push --force origin work/foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_push_force_with_lease_near_miss_allows():
    # Recipe-flagged near-miss: --force-with-lease must NOT match the
    # --force boundary pattern.
    payload = _payload(
        "git push --force-with-lease origin work/foo", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_git_reset_hard_denies():
    payload = _payload("git reset --hard origin/main", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git reset --hard" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_novel_verb_default_denies():
    # "gc" is not on the safe-forward allowlist -> default-deny.
    payload = _payload("git gc --aggressive", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "default-deny" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_rm_recursive_force_denies():
    payload = _payload("rm -rf state/scratch/", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "rm -r/-f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_rm_recursive_force_inside_a_scratch_looking_path_still_denies():
    # Regression for the 2026-08-03 rm-carve-out proposal: the PM took the
    # standing offer to ship the message correction ALONE, with no
    # scratchpad/temp-root exception of any kind. A target that LOOKS like
    # a session scratchpad path must deny exactly the same as any other rm.
    payload = _payload(
        "rm -rf /private/tmp/claude-501/some-repo/some-session/scratchpad/file.txt",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "rm -r/-f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_rm_deny_message_does_not_advise_a_sandbox_scoping_that_does_not_exist():
    # Regression for state/improvement-queue/2026-08-03-destructive-action-
    # deny-advises-a-sandbox-scoping-that-does-not-exist.yaml: the deny must
    # NEVER tell a subagent to "scope the op to your sandbox dir" (or any
    # other path-based carve-out) when no such mechanism is implemented.
    # This is the core regression -- it must keep passing even if the exact
    # wording of the message changes again later.
    payload = _payload("rm -rf state/scratch/", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sandbox dir" not in reason
    assert "Scope the op" not in reason
    assert "denied OUTRIGHT" in reason
    assert "no sandbox-\nscoped, path-based, or any other exception" in reason


def test_rm_bare_path_allows():
    payload = _payload("rm state/scratch/one-file.txt", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_chmod_recursive_denies():
    payload = _payload("chmod -R 755 some_dir/", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "chmod/chown -R" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_chmod_non_recursive_allows():
    payload = _payload("chmod +x some_script.sh", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_generic_branch_deny_message_does_not_advise_a_sandbox_scoping_that_does_not_exist():
    # Review: coordinator:code-reviewer (slice B, Finding 3) -- the generic
    # git/rm/chmod-chown-R fallback branch of `_build_reason` had the same
    # phantom "scope it to your sandbox dir" advice the rm-specific branch
    # was fixed for; it has since been corrected in-place, but with no test
    # pinning the fix. `chmod -R` routes to this generic branch (unlike
    # `rm -rf`, which has its own dedicated branch and its own absence
    # test above) -- assert absence, not merely presence of new wording, so
    # a future prose-cap pass can't silently reintroduce it.
    payload = _payload("chmod -R 755 some_dir/", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sandbox dir" not in reason
    assert "surface it to the EM" in reason


def test_generic_branch_deny_message_states_shell_surface_not_capability_boundary():
    # Regression for state/bug-backlog/2026-08-10-the-git-verb-bash-guard-
    # does-not-stop-a-a1caf2991aa6.yaml: the generic fallback branch of
    # `_build_reason` used to claim the destructive-git surface was
    # "EM-locked" with "No subagent override" -- phrasing that reads as a
    # capability boundary. It is not one: no shell-token matcher can
    # constrain an interpreter, and a subagent that can run Python (or any
    # other interpreter) reaches git regardless, unseen by this guard. The
    # message must say so plainly, still deny the same way, and still tell
    # the caller what to do instead.
    payload = _payload("git gc --aggressive", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell surface" in reason
    assert "not a capability" in reason
    assert "interpreter" in reason
    assert "EM-locked" not in reason
    assert "No subagent override" not in reason
    assert "surface it to the EM" in reason


def test_rm_deny_message_states_shell_surface_not_capability_boundary():
    # Review: coordinator:code-reviewer (S5) -- the rm branch (and the
    # machine-local branch below) claimed enforcement stronger than the
    # code holds ("denied OUTRIGHT ... no ... exception this guard will
    # honor", "There is NO subagent-honored override") with no disclosure
    # that this is a shell-token matcher, same bypass property as the
    # generic git branch's own disclaimer two branches down. Assert the
    # disclaimer landed here too, symmetrically, without weakening the
    # existing hard-deny wording pinned above.
    payload = _payload("rm -rf state/scratch/", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell surface" in reason
    assert "not a capability boundary" in reason
    assert "denied OUTRIGHT" in reason


def test_machine_local_write_deny_message_states_shell_surface_not_capability_boundary():
    payload = _payload(
        "machine-local set repos.foo /tmp/evil", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell surface" in reason
    assert "not a capability boundary" in reason
    assert "NO subagent-honored override" in reason


def test_ambiguous_identity_denies_even_on_benign_looking_git(monkeypatch):
    # The module no longer calls a single resolve_effective_types(payload, git_root)
    # seam -- check() now inlines the DUAL OR-resolver via resolve_git_root(),
    # _resolve_subagent_identity() (imported from write_guards.block_subagent_plan_body_write),
    # and _read_backpointer_subagent_type() (imported from bash_guards._helpers), each bound
    # into this module's own namespace by the `from X import name` statements at module
    # top -- so each is patched individually, on the `guard` module object, where check()
    # actually looks them up.
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: "AMBIGUOUS"
    )
    payload = _payload("git status", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "ambiguous agent identity" in reason
    assert "AMBIGUOUS" in reason


def test_empty_effective_type_confines_as_subagent(monkeypatch):
    # agent_id resolves truthy, but BOTH the primary leg (payload agent_type,
    # absent here) and the secondary leg (_read_backpointer_subagent_type)
    # resolve empty -- effective_type ends up "". Fixed 2026-07-30: a
    # subagent whose kind is unresolvable is still a subagent and is denied
    # (fail-closed), not allowed on the lookup-miss.
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: "")
    payload = _payload("git rebase -i", agent_type=None)
    payload["agent_id"] = "deadbeef0123"
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_unparseable_agent_id_with_known_agent_type_still_denies(monkeypatch):
    # _resolve_subagent_identity resolves to "" even though a raw agent_id was
    # supplied. Fixed 2026-07-30: raw_agent_id presence (not whether it
    # canonicalizes) is the EM/subagent discriminator, so this no longer
    # short-circuits to allow -- and here the PRIMARY leg (payload agent_type)
    # is already known as "coordinator:executor", so the guard denies via the
    # normal resolved-kind path, not merely the fail-closed unresolved-kind
    # default.
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "")
    payload = _payload("git rebase -i", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_command_allows():
    payload = {"tool_name": "Bash", "tool_input": {}, "agent_id": "deadbeef0123"}
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Indirection-wrapper hardening (2026-07-21 addition -- see module docstring
# "INDIRECTION-WRAPPER HARDENING" and
# cross-repo/inbox/2026-07-20-claude-central-em-subagent-destructive-guard-
# indirection-bypass.md). Each wrapper shape gets a denied case; shapes this
# guard can RELIABLY parse (`-c <string>`, `env ... <cmd>`) also get a
# benign-passes case, to demonstrate the recurse-and-match path does not
# over-block content it can actually examine.
# ---------------------------------------------------------------------------


def test_indirection_bash_script_file_now_advises_not_denies():
    # RESHAPE (2026-08-06, C18d, docs/plans/2026-08-06-apply-guard-class-
    # census.md): `bash <file>` -- script content is not in the command text
    # at all, so this guard genuinely cannot see it. It no longer denies
    # outright; it advises (allow + additionalContext) instead.
    payload = _payload("bash repro.sh", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    context = hso["additionalContext"]
    assert "indirection wrapper" in context
    assert "ADVISORY" in context


def test_indirection_sh_script_file_advises_even_when_benign():
    # Same shape, memo's own "legitimate `bash run-tests.sh`" example --
    # now advises rather than denies, because the guard cannot examine the
    # file's content either way.
    payload = _payload("sh run-tests.sh", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "sh <file>" in hso["additionalContext"]


def test_indirection_bash_noexec_syntax_check_allows():
    # Bug fix (2026-07-21: state/bug-backlog/2026-07-21-subagent-guard-
    # false-positive-bash-n-syn-5ef6ef52e2f9.yaml): `-n`/`--noexec` puts the
    # shell in parse-only mode -- it executes NOTHING regardless of what
    # the target file contains, so this must NOT hit the outright-deny
    # `<file>` shape above.
    payload = _payload("bash -n repro.sh", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_indirection_sh_noexec_long_flag_syntax_check_allows():
    # `--noexec` is the long-flag spelling of the same `-n` behavior.
    payload = _payload("sh --noexec run-tests.sh", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_indirection_bash_n_as_filename_substring_still_advises():
    # Bypass-risk check: a filename that merely CONTAINS "-n" as a substring
    # (not a standalone token) must NOT be mistaken for the `-n` flag -- the
    # opaque `<file>` shape still fires (now as an advisory, not a deny).
    payload = _payload("bash weird-name.sh", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "indirection wrapper" in hso["additionalContext"]


def test_indirection_bash_n_after_script_path_still_advises():
    # Bypass-risk check: `-n` AFTER the script path is a positional argument
    # PASSED TO the script (bash stops parsing its own options at the first
    # non-option token), not a noexec flag governing bash itself -- the
    # script still runs, so this must still surface the advisory.
    payload = _payload("bash malicious.sh -n", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "indirection wrapper" in hso["additionalContext"]


# ---------------------------------------------------------------------------
# Indirection-advisory message shape (RESHAPE 2026-08-06, C18d -- see module
# docstring's new "RESHAPE" comment on `check()`). The prior B2/B3 "message
# honesty" tests pinned a long deny message's "Safe forward paths" guidance
# text; that guidance no longer exists because this shape no longer blocks
# at all -- these tests now pin the advisory's own shape instead.
# ---------------------------------------------------------------------------


def test_indirection_advisory_message_does_not_claim_blocking():
    # The prior deny message's opening line read "indirection is blocked for
    # subagents" -- an overclaim even before this reshape (a bare python3-
    # file invocation was always allowed unconditionally). Now that this
    # shape is advisory, not a deny, the message must not claim blocking.
    payload = _payload("bash repro.sh", agent_type="coordinator:executor")
    result = guard.check(payload)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    context = hso["additionalContext"]
    assert "not blocked" in context
    assert "indirection is blocked for subagents" not in context


def test_indirection_deny_message_scratch_script_form_actually_allows():
    # A bare python3-file invocation is a plain allow (no advisory, no
    # deny) -- unaffected by the reshape, still verified directly.
    payload = _payload("python3 /tmp/scratch/repro.py", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_heredoc_fed_python3_destructive_payload_bypasses_inline_c_deny():
    # B3: the identical logical payload that denies inline via `-c` (see
    # `test_indirection_python_inline_c_destructive_payload_denies`)
    # allows when fed the same text via a heredoc instead -- a bare
    # python3 invocation (no `-c` token) is allowed unconditionally by
    # design, and `_strip_heredoc_bodies` removes the body before either
    # path is classified. Named divergence, not a defect -- see module
    # docstring.
    command = (
        "python3 <<'EOF'\n"
        "import subprocess\n"
        "subprocess.run(['git', 'push', '--force', 'origin', 'work/foo'])\n"
        "EOF"
    )
    payload = _payload(command, agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_indirection_inline_c_destructive_payload_denies():
    # `sh -c '<destructive>'` -- reliably parseable, recurse-matched against
    # the same rm deny regex used by the direct rm surface check.
    payload = _payload("sh -c 'rm -rf /tmp/scratch'", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "rm -r/-f" in reason
    assert "sh -c" in reason


def test_indirection_inline_c_benign_payload_allows():
    # Same wrapper shape, benign payload -- must NOT over-block since this
    # shape is reliably parseable.
    payload = _payload("sh -c 'echo hello world'", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_indirection_python_inline_c_destructive_payload_denies():
    # Memo's own explicit bypass example: `python -c '...'`.
    payload = _payload(
        "python -c 'import subprocess; subprocess.run([\"git\", \"push\", \"--force\"])'",
        agent_type="coordinator:executor",
    )
    # The embedded git literal is inside a python-source string, so this
    # exercises the recurse-into-payload path (git word present in the
    # unwrapped -c payload text) rather than a top-level literal match.
    result = guard.check(payload)
    assert result is not None
    assert "python -c" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_python_inline_c_readonly_git_word_probe_allows():
    # AC5 repro (docs/plans/2026-08-15-the-close-s-three-deferred-defects-
    # becom.md C3): a review-integrator's read-only probe -- the word "git"
    # appears only as quoted DATA (a substring count over file content),
    # no git binary invoked or invocable. Denied before this fix (the
    # INLINE-INTERPRETER CARVE-OUT routes any `python3 -c '...'` mentioning
    # "git" to the legacy free-text classifier, whose terminal catchall
    # denied on bare word presence).
    payload = _payload(
        'python3 -c "print(open(\'notes.txt\').read().count(\'git\'))"',
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_python_inline_c_readonly_git_word_probe_single_quoted_allows():
    # Same shape, single-quoted -c payload -- confirms the fix is not
    # tied to a specific quote style.
    payload = _payload(
        "python3 -c 'print(open(\"notes.txt\").read().count(\"git\"))'",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_python_inline_c_git_word_probe_with_grep_style_pattern_allows():
    # A grep-shaped read of the WORD "git" via re.search, same non-
    # invocation data-only shape as the reported incident.
    payload = _payload(
        'python3 -c "import re; print(bool(re.search(\'git\', open(\'f\').read())))"',
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_python_inline_c_destructive_payload_still_denies_after_catchall_fix():
    # AC6: the specific push --force free-text pattern (not the catchall
    # this fix narrows) still catches the memo's exact bypass example --
    # duplicate assertion of test_indirection_python_inline_c_destructive_
    # payload_denies, pinned again here directly alongside the fix.
    payload = _payload(
        "python3 -c 'import subprocess; subprocess.run([\"git\", \"push\", \"--force\"])'",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_python_inline_c_rebase_payload_still_denies():
    # AC6: `git rebase` free-text pattern still fires inside a -c payload.
    payload = _payload(
        "python3 -c \"import os; os.system('git rebase -i HEAD~3')\"",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_python_inline_c_reset_hard_payload_still_denies():
    # AC6: `git reset --hard` free-text pattern still fires inside a -c
    # payload.
    payload = _payload(
        "python3 -c \"import os; os.system('git reset --hard HEAD~1')\"",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_python_inline_c_stash_pop_payload_still_denies():
    # AC6: `git stash pop` free-text pattern still fires inside a -c
    # payload.
    payload = _payload(
        "python3 -c \"import os; os.system('git stash pop')\"",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_sh_c_unparseable_shell_segment_still_denies_via_catchall():
    # AC6: a genuinely unparseable REAL shell segment (unbalanced quote)
    # must keep the unchanged legacy catchall -- `strict=True`,
    # `subcmd is None` (not the interpreter-carve-out sentinel), so
    # `default_deny_on_unmatched` stays True. Uses a git-mentioning,
    # unterminated-quote segment to reach `_evaluate_git_segment_legacy`
    # via genuine unparseability rather than the -c carve-out.
    payload = _payload(
        "git frobnicate 'unterminated",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "default-deny" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_evaluate_git_segment_legacy_default_deny_on_unmatched_true_denies_directly():
    # Direct unit coverage: default (True) keeps the terminal catchall.
    assert (
        guard._evaluate_git_segment_legacy("some prose mentioning git only")
        == "unrecognized git verb (default-deny)"
    )


def test_evaluate_git_segment_legacy_default_deny_on_unmatched_false_allows_directly():
    # Direct unit coverage: False suppresses ONLY the terminal catchall.
    assert (
        guard._evaluate_git_segment_legacy(
            "some prose mentioning git only", default_deny_on_unmatched=False
        )
        is None
    )


def test_evaluate_git_segment_legacy_default_deny_on_unmatched_false_still_denies_push_force():
    # Direct unit coverage: False does NOT touch the specific push --force
    # pattern above the catchall.
    assert (
        guard._evaluate_git_segment_legacy(
            "subprocess.run(['git', 'push', '--force'])",
            default_deny_on_unmatched=False,
        )
        == "git push --force"
    )


def test_indirection_windows_bash_exe_backslash_path_inline_c_denies():
    # A2 fix: `C:\Windows\System32\bash.exe -c '<payload>'` -- the
    # memo's own explicit Windows-spelled bypass example. Before the fix,
    # `_evaluate_wrapper_indirection`'s raw `tokens[0].rsplit("/", 1)[-1]`
    # never stripped the `\`-separators or `.exe` suffix, so `head_base`
    # never equalled `bash` and the whole -c-indirection deny silently
    # never fired on Windows.
    payload = _payload(
        "C:\\Windows\\System32\\bash.exe -c 'git push origin main --force'",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "force" in reason.lower()


def test_indirection_windows_python_exe_backslash_path_inline_c_denies():
    # A2 fix: `C:\Python311\python.exe -c '<payload>'` -- second
    # Windows-spelled bypass example named in the finding.
    payload = _payload(
        "C:\\Python311\\python.exe -c \"import subprocess; "
        "subprocess.run(['git', 'push', 'origin', 'main', '--force'])\"",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None


def test_indirection_windows_bash_exe_backslash_path_benign_allows():
    # Same Windows-spelled interpreter head, benign payload -- must still
    # allow (the fix must not over-block ordinary Windows-spelled bash
    # invocations, only the destructive-indirection shapes).
    payload = _payload(
        'C:\\Windows\\System32\\bash.exe -c "echo hello world"',
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_normalize_windows_wrapper_argv0_rewrites_bash_and_python_directly():
    # Direct unit coverage of the widened pre-pass (git-only ->
    # git+bash+sh+zsh+python+python3+env+xargs).
    assert guard._normalize_windows_wrapper_argv0(
        "C:\\Windows\\System32\\bash.exe -c hi"
    ) == "C:/Windows/System32/bash.exe -c hi"
    assert guard._normalize_windows_wrapper_argv0(
        "C:\\Python311\\python.exe -c hi"
    ) == "C:/Python311/python.exe -c hi"
    # git behavior (the original, pre-widen case) is preserved.
    assert guard._normalize_windows_wrapper_argv0(
        "C:\\path\\to\\git.exe push"
    ) == "C:/path/to/git.exe push"
    # A backslash elsewhere in the command (not at argv0 position, not one
    # of the recognized basenames) is left untouched.
    assert guard._normalize_windows_wrapper_argv0(
        "echo C:\\notes\\bash.exe for details"
    ) == "echo C:\\notes\\bash.exe for details"


def test_indirection_xargs_with_visible_destructive_text_still_denies():
    # `echo scratch/one-file.txt | xargs rm -rf` -- xargs's OWN argument text
    # literally contains `rm -rf`, so this hits the DIRECT rm surface match
    # (visible in the command text, not hidden behind xargs's stdin
    # assembly) before the wrapper/indirection branch is ever reached --
    # stays hard deny, unaffected by the indirection-advisory reshape.
    payload = _payload("echo scratch/one-file.txt | xargs rm -rf", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "rm -r/-f" in hso["permissionDecisionReason"]


def test_indirection_xargs_benign_content_now_advises():
    # `xargs` genuinely assembles its command from stdin -- content this
    # guard cannot see. Opaque, so it now advises rather than denies.
    payload = _payload("echo hello | xargs echo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "xargs" in hso["additionalContext"]


def test_indirection_env_wrapped_c_destructive_payload_denies():
    # `env ... <cmd>` chained with a nested `-c` shape -- exercises the
    # env-strip -> interpreter -c -> recurse chain.
    payload = _payload(
        "env FOO=bar sh -c 'rm -rf /tmp/scratch'", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "rm -r/-f" in reason


def test_indirection_env_wrapped_benign_command_allows():
    # `env FOO=bar <benign>` -- reliably parseable, must not over-block.
    payload = _payload("env FOO=bar npm test", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_indirection_env_wrapped_benign_c_shape_allows():
    payload = _payload("env FOO=bar sh -c 'ls -la'", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_indirection_unparseable_wrapper_segment_advises():
    # A segment that matches the cheap wrapper probe but has unterminated
    # quoting cannot be tokenized by shlex -- an opaque shape, so this now
    # surfaces the advisory rather than failing closed as a hard deny.
    payload = _payload("bash -c 'echo unterminated", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unparseable indirection wrapper" in hso["additionalContext"]


def test_indirection_wrapper_no_agent_id_allows():
    # Top-level EM call -- no agent_id in payload at all. Indirection
    # hardening must not affect the EM main-loop.
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "bash repro.sh"},
        "session_id": "sess1",
    }
    assert guard.check(payload) is None


def test_indirection_wrapper_probe_does_not_over_block_plain_commands():
    # Sanity check the new Layer-1 wrapper probe doesn't widen the
    # already-covered benign-command fast path.
    payload = _payload("ls -la && grep foo bar.txt", agent_type="coordinator:executor")
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Semicolon/operator-in-quoted-value false positive (2026-07-26 fix). A
# `;`/`&&`/`||`/`|` INSIDE a quoted argument value (e.g. a cross-repo-memo
# `--title`) is not a real command separator -- the naive
# `re.split(r"[;&|\n]+", cmd_text)` this evaluator used to segment on split
# such a quoted operator anyway, breaking the segment into two bogus halves,
# one an unterminated-quote fragment that still matched the cheap
# `_WRAPPER_PROBE_RE` (via the `python3` interpreter token) and then failed
# `shlex.split`, tripping the fail-closed "unparseable indirection wrapper"
# path on an entirely ordinary quoted value. Reproduced live sending a
# cross-repo memo with a semicolon in `--title`. Fixed by segmenting via the
# same quote-aware `_tokenize_full_command`/`_segments_from_tokens` pair the
# tokenized authoritative pass already uses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operator", [";", "&&", "||", "|"])
def test_indirection_operator_inside_quoted_value_allows(operator):
    payload = _payload(
        'python3 coordinator/bin/cross-repo-memo.py --to sibling '
        f'--title "some title{operator} with an operator" --summary "s"',
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


@pytest.mark.parametrize(
    ("operator", "expected_fragment"),
    [
        (";", "bash <file>"),
        ("&&", "bash <file>"),
        ("||", "bash <file>"),
        ("|", "bash <file>"),
    ],
)
def test_indirection_operator_outside_quotes_still_advises(operator, expected_fragment):
    # The SAME operators, used as REAL command separators outside any quoted
    # value, must still surface the advisory -- the fix must not weaken
    # genuine-shape detection, only stop mis-segmenting quoted content.
    payload = _payload(f"echo hi {operator} bash malicious.sh", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert expected_fragment in hso["additionalContext"]


def test_indirection_semicolon_inside_quotes_directly_on_evaluator():
    # Direct entrypoint check (no identity-resolution plumbing) mirroring the
    # exact repro: a cross-repo-memo invocation with a semicolon inside a
    # quoted --title value must classify as non-wrapper.
    cmd = (
        'python3 coordinator/bin/cross-repo-memo.py --to sibling '
        '--title "some title; with a semicolon" --summary "s"'
    )
    assert guard._evaluate_wrapper_indirection(cmd) is None


def test_indirection_prose_fix_is_flag_name_agnostic():
    # cross-repo/archive/2026-07-26-project-opticon-em-guard-title-false-
    # positive-and-validator-rehoming.md, Finding 1: the fix landed in
    # 8fb0c481 is a general quote-aware SEGMENTATION fix, not a `--title`
    # allowlist entry -- it must equally clear a trigger word/operator
    # sitting in ANY quoted option value, on ANY CLI, `--title` included but
    # not special-cased. `bash` (the reporter's literal trigger word) and an
    # operator both sit inside quoted prose here, on three different flag
    # names across two different CLIs.
    cases = [
        'python3 coordinator/bin/cross-repo-memo.py --to sibling --title x '
        '--summary "finished the bash migration; no more shell scripts"',
        'python3 coordinator/bin/cross-repo-memo.py --to sibling --body-file - '
        '--title "note: bash && migration done"',
        'some-other-cli --note "the bash || sh migration is done" --run',
    ]
    for cmd in cases:
        assert guard._evaluate_wrapper_indirection(cmd) is None, cmd


def test_indirection_prose_trigger_word_does_not_mask_real_indirection_elsewhere():
    # The false-positive fix must not go the other way: a subagent-crafted
    # command may legitimately deny for a REAL `bash <file>` indirection
    # while a --title in the SAME command also happens to mention "bash" (and
    # a semicolon) in prose -- the prose match must not short-circuit or
    # otherwise suppress the genuine deny.
    cmd = (
        "echo start ; bash malicious.sh ; python3 coordinator/bin/cross-repo-memo.py "
        '--to sibling --title "finished the bash migration; no more shell scripts" '
        "--kind fyi"
    )
    verdict = guard._evaluate_wrapper_indirection(cmd)
    assert verdict is not None
    assert "bash <file>" in verdict

    payload = _payload(cmd, agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "bash <file>" in hso["additionalContext"]


# ---------------------------------------------------------------------------
# Boundary-anchor widen + tokenized-path fixes (2026-07-21 code review,
# Findings 1-4). Each case reproduced a real bypass on pre-fix disk (verified
# independently against current HEAD before landing, per review-integrator
# discipline) -- a quoted interpreter/verb name, command substitution /
# backtick / subshell-paren wrapping, a bundled `-ic` short flag, and a
# versioned `python3.NN` binary each slipped past Layer 1 with zero
# identity-resolution cost.
# ---------------------------------------------------------------------------


def test_quoted_interpreter_name_advises():
    # Finding 1: a quoted bare word is a shell no-op -- `'bash' repro.sh`
    # runs identically to `bash repro.sh` -- but the ORIGINAL boundary class
    # (`[;&|\s]` only) did not treat a quote character as a boundary, so
    # Layer 1 never fired and this allowed outright. Now that the resolved
    # shape (opaque `<file>` indirection) surfaces, it advises, not denies.
    payload = _payload("'bash' repro.sh", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "indirection wrapper" in hso["additionalContext"]


def test_quoted_git_verb_denies():
    # Finding 1's git-surface variant: `'git' push --force` bypassed the
    # ORIGINAL `_GIT_SURFACE_RE` Layer-1 gate entirely (quote not a boundary
    # char), so `check()` returned allow before identity resolution ever ran.
    payload = _payload("'git' push --force origin work/foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_command_substitution_git_push_force_advises():
    # Finding 2: `$(...)` command substitution runs its inner command for
    # the side effect regardless of what the captured stdout is subsequently
    # used for -- `(`/`` ` `` were not boundary chars in the ORIGINAL class,
    # so `$(git push --force)` bypassed Layer 1 with no identity check. Layer
    # 1 still catches this, but the tokenizer resolves `$(git` at argv0
    # position as an unresolved `$(...)` reference (not a stripped-subshell
    # `git` token) -- an OPAQUE indirection shape, so this now advises
    # rather than denies (RESHAPE, C18d). Unchanged from before this
    # reshape: still not a silent allow.
    payload = _payload(
        "$(git push --force origin work/foo)", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_backtick_substitution_rm_denies():
    # Finding 2's backtick variant: `` `rm -rf /tmp/x` ``.
    payload = _payload("`rm -rf /tmp/x`", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "rm -r/-f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_subshell_paren_rm_denies():
    # Finding 2's bare-subshell variant: `(rm -rf /tmp/scratch)`.
    payload = _payload("(rm -rf /tmp/scratch)", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "rm -r/-f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_python_bundled_c_flag_denies():
    # Finding 3: Python's CLI parser accepts bundled short flags --
    # `python3 -ic '<payload>'` behaves as `python3 -i -c '<payload>'` --
    # so an exact `tokens[1] == "-c"` check missed this shape entirely and
    # the destructive payload was never inspected. (The reason text may
    # come from either the direct rm-surface probe or the `-c` unwrap path
    # -- both now widened/fixed in this pass and legitimately overlap on
    # this shape; the load-bearing assertion is deny, not which path fired.)
    payload = _payload("python3 -ic 'rm -rf /'", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "rm -r/-f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_bundled_c_flag_detected_by_wrapper_evaluator_directly():
    # White-box isolation of Finding 3's fix, independent of the Finding 1/2
    # boundary widen (which can also catch a quote-adjacent destructive verb
    # via the direct surface probes and would otherwise mask whether the
    # `-c` bundled-flag scan itself is doing the work): a segment whose
    # destructive verb is UNBOUNDED-BY-A-SEEN-SURFACE-PROBE gets routed
    # correctly through `_unwrap_and_classify` only because
    # `_BUNDLED_C_FLAG_RE` recognizes `-ic` as carrying `-c`.
    verdict = guard._evaluate_wrapper_indirection("python3 -ic 'rm -rf /'", 0)
    assert verdict is not None
    assert "rm -r/-f" in verdict


def test_versioned_python_interpreter_denies():
    # Finding 4: `python3.11`/`python3.12` etc. were invisible to the
    # ORIGINAL `_WRAPPER_PROBE_RE` (`python3?` only matches literal `python`
    # or `python3`), so Layer 1 never fired for a versioned interpreter.
    payload = _payload("python3.11 -c 'rm -rf /'", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "rm -r/-f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_versioned_python_interpreter_normalized_by_wrapper_evaluator_directly():
    # White-box isolation of Finding 4's fix: `_normalize_interpreter_basename`
    # must strip the `.11` suffix so `python3.11` is treated identically to
    # `python3` for the `_C_FLAG_INTERPRETERS` membership check.
    assert guard._normalize_interpreter_basename("python3.11") == "python3"
    assert guard._normalize_interpreter_basename("python3.12.1") == "python3"
    assert guard._normalize_interpreter_basename("python3") == "python3"
    assert guard._normalize_interpreter_basename("bash") == "bash"


# ---------------------------------------------------------------------------
# 2026-07-21 security-audit hardening -- deeper bypass class (findings
# #1-#6). Root cause: the git/rm/chmod/chown verb-identification used
# raw-substring regex matching over the command TEXT -- any trick that stops
# the destructive verb's literal characters from appearing CONTIGUOUS
# defeats it. Fixed by tokenizing with `shlex` and matching the resulting
# argv[0] TOKEN, not a raw-string regex substring (`_evaluate_tokenized`).
# ---------------------------------------------------------------------------


def test_finding1_quote_split_rm_denies():
    payload = _payload("r''m -rf /tmp/foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "rm -r/-f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_finding1_quote_split_git_denies():
    payload = _payload("g''it push --force", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_finding1_quote_split_chmod_denies():
    payload = _payload("ch\"m\"od -R 777 /tmp/foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "chmod/chown -R" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_finding1_quote_split_chown_denies():
    payload = _payload("ch''own -R x /tmp/foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "chmod/chown -R" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_finding2_backslash_split_rm_denies():
    payload = _payload("r\\m -rf /tmp/foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "rm -r/-f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_finding3_variable_indirection_advises():
    # An unresolved $VAR referenced value is genuinely unresolvable (not a
    # recursively-classified real match) -- opaque, so it advises.
    payload = _payload("V=rm; $V -rf /tmp/foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    context = hso["additionalContext"]
    assert "unresolved" in context
    assert "indirection" in context


def test_finding4_bare_pipe_fed_interpreter_advises():
    # Piped content is never present in the command text -- opaque, so this
    # advises rather than denies.
    payload = _payload(
        "echo aGk= | base64 -d | bash", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    context = hso["additionalContext"]
    assert "bash" in context
    assert "indirection wrapper" in context


def test_finding5_dot_source_advises():
    # `.`/`source` executes a script in-process with content this guard
    # never sees -- opaque, so this advises rather than denies.
    payload = _payload(". /tmp/payload.sh", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "indirection wrapper" in hso["additionalContext"]


def test_finding5_source_keyword_advises():
    # `source` (the long spelling of `.`) executes a script in-process with
    # content this guard never sees -- opaque, now advises rather than
    # denies.
    payload = _payload("source /tmp/payload.sh", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "indirection wrapper" in hso["additionalContext"]


def test_finding6_eval_unresolved_var_advises():
    # An unresolved `eval $X` operand is genuinely unresolvable -- opaque,
    # so it advises rather than denies.
    payload = _payload("eval $X", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_finding6_eval_command_substitution_advises():
    payload = _payload(
        'eval "$(echo rm) -rf /tmp"', agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_finding6_eval_literal_destructive_payload_denies():
    payload = _payload("eval rm -rf /tmp/scratch", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "rm -r/-f" in result["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# Benign-regression coverage for the tokenized authoritative pass -- must
# NOT over-block ordinary subagent Bash.
# ---------------------------------------------------------------------------


def test_tokenized_pass_python3_module_invocation_allows():
    payload = _payload("python3 -m pytest", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_tokenized_pass_python3_script_invocation_allows():
    payload = _payload("python3 script.py --flag", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_tokenized_pass_git_status_allows():
    payload = _payload("git status", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_tokenized_pass_git_log_allows():
    payload = _payload("git log", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_tokenized_pass_chmod_plus_x_allows():
    payload = _payload("chmod +x file", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_tokenized_pass_sh_c_benign_allows():
    payload = _payload("sh -c 'echo hello'", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_tokenized_pass_env_wrapped_benign_allows():
    payload = _payload("env FOO=bar npm test", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_tokenized_pass_ls_allows():
    payload = _payload("ls", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_tokenized_pass_quoted_apostrophe_in_double_quotes_allows():
    # A benign command with a literal apostrophe safely nested inside double
    # quotes must parse cleanly and not trip the fail-closed unparseable path.
    payload = _payload(
        'git commit -m "fix: don\'t break" -- file.txt',
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is None


def test_tokenized_pass_quoted_semicolon_in_argument_allows():
    # A quoted `;` inside an argument must stay attached to its word and not
    # be misread as a command separator that breaks tokenization.
    payload = _payload("echo 'a;b'", agent_type="coordinator:executor")
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Heredoc-body false-positive fix (2026-07-23). A heredoc BODY is stdin DATA,
# never shell command tokens -- prose inside a benign file-write heredoc that
# happens to contain the bare word "git"/"rm"/"chmod" must not be classified
# as a destructive command. See `_strip_heredoc_bodies` docstring.
# ---------------------------------------------------------------------------


def test_heredoc_benign_prose_containing_git_word_allows():
    command = "cat > notes.md <<'EOF'\nthe word git in prose\nEOF"
    payload = _payload(command, agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_heredoc_benign_prose_containing_rm_and_chmod_words_allows():
    command = "cat > notes.md <<'EOF'\nrun rm -rf and chmod -R as an example\nEOF"
    payload = _payload(command, agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_heredoc_interpreter_fed_wrapper_still_surfaces():
    # Anti-bypass: an interpreter FED by a heredoc must still surface --
    # after the body is stripped, the residual `bash <<'EOF'` still matches
    # the wrapper probe and is an interpreter-invoked-script indirection
    # wrapper -- opaque (script content unexamined), so this now advises
    # rather than denies (RESHAPE, C18d), but it is never a silent allow.
    command = "bash <<'EOF'\ngit push --force origin work/foo\nEOF"
    payload = _payload(command, agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "additionalContext" in result["hookSpecificOutput"]


def test_strip_heredoc_bodies_removes_body_keeps_command_line():
    cmd = "cat > f.md <<'EOF'\nthe word git in prose\nEOF"
    stripped = guard._strip_heredoc_bodies(cmd)
    assert stripped == "cat > f.md <<'EOF'"
    assert "git" not in stripped


# ---------------------------------------------------------------------------
# `<<\EOF`-style backslash-escaped heredoc delimiter (2026-07-29 review
# finding). `<<\EOF` is the standard POSIX spelling for a non-expanding
# heredoc, equivalent in effect to `<<'EOF'` -- `_HEREDOC_OP_RE` previously
# had no branch for the leading `\` and silently failed to match this
# spelling at all, so `_strip_heredoc_bodies` left such a heredoc's body
# untouched and it was rescanned as live command text. Same false-deny class
# `test_heredoc_benign_prose_containing_git_word_allows` pins for `<<'EOF'`,
# just for the untested backslash spelling.
# ---------------------------------------------------------------------------


def test_heredoc_backslash_delimiter_benign_prose_allows():
    command = "cat > notes.md <<\\EOF\nthe word git in prose\nEOF"
    payload = _payload(command, agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_heredoc_backslash_delimiter_review_doc_quoting_worktree_add_allows():
    # The false-deny fix case named by the review finding: a `cat <<\EOF`
    # write whose body quotes worktree/destructive text as prose must not
    # deny.
    command = (
        "cat <<\\EOF > /tmp/review.md\n"
        "Discussion of git worktree add ../wt-1 x and rm -rf as examples.\n"
        "EOF"
    )
    payload = _payload(command, agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_strip_heredoc_bodies_removes_backslash_delimited_body():
    cmd = "cat > f.md <<\\EOF\nthe word git in prose\nEOF"
    stripped = guard._strip_heredoc_bodies(cmd)
    assert stripped == "cat > f.md <<\\EOF"
    assert "git" not in stripped


def test_heredoc_backslash_delimiter_interpreter_fed_wrapper_still_surfaces():
    # Differential: widening `_HEREDOC_OP_RE` to recognize `<<\EOF` means the
    # body of `bash <<\EOF ... EOF` is now stripped too (previously it
    # wasn't -- the destructive verb inside was visible as raw, unstripped
    # text and denied that way instead). Verdict must stay non-silent either
    # way: after stripping, the residual `bash <<\EOF` line still has >=2
    # tokens and still matches the interpreter-invoked-script wrapper probe
    # (`_SHELL_FILE_INTERPRETERS` branch), independent of heredoc delimiter
    # spelling. Opaque shape -- advises (RESHAPE, C18d), not a deny, but
    # still never a silent allow. Mirrors
    # `test_heredoc_interpreter_fed_wrapper_still_surfaces` for the
    # `<<'EOF'` spelling -- this is the genuine interpreter-fed-by-heredoc
    # shape (the interpreter itself executes the body), not the
    # `test_real_invocation_preceding_unrelated_heredoc_still_denies` shape
    # in the sibling worktree-guard test file.
    command = "bash <<\\EOF\ngit push --force origin work/foo\nEOF"
    payload = _payload(command, agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "additionalContext" in result["hookSpecificOutput"]


# ---------------------------------------------------------------------------
# Verb-anchored classification (2026-07-25 false-positive fix). The git
# subcommand classifier used to `\bword\b`-search the whole segment text, so
# a mention of a verb inside a quoted argument, or a hyphenated sibling
# subcommand, was indistinguishable from the verb actually being invoked.
# Two confirmed false positives (both previously DENIED as "git merge (not
# --ff-only)"): `git grep <pattern containing "merge">` and
# `git merge-base --is-ancestor A B`. Fixed by anchoring classification on
# the real argv subcommand (`_git_subcommand_for_segment` /
# `_evaluate_git_segment_anchored`), not a free-text search.
# ---------------------------------------------------------------------------


def test_git_grep_hyphenated_merge_word_in_pattern_allows():
    # False positive (a): the search pattern merely CONTAINS the word
    # "merge" (hyphen-bounded) -- this is `git grep`, not `git merge`.
    payload = _payload(
        'git grep -n "some-hyphenated-merge-token"', agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_git_merge_base_is_ancestor_allows():
    # False positive (b): `merge-base` is a distinct read-only subcommand --
    # the hyphen is a word boundary, so `\bmerge\b` used to match inside it.
    payload = _payload(
        "git merge-base --is-ancestor A B", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_git_merge_still_denies():
    payload = _payload("git merge foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git merge (not --ff-only)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_merge_ff_only_still_allows():
    payload = _payload("git merge --ff-only foo", agent_type="coordinator:executor")
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# `--no-optional-locks` enumeration (C2, docs/plans/2026-08-11-pytest-grant-
# and-working-interpreter-disjoint.md). Before this fix, the flag was absent
# from `_GIT_GLOBAL_OPT_NO_ARG`, so the anchored parser bailed on the
# unrecognized flag and handed the segment to the legacy classifier, where
# `_MERGE_WORD_RE` matched inside `merge-base`.
# ---------------------------------------------------------------------------


def test_git_no_optional_locks_merge_base_allows():
    # AC4: the flag is now enumerated, so the anchored parser never bails
    # and `merge-base` reaches its correct exact-token, read-only handling.
    payload = _payload(
        "git --no-optional-locks merge-base HEAD origin/main",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_git_no_optional_locks_merge_no_ff_still_denies():
    # AC5: the flag must not become a bypass for a real merge -- same deny
    # reason as the unflagged form.
    payload = _payload(
        "git --no-optional-locks merge --no-ff feat", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git merge (not --ff-only)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_no_optional_locks_merge_ff_only_allows():
    # AC5: the flagged --ff-only form behaves like the unflagged one.
    payload = _payload(
        "git --no-optional-locks merge --ff-only feat", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_merge_word_re_pattern_unchanged():
    # AC6: inert-probe guard against a future well-meaning narrowing of the
    # legacy classifier's merge regex -- see the plan's "one real judgment
    # call" section. This assertion must never be "fixed" by editing the
    # regex; it exists to catch exactly that.
    assert guard._MERGE_WORD_RE.pattern == r"\bmerge\b"


def test_git_pull_still_denies():
    payload = _payload("git pull", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git pull (not --ff-only)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_pull_ff_only_still_allows():
    payload = _payload("git pull --ff-only", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_commit_message_mentioning_merge_allows_and_not_classified_as_merge():
    payload = _payload(
        'git commit -m "merge the configs"', agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is None


def test_git_global_option_c_path_merge_still_denies():
    # Global options (`-C <path>`) must be skipped correctly when resolving
    # the real subcommand -- `merge` here is still the real subcommand.
    payload = _payload(
        "git -C /some/path merge foo", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git merge (not --ff-only)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_unknown_verb_still_default_denies():
    payload = _payload("git frobnicate", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "default-deny" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_unparseable_segment_does_not_become_allowed():
    # Unbalanced quoting cannot be shlex-tokenized -- falls back to the
    # legacy free-text classifier, which still denies a genuinely
    # destructive `push --force`, rather than the parse failure silently
    # becoming an allow.
    payload = _payload(
        "git push --force 'unterminated", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Legacy-path worktree/remote default-deny (2026-07-25 fix, P2(b)). These
# force routing to `_evaluate_git_segment_legacy` via the SAME unenumerated
# `--namespace` global-flag ambiguity used above, so the ANCHORED classifier
# never runs -- exercising the free-text next-word check
# (`_NEXT_WORD_AFTER_RE` gated on `_LEGACY_WORKTREE_READONLY`/
# `_LEGACY_REMOTE_READONLY`) that replaced legacy's former allow-by-default
# on any unenumerated worktree/remote second-level verb.
# ---------------------------------------------------------------------------


def test_legacy_worktree_unrecognized_subcommand_denies():
    payload = _payload(
        "git --namespace worktree futuristic-verb", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "default-deny" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_legacy_worktree_list_allows():
    payload = _payload(
        "git --namespace worktree list", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_legacy_worktree_bare_allows():
    payload = _payload("git --namespace worktree", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_legacy_worktree_remove_denies():
    # Already-known mutating form -- must still deny via `_WORKTREE_MUTATE_RE`,
    # unchanged by the P2(b) fix.
    payload = _payload(
        "git --namespace worktree remove /p", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_legacy_remote_unrecognized_subcommand_denies():
    payload = _payload(
        "git --namespace remote futuristic-verb", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "default-deny" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_legacy_remote_v_allows():
    payload = _payload("git --namespace remote -v", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_legacy_remote_show_allows():
    payload = _payload(
        "git --namespace remote show origin", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_legacy_remote_get_url_allows():
    payload = _payload(
        "git --namespace remote get-url origin", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_legacy_remote_bare_allows():
    payload = _payload("git --namespace remote", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_legacy_remote_set_url_denies():
    # Already-known mutating form -- must still deny via `_REMOTE_MUTATE_RE`,
    # unchanged by the P2(b) fix.
    payload = _payload(
        "git --namespace remote set-url origin http://evil.example",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_git_ls_remote_allows():
    payload = _payload("git ls-remote origin", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_blame_allows():
    payload = _payload("git blame file.py", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_shortlog_allows():
    payload = _payload("git shortlog", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_cat_file_allows():
    payload = _payload("git cat-file -p HEAD", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_for_each_ref_allows():
    payload = _payload("git for-each-ref", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_symbolic_ref_repoint_head_denies():
    # `symbolic-ref` is NOT in the safe-forward allowlist -- it can repoint
    # HEAD (`git symbolic-ref HEAD refs/heads/evil`), so it must default-deny
    # like any other unenumerated verb, not allow.
    payload = _payload(
        "git symbolic-ref HEAD refs/heads/evil", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "default-deny" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_symbolic_ref_delete_denies():
    # `git symbolic-ref --delete <ref>` deletes a symbolic ref -- also
    # default-denied, not allowlisted.
    payload = _payload(
        "git symbolic-ref --delete HEAD", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "default-deny" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_name_rev_allows():
    payload = _payload("git name-rev HEAD", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_check_ignore_allows():
    payload = _payload("git check-ignore foo.txt", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_subcommand_for_segment_skips_global_options():
    subcmd, parse_ok = guard._git_subcommand_for_segment(
        "git -C /some/path -c user.name=x merge foo"
    )
    assert parse_ok is True
    assert subcmd == "merge"


def test_git_subcommand_for_segment_unparseable_fails_closed():
    subcmd, parse_ok = guard._git_subcommand_for_segment("git push --force 'unterminated")
    assert parse_ok is False


# ---------------------------------------------------------------------------
# P0 security-regression fix (2026-07-25 review): `_real_git_subcommand`
# used to treat ANY unrecognized `--xxx` token as taking NO argument, so an
# unenumerated space-separated global option (`--namespace <path>`,
# `--super-prefix <path>`, `--config-env <name>=<var>`, `--exec-path <path>`)
# caused the flag's own VALUE token to be misresolved as the subcommand,
# leaving the REAL subcommand (and any destructive flags after it, e.g.
# `push --force`) never inspected. Fixed by treating any unrecognized
# `-`/`--`-prefixed token (that isn't a self-contained `--foo=value` form)
# as "argument shape uncertain" and failing the segment over to the legacy
# free-text classifier, which correctly denies these on plain word/flag
# presence regardless of argv position.
# ---------------------------------------------------------------------------


def test_git_namespace_flag_bypass_denies():
    payload = _payload(
        "git --namespace status push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_git_super_prefix_flag_bypass_denies():
    payload = _payload(
        "git --super-prefix status push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_git_config_env_flag_bypass_denies():
    payload = _payload(
        "git --config-env status push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_git_exec_path_flag_bypass_denies():
    payload = _payload(
        "git --exec-path status push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_git_subcommand_for_segment_unrecognized_global_option_fails_over():
    # An unenumerated `--xxx` token (argument shape unknown) must fail the
    # segment over to the legacy classifier -- never guess "no argument".
    subcmd, parse_ok = guard._git_subcommand_for_segment(
        "git --namespace status push --force"
    )
    assert parse_ok is False


def test_git_known_no_arg_global_flags_still_resolve_subcommand():
    # The small explicit no-argument allowlist must still resolve the real
    # subcommand correctly (not a regression of the P0 fix).
    subcmd, parse_ok = guard._git_subcommand_for_segment(
        "git --no-pager --bare --literal-pathspecs --paginate -P status"
    )
    assert parse_ok is True
    assert subcmd == "status"


# ---------------------------------------------------------------------------
# `worktree`/`remote` second-level subcommand classification (2026-07-25
# fix). Both verbs sat in the safe sets (`_SAFE_GIT_SUBCOMMANDS` and the
# legacy `_SAFE_VERB_RE`) as BARE VERBS with no inspection of the
# second-level subcommand, so every mutating form allowed uninspected.
# ---------------------------------------------------------------------------


def test_git_worktree_remove_denies():
    payload = _payload("git worktree remove /tmp/foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git worktree remove" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_worktree_prune_denies():
    payload = _payload("git worktree prune", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git worktree prune" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_worktree_move_denies():
    payload = _payload("git worktree move A B", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git worktree move" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_worktree_add_denies():
    payload = _payload("git worktree add /tmp/wt branch", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git worktree add" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_worktree_repair_denies():
    payload = _payload("git worktree repair", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None


def test_git_worktree_unlock_denies():
    payload = _payload("git worktree unlock /tmp/wt", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None


def test_git_worktree_lock_denies():
    payload = _payload("git worktree lock /tmp/wt", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None


def test_git_worktree_list_allows():
    payload = _payload("git worktree list", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_worktree_bare_allows():
    payload = _payload("git worktree", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_worktree_unrecognized_subcommand_denies():
    payload = _payload("git worktree bogus-subcmd", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "default-deny" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_remote_remove_denies():
    payload = _payload("git remote remove origin", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git remote remove" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_remote_set_url_denies():
    payload = _payload(
        "git remote set-url origin http://evil.example", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git remote set-url" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_remote_rename_denies():
    payload = _payload("git remote rename a b", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git remote rename" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_remote_prune_denies():
    payload = _payload("git remote prune origin", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git remote prune" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_remote_rm_denies():
    payload = _payload("git remote rm origin", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None


def test_git_remote_set_head_denies():
    payload = _payload("git remote set-head origin -a", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None


def test_git_remote_set_branches_denies():
    payload = _payload(
        "git remote set-branches origin main", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None


def test_git_remote_update_denies():
    payload = _payload("git remote update", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None


def test_git_remote_v_allows():
    payload = _payload("git remote -v", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_remote_show_allows():
    payload = _payload("git remote show origin", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_remote_get_url_allows():
    payload = _payload("git remote get-url origin", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_remote_bare_allows():
    payload = _payload("git remote", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_remote_unrecognized_subcommand_denies():
    payload = _payload("git remote bogus-subcmd", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "default-deny" in result["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# Invocation-head normalization (2026-07-25 fix). `_GIT_SURFACE_RE` and
# `_evaluate_tokenized`'s `norm_head == "git"` check only recognized an
# exact `git` basename, so `git.exe`, a Windows absolute path, and a
# forward-slash-prefixed path all needed checking; the forward-slash forms
# already worked via `.rsplit("/", 1)`, the `.exe`/backslash forms did not.
# ---------------------------------------------------------------------------


def test_git_exe_bare_push_force_denies():
    payload = _payload("git.exe push --force", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_exe_windows_absolute_path_push_force_denies():
    payload = _payload(
        "C:\\path\\to\\git.exe push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_usr_bin_git_push_force_denies():
    payload = _payload("/usr/bin/git push --force", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_dot_slash_git_push_force_denies():
    payload = _payload("./git push --force", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_gitk_is_not_treated_as_git():
    # Negative case: `gitk` must NOT normalize to `git` -- substring
    # matching, not exact-basename matching, would be the exact bug class
    # this fix exists to avoid reintroducing.
    payload = _payload("gitk", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_foo_is_not_treated_as_git():
    payload = _payload("git-foo status", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_mygit_is_not_treated_as_git():
    payload = _payload("mygit push --force", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_gitk_with_path_prefix_is_not_treated_as_git():
    # `/usr/bin/gitk`'s basename is `gitk`, not `git` -- exact-basename
    # identity, never substring, must hold regardless of the leading path.
    payload = _payload("/usr/bin/gitk push --force", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_gitk_bare_invocation_is_not_treated_as_git():
    payload = _payload("gitk --all", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_normalize_executable_basename_exe_and_backslash_stripping_directly():
    assert guard._normalize_executable_basename("git.exe") == "git"
    # Review: code-reviewer -- Finding 1 (P1, 2026-07-28): the basename is
    # now fully case-folded (not just the `.exe` suffix), since Windows
    # PATH/cmd.exe resolution is case-insensitive and every downstream
    # identity/membership check compares against a lowercase literal.
    assert guard._normalize_executable_basename("GIT.EXE") == "git"
    assert guard._normalize_executable_basename("Git.exe") == "git"
    assert guard._normalize_executable_basename("gIt") == "git"
    assert guard._normalize_executable_basename("BASH.EXE") == "bash"
    assert guard._normalize_executable_basename("C:\\path\\to\\git.exe") == "git"
    assert guard._normalize_executable_basename("/usr/bin/git") == "git"
    assert guard._normalize_executable_basename("./git") == "git"
    # Case-folding must not make matching sloppy in the allow direction --
    # negative controls stay distinct from "git" regardless of case.
    assert guard._normalize_executable_basename("gitk") == "gitk"
    assert guard._normalize_executable_basename("GITK") == "gitk"
    assert guard._normalize_executable_basename("git-foo") == "git-foo"
    assert guard._normalize_executable_basename("legit") == "legit"
    # Trailing separator noise (the escaped-space artifact ahead of
    # `_normalize_windows_wrapper_argv0`'s rewrite) must not collapse the
    # basename to '' -- Bug 1 fix, 2026-07-25.
    assert guard._normalize_executable_basename("C:\\path\\to\\git.exe\\") == "git"
    assert guard._normalize_executable_basename("mygit") == "mygit"
    assert guard._normalize_executable_basename("mygit.exe") == "mygit"


def test_tokenized_reconstruction_apostrophe_commit_message_with_rebase_word_allows():
    # P3 fix: _evaluate_tokenized used to rebuild a segment via a bare
    # " ".join() with no re-quoting before handing it to the shlex-based
    # subcommand resolver. A commit message containing an apostrophe broke
    # that re-parse (unterminated quote), which fell through to the LEGACY
    # free-text classifier operating on the *mangled* reconstruction text --
    # and legacy's unconditional `\brebase\b` word-search would then
    # false-positive-deny on the word "rebase" merely mentioned inside the
    # commit message, even though the real subcommand is `commit`. Fixed by
    # reconstructing with shlex.quote per token so the round-trip is
    # lossless.
    payload = _payload(
        "git commit -m \"don't forget to rebase later\"",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# P1 adversarial-shape regression tests (2026-07-25 review-integration).
# Locks in behavior empirically confirmed via `guard.check()` -- the module's
# authoritative entry point. `_evaluate_git_segment`/`_git_subcommand_for_segment`
# are NOT valid oracles for these shapes: a synthetic segment fed straight to
# the inner classifier skips Layer 1 surface-gating and identity resolution
# entirely, so it can report a false ALLOW or DENY that `check()` itself does
# not produce (this is exactly how an earlier verification attempt at this
# same finding went wrong before landing on this harness).
# ---------------------------------------------------------------------------


def test_windows_git_exe_quoted_double_with_space_denies():
    payload = _payload(
        '"C:\\Program Files\\Git\\bin\\git.exe" push --force',
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_quoted_single_with_space_denies():
    payload = _payload(
        "'C:\\Program Files\\Git\\bin\\git.exe' push --force",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_unc_path_denies():
    payload = _payload(
        "\\\\server\\share\\git.exe push --force",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_doubled_backslashes_denies():
    payload = _payload(
        "C:\\path\\\\to\\\\git.exe push --force",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_quoted_worktree_remove_denies():
    payload = _payload(
        '"C:\\Program Files\\Git\\bin\\git.exe" worktree remove /p',
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git worktree remove" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_mention_in_commit_message_allows():
    # A mention of a Windows git.exe path inside a commit message string is
    # data, not an invocation -- must not be classified as git at all.
    payload = _payload(
        'git commit -m "see C:\\notes\\git.exe for details"',
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_windows_git_exe_trailing_backslash_before_space_denies():
    # Fixed 2026-07-25: a Windows git.exe path whose argv0 token ends in a
    # backslash immediately before the separating space
    # (`C:\path\to\git.exe\ push --force`) used to normalize its basename to
    # '' (not "git") because the trailing backslash left nothing after the
    # final `rsplit("\\", 1)`, so `_normalize_windows_wrapper_argv0`'s pre-pass
    # never rewrote it and `shlex.split` went on to merge argv0 with the next
    # word. `_normalize_executable_basename` now strips trailing `/`/`\`
    # separator noise before splitting, so the basename resolves to `git`,
    # the pre-pass rewrites the backslashes, and the trailing separator no
    # longer swallows the following space.
    payload = _payload(
        "C:\\path\\to\\git.exe\\ push --force",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# 2026-07-28 P0 fix -- SPACED-WINDOWS-PATH ARGV0 HOLE. `C:\Program
# Files\Git\bin\git.exe` (git-for-Windows' DEFAULT install location) evaded
# every check above because `_normalize_windows_wrapper_argv0`'s raw-token
# capture (`_RAW_HEAD_TOKEN_RE`) stopped at the first whitespace inside the
# UNQUOTED path, so the path was only ever partially rewritten and
# downstream `shlex.split` either mangled it (backslash form) or split it
# on the embedded space (forward-slash form) -- landing "git"/"bash" one
# argv position too late for this module's position-anchored argv0 checks.
# `_normalize_windows_argv0_head_path_with_spaces` closes this by quoting
# an unquoted, whitespace-containing argv0-head path before `shlex` ever
# runs. Covers both separator forms; DENY-regression coverage for the
# already-quoted/no-space/wrapper-indirection shapes lives in the tests
# above/below and is deliberately not duplicated here.
# ---------------------------------------------------------------------------


def test_windows_git_exe_spaced_path_backslash_rebase_denies():
    # The exact hole from the dispatch brief: unquoted, backslash-separated,
    # git-for-Windows' default install location.
    payload = _payload(
        "C:\\Program Files\\Git\\bin\\git.exe rebase -i HEAD~3",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git rebase" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_spaced_path_forward_slash_rebase_denies():
    # Same hole, forward-slash separator form -- both leaked per the brief.
    payload = _payload(
        "C:/Program Files/Git/bin/git.exe rebase -i HEAD~3",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git rebase" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_spaced_path_backslash_push_force_denies():
    payload = _payload(
        "C:\\Program Files\\Git\\bin\\git.exe push --force origin x",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_spaced_path_forward_slash_push_force_denies():
    payload = _payload(
        "C:/Program Files/Git/bin/git.exe push --force origin x",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_bash_exe_spaced_path_inline_c_denies():
    # Wrapper-indirection path, spaced argv0 head -- must stay DENY (this
    # shape was already reported working pre-fix; regression-locked here so
    # a future change to the new spaced-path normalizer cannot silently
    # re-open it).
    payload = _payload(
        'C:\\Program Files\\Git\\bin\\bash.exe -c "git push origin main --force"',
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None


def test_windows_bash_exe_unspaced_path_inline_c_denies():
    # Same wrapper shape, unspaced argv0 head (System32 default install
    # location) -- both must deny identically.
    payload = _payload(
        'C:\\Windows\\System32\\bash.exe -c "git push origin main --force"',
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None


def test_windows_git_exe_spaced_path_status_allows():
    # Negative control: a benign command through the SAME spaced path must
    # still allow -- the spaced-path handling must not make matching sloppy.
    payload = _payload(
        "C:\\Program Files\\Git\\bin\\git.exe status",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_windows_gitk_exe_spaced_path_allows():
    # Negative control: a lookalike binary (`gitk`, not `git`) through the
    # same spaced path must not be treated as git.
    payload = _payload(
        "C:\\Program Files\\Git\\bin\\gitk.exe --all",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_windows_git_foo_spaced_path_allows():
    # Negative control: a hyphenated lookalike (`git-foo`, not `git`)
    # through the same spaced path must not be treated as git.
    payload = _payload(
        "C:\\Program Files\\Git\\bin\\git-foo.exe status",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_windows_legit_spaced_path_allows():
    # Negative control: `legit.exe` through the same spaced path must not
    # be treated as git (substring-of-"git" lookalike).
    payload = _payload(
        "C:\\Program Files\\Some Tool\\legit.exe status",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Spaced UNC path (2026-07-28 review, Finding 4, P2): a UNC path opens with
# TWO leading backslashes, not one -- the original spaced-path regex anchor
# only accepted a single leading separator (or a drive letter + one
# separator), so a spaced UNC path fell through both passes unrewritten and
# still evaded detection. See `_WINDOWS_ARGV0_HEAD_PATH_RE`'s own comment
# for the anchor-widening fix.
# ---------------------------------------------------------------------------


def test_windows_unc_spaced_path_backslash_rebase_denies():
    payload = _payload(
        "\\\\server\\share\\Program Files\\Git\\bin\\git.exe rebase -i HEAD~3",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None


def test_windows_unc_spaced_path_forward_slash_push_force_denies():
    payload = _payload(
        "//server/share/Program Files/Git/bin/git.exe push --force origin x",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_unc_spaced_path_bash_exe_inline_c_rm_rf_denies():
    payload = _payload(
        '\\\\server\\share\\Program Files\\Git\\bin\\bash.exe -c "rm -rf /tmp/x"',
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None


def test_windows_unc_spaced_path_status_allows():
    # Negative control: a benign command through a spaced UNC path must
    # still allow.
    payload = _payload(
        "\\\\server\\share\\Program Files\\Git\\bin\\git.exe status",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Case-variant executable basenames (2026-07-28 review, Finding 1, P1 --
# still-live bypass): Windows PATH/cmd.exe resolution is case-insensitive,
# so `GIT.EXE`, `Git.exe`, `GIT` are real, executable invocations on
# Windows exactly like the lowercase spelling -- every identity check in
# this module must fold case, not just the `.exe`-suffix strip.
# ---------------------------------------------------------------------------


def test_git_exe_uppercase_bare_stash_denies():
    payload = _payload("GIT.EXE stash", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "stash" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_exe_titlecase_bare_push_force_denies():
    payload = _payload("Git.exe push --force", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_uppercase_spaced_path_rebase_denies():
    payload = _payload(
        "C:\\Program Files\\Git\\bin\\GIT.EXE rebase -i HEAD~3",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None


def test_windows_git_exe_mixedcase_spaced_path_forward_slash_push_force_denies():
    payload = _payload(
        "C:/Program Files/Git/bin/Git.exe push --force origin x",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_windows_git_exe_uppercase_spaced_path_status_allows():
    # Negative control: a benign command through an uppercase-spelled spaced
    # path must still allow -- case-folding must not make matching sloppy.
    payload = _payload(
        "C:\\Program Files\\Git\\bin\\GIT.EXE status",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_gitk_uppercase_bare_invocation_allows():
    # Negative control: `GITK` (case-varied lookalike) must not be treated
    # as `git`.
    payload = _payload("GITK --all", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_foo_uppercase_bare_invocation_allows():
    payload = _payload("GIT-FOO.EXE status", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_legit_uppercase_bare_invocation_allows():
    payload = _payload("LEGIT.EXE stash", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_mygit_exe_uppercase_bare_invocation_allows():
    payload = _payload("MYGIT.EXE push --force", agent_type="coordinator:executor")
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# 2026-07-25 P0 fix -- SAFE-FORWARD OPTION-SURFACE HARDENING. The five
# confirmed live exploits from the dispatch brief (`_SAFE_GIT_SUBCOMMANDS`
# and several verb-level allows granted the whole remaining command line
# with no option inspection at all), plus allow-regression coverage proving
# these fixes did NOT over-deny the legitimate forms.
# ---------------------------------------------------------------------------


def test_git_show_output_write_denies():
    payload = _payload(
        "git show --output=/tmp/pwn.py HEAD", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "--output" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_log_output_write_denies():
    payload = _payload(
        "git log --output=/tmp/pwn.py", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "--output" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_show_output_attached_no_equals_denies():
    payload = _payload(
        "git show --output/tmp/pwn.py HEAD", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None


def test_git_show_ext_diff_denies():
    payload = _payload("git show --ext-diff HEAD", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "--ext-diff" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_show_dash_o_denies():
    payload = _payload(
        "git show -o/tmp/pwn.py HEAD", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None


def test_git_show_output_after_dashdash_allows():
    # A pathspec literally named `--output=x` given AFTER a bare `--`
    # terminator is a pathspec, not an option -- must not deny.
    payload = _payload(
        "git show HEAD -- --output=x", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_git_show_output_indicator_flag_still_allows():
    # Hyphen-boundary exemption: `--output-indicator-new=X` is a real,
    # non-write git formatting flag and must NOT be caught by the
    # `--output` prefix denial.
    payload = _payload(
        "git show --output-indicator-new=X HEAD", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_git_show_stat_allows():
    payload = _payload("git show --stat HEAD", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_log_patch_flag_allows():
    payload = _payload("git log -p", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_diff_name_only_allows():
    payload = _payload("git diff --name-only", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_checkout_force_short_denies():
    payload = _payload("git checkout -f", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git checkout -f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_checkout_force_long_denies():
    payload = _payload("git checkout --force", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None


def test_git_checkout_dash_b_force_reset_denies():
    payload = _payload(
        "git checkout -B main origin/main", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None


def test_git_checkout_dashdash_pathspec_still_allows_via_pre_existing_gate():
    # `git checkout -- somefile` was ALREADY denied before this fix (the
    # pre-existing `_CHECKOUT_DASHDASH_RE` pathspec-clobber gate) -- not a
    # newly-introduced deny.
    payload = _payload(
        "git checkout -- somefile", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git checkout <pathspec>" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_checkout_new_branch_no_force_allows():
    payload = _payload("git checkout -b feature/foo", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_switch_force_denies():
    payload = _payload("git switch -f other-branch", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None


def test_git_switch_discard_changes_denies():
    payload = _payload(
        "git switch --discard-changes other-branch", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None


def test_git_switch_dash_c_force_reset_denies():
    payload = _payload(
        "git switch -C main origin/main", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None


def test_git_switch_plain_allows():
    payload = _payload("git switch other-branch", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_branch_dash_m_upper_force_rename_denies():
    payload = _payload("git branch -M main", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "-M/-C" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_branch_dash_c_upper_force_copy_denies():
    payload = _payload("git branch -C main copy", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None


def test_git_branch_lowercase_m_with_force_denies():
    payload = _payload(
        "git branch -m -f oldname newname", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "-m/-c --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_branch_move_without_force_allows():
    payload = _payload(
        "git branch -m oldname newname", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_git_branch_plain_create_allows():
    payload = _payload("git branch feature/foo", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_tag_force_replace_denies():
    payload = _payload("git tag -f v1.0.0", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git tag -f" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_tag_plain_create_allows():
    payload = _payload("git tag v1.0.0", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_merge_prose_ff_only_in_message_still_denies():
    payload = _payload(
        'git merge -m "we prefer --ff-only merges" feat',
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git merge (not --ff-only)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_merge_real_ff_only_flag_still_allows():
    payload = _payload("git merge --ff-only feat", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_config_prose_get_in_value_denies():
    payload = _payload(
        'git config alias.lg "log --get"', agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git config (not --get)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_config_real_get_flag_still_allows():
    payload = _payload("git config --get user.name", agent_type="coordinator:executor")
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# 2026-07-26 UNSCOPED-STASH GAP CLOSE (module docstring) -- bare `git stash`
# / `git stash push` with no `--`-delimited pathspec sweeps every
# uncommitted change on the shared tree; only a pathspec-scoped push, and
# the read-only list/show forms, may remain allowed.
# ---------------------------------------------------------------------------


def test_git_stash_bare_denies():
    payload = _payload("git stash", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git stash (unscoped)" in reason
    assert "sweeps EVERY" in reason


def test_git_stash_push_no_pathspec_denies():
    payload = _payload("git stash push", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_implicit_push_flag_only_denies():
    # `git stash -u` is shorthand for `git stash push -u` -- same
    # sweep-everything shape, no pathspec.
    payload = _payload("git stash -u", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_push_with_pathspec_allows():
    payload = _payload(
        "git stash push -- state/subagent-share/my-file.md",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_git_stash_save_denies():
    # 2026-07-28: `git stash save [<msg>]` is the pre-2.16 deprecated spelling
    # of `git stash push [-m <msg>]` -- identical working-tree sweep. It was
    # grouped with `create`/`store` as an "other subcommand" and fell through
    # to allow, exempting a real unscoped sweep from this very rule. Found by
    # review of the EM-path sibling fix, which had inherited the same
    # misclassification from here.
    payload = _payload("git stash save", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_save_with_message_denies():
    payload = _payload('git stash save "wip"', agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_save_with_pathspec_allows():
    # The `--`-delimited pathspec scopes it, exactly as for `push`.
    payload = _payload(
        "git stash save -- state/subagent-share/my-file.md",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_git_stash_list_allows():
    payload = _payload("git stash list", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_stash_show_allows():
    payload = _payload("git stash show -p", agent_type="coordinator:executor")
    assert guard.check(payload) is None


# 2026-08-22 fix (UNSCOPED-STASH GAP, REOPENED): `shlex` has no concept of
# shell redirection, so `git stash 2>&1` tokenizes `remaining` to `["2>&1"]`
# -- a token that is neither `None` nor `"push"`/`-`-prefixed, so the
# `is_push_or_bare` test used to go False and an unscoped stash-push sailed
# through to the safe-forward allowlist. Confirmed live: a
# `coordinator:review-integrator` subagent's `git stash 2>&1 | head -5;
# echo done` swept 144 files on a shared tree and this guard never denied it
# (state/bug-backlog/2026-08-21-subagent-unscoped-stash-push-swept-144-f-
# ea557efb4908.yaml). Each case below reproduces the EXACT displacement
# shape -- a redirection occupying the position this guard reads as "the
# first real argument after stash" -- not a generic stash re-test.
def test_git_stash_bare_with_stderr_redirect_denies():
    payload = _payload("git stash 2>&1", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_bare_with_stderr_redirect_and_pipe_denies():
    # The EXACT shape from the incident transcript (`cd <repo> && git stash
    # 2>&1 | head -5; echo done`) -- repo path elided, irrelevant to
    # classification.
    payload = _payload(
        "cd repo && git stash 2>&1 | head -5; echo done",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_bare_with_stdout_redirect_denies():
    payload = _payload("git stash >/dev/null", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_bare_with_stderr_redirect_to_file_denies():
    payload = _payload("git stash 2>/dev/null", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_bare_with_separated_redirect_target_denies():
    # Whitespace between the operator and its target still yields two
    # `shlex` tokens (`[">", "/dev/null"]`) -- both must be consumed.
    payload = _payload("git stash > /dev/null", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_push_with_redirect_still_denies():
    # A real token (`push`) already occupies the first-argument position
    # before the redirect -- this shape denied even pre-fix; kept as a
    # boundary case so a future change can't silently narrow the strip to
    # break it.
    payload = _payload("git stash push 2>&1", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_dash_u_with_redirect_still_denies():
    payload = _payload("git stash -u 2>&1", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_pathspec_with_redirect_still_allows():
    # The `--`-delimited pathspec scoping must survive stripping a leading
    # redirection -- confirms the fix doesn't over-strip into `remaining`
    # and lose the `"--" not in remaining` scoping check.
    payload = _payload(
        "git stash push -- state/subagent-share/my-file.md 2>&1",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_git_stash_bare_with_input_redirect_denies():
    # 2026-08-23 fix: `<`-family input redirection is a distinct character
    # class from `>`-family output redirection, and the original regex only
    # matched the latter. `git stash </dev/null` tokenized `remaining` to
    # `["</dev/null"]`, undetected, and fell through to allow.
    payload = _payload("git stash </dev/null", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_bare_with_heredoc_marker_denies():
    payload = _payload("git stash <<EOF", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_bare_with_herestring_denies():
    payload = _payload("git stash <<< x", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_bare_with_separated_input_redirect_target_denies():
    payload = _payload("git stash < /dev/null", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_pathspec_with_input_redirect_still_allows():
    # Same over-stripping boundary as the `>`-family case above, for the
    # newly-widened `<`-family branch.
    payload = _payload(
        "git stash push -- state/subagent-share/my-file.md </dev/null",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_git_stash_pop_still_denies_as_pop_apply():
    payload = _payload("git stash pop", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git stash pop/apply" in reason
    assert "git stash (unscoped)" not in reason


def test_git_stash_apply_still_denies_as_pop_apply():
    payload = _payload("git stash apply stash@{0}", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash pop/apply" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_drop_still_denies_as_drop_clear():
    payload = _payload("git stash drop", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git stash drop/clear" in reason
    assert "git stash (unscoped)" not in reason


def test_git_stash_clear_still_denies_as_drop_clear():
    payload = _payload("git stash clear", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash drop/clear" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_pop_deny_message_names_no_stash_recipe():
    # 2026-07-29: pop/apply used to fall through to the generic catch-all
    # deny reason with no forward path at all. It now names the no-stash
    # pre-existing-failure recipe explicitly (design-as-offers).
    payload = _payload("git stash pop", agent_type="coordinator:executor")
    result = guard.check(payload)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git show <ref>:<path>" in reason
    assert "your-wip.bak" in reason
    assert "STACK-POSITION" in reason


def test_git_stash_apply_deny_message_names_no_stash_recipe():
    payload = _payload("git stash apply stash@{0}", agent_type="coordinator:executor")
    result = guard.check(payload)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git show <ref>:<path>" in reason


def test_git_stash_drop_deny_message_names_em_escalation():
    payload = _payload("git stash drop", agent_type="coordinator:executor")
    result = guard.check(payload)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Surface it to the EM" in reason
    assert "no-stash" in reason


def test_git_stash_clear_deny_message_names_em_escalation():
    payload = _payload("git stash clear", agent_type="coordinator:executor")
    result = guard.check(payload)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Surface it to the EM" in reason


def test_no_stash_pre_existing_failure_recipe_end_to_end_allows():
    # Regression for the doctrine/guard reconciliation (2026-07-29): a
    # subagent can now complete the pre-existing-failure-verification recipe
    # WITHOUT ever touching the stash stack or doing a whole-tree checkout --
    # every command in the replacement recipe must allow under this guard.
    recipe_commands = [
        # 1. Save the current (edited) content of an owned file.
        "cp coordinator_core/foo.py coordinator_core/foo.py.your-wip.bak",
        # 2. Overwrite it with the pre-edit content from a prior ref --
        #    `show` is on the safe-forward allowlist, so this allows
        #    regardless of whether the ref is HEAD, a merge-base SHA, or a
        #    stash entry (`stash@{0}:<path>`, for reading out an
        #    already-orphaned stash without popping it).
        "git show HEAD:coordinator_core/foo.py > coordinator_core/foo.py",
        "git show a1b2c3d4:coordinator_core/foo.py > coordinator_core/foo.py",
        "git show stash@{0}:coordinator_core/foo.py > coordinator_core/foo.py",
        # 3. (test runner invocation is outside this guard's surface --
        #    pytest/python3 without -c is not a wrapper-deny shape)
        # 4. Restore the edit.
        "cp coordinator_core/foo.py.your-wip.bak coordinator_core/foo.py",
        # 5. Clean up the backup -- bare rm, no -r/-f, unaffected.
        "rm coordinator_core/foo.py.your-wip.bak",
    ]
    for cmd in recipe_commands:
        payload = _payload(cmd, agent_type="coordinator:executor")
        result = guard.check(payload)
        assert result is None, f"recipe command unexpectedly denied: {cmd!r} -> {result}"


def test_git_stash_branch_form_unaffected_allows():
    # `git stash branch <name>` is a different subcommand entirely, not the
    # sweep-everything push/bare shape -- unaffected by this fix.
    payload = _payload("git stash branch my-recovery-branch", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_stash_word_in_commit_message_not_denied():
    # The word "stash" appearing in an unrelated position (here, a commit
    # message) must not false-trip the new rule.
    payload = _payload(
        'git commit -m "clean up leftover stash usage in docs"',
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# SETTINGS-HOME BIN NARROW EXEMPTION (2026-07-27) -- the doctrine-mandated
# settings-home CLI invocation form
# `"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<cli>"`
# must ALLOW despite its unresolved-argv0 shape; every bullet the exemption
# regex was narrowed against (see `_SETTINGS_HOME_BIN_EXEMPT_RE`'s module
# comment) must still DENY.
# ---------------------------------------------------------------------------


def test_settings_home_bin_documented_default_form_allows():
    payload = _payload(
        '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-doc-new" '
        "--type run-report --plan foo.md --chunk C1",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_settings_home_bin_documented_default_form_braced_home_allows():
    payload = _payload(
        '"${COORDINATOR_SETTINGS_HOME:-${HOME}/.coordinator-claude-settings}/bin/cross-repo-memo" '
        "draft foo --to bar-em",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_settings_home_bin_bare_var_no_default_allows():
    payload = _payload(
        "$COORDINATOR_SETTINGS_HOME/bin/coordinator-doc-new --type run-report",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_settings_home_bin_braced_var_no_default_allows():
    payload = _payload(
        "${COORDINATOR_SETTINGS_HOME}/bin/coordinator-doc-new --type run-report",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_settings_home_bin_unquoted_allows():
    # No surrounding quotes at all -- the exemption must not depend on
    # quoting, since shlex quote-stripping is applied identically either way.
    payload = _payload(
        "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-doc-new --type run-report",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_settings_home_bin_other_variable_still_advises():
    # A DIFFERENT variable name in the same shape must NOT be exempt --
    # still an unresolved (opaque) argv0, so it advises (RESHAPE, C18d).
    payload = _payload(
        "$FOO/bin/coordinator-doc-new --type run-report", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_settings_home_bin_arbitrary_braced_variable_still_advises():
    payload = _payload(
        "${ANYTHING}/bin/coordinator-doc-new --type run-report",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_settings_home_command_substitution_argv0_still_advises():
    # Command substitution anywhere in argv0 position must never be exempt,
    # even if it happens to reference `COORDINATOR_SETTINGS_HOME` textually.
    payload = _payload(
        "$(which rm) -rf /tmp/foo", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_settings_home_backtick_argv0_not_reclassified_by_this_change():
    # Pre-existing guard behavior, NOT something this change touches: `` `..`
    # `` is not `$`-prefixed, so it never entered `_ARGV0_UNRESOLVED_RE`'s
    # scope (that pattern is `^\$` only) even before this exemption existed
    # -- `` `which rm` `` tokenizes to a single opaque argv0 token that
    # matches neither the `rm` verb identity nor the new settings-home
    # exemption, so it is simply unclassified and allows. Documented here as
    # a not-in-scope boundary check: this exemption must not accidentally
    # WIDEN what's exempt to cover backtick substitution too (it doesn't --
    # the exempt regex requires a literal `$`/`${` prefix).
    payload = _payload(
        "`which rm` -rf /tmp/foo", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_settings_home_bin_wrong_default_still_advises():
    # `:-` supplies the value when the var is unset -- an attacker-chosen
    # default is an attacker-chosen command, so only the documented literal
    # default is exempt. Still opaque, so it advises rather than denies.
    payload = _payload(
        "${COORDINATOR_SETTINGS_HOME:-/tmp/evil}/bin/coordinator-doc-new --type run-report",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_settings_home_bin_path_traversal_tail_still_advises():
    payload = _payload(
        "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/../../evil --flag",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_settings_home_bin_nested_expansion_tail_still_advises():
    # A second, unresolved expansion embedded in the tail must not slip
    # through as a "plain name".
    payload = _payload(
        "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/$EVIL",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_settings_home_bin_metachar_in_tail_still_advises():
    # A command-substitution suffix fused directly onto the tail (no
    # whitespace/separator, so it stays part of the SAME argv0 token) must
    # not be treated as a plain resolved name -- `[A-Za-z0-9_.-]+$` rejects
    # the embedded `$(...)`.
    payload = _payload(
        "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-doc-new$(true)",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


def test_settings_home_bin_env_wrapped_form_allows():
    payload = _payload(
        "env FOO=bar ${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-doc-new --type run-report",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_settings_home_bin_env_wrapped_other_variable_still_advises():
    payload = _payload(
        "env FOO=bar $BAR/bin/coordinator-doc-new --type run-report",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "unresolved" in hso["additionalContext"]


# ---------------------------------------------------------------------------
# 2026-07-28 `git mv` CARVE-OUT DECLINED (DoE memo 2026-07-25 P2) -- the
# verdict is unchanged (still denied); what changed is that `mv` is now
# classified by name so the deny message can offer the `mv`-plus-report
# forward path instead of the generic unrecognized-verb text. These tests
# pin BOTH halves: the deny survives, and the offer is present.
# ---------------------------------------------------------------------------


def test_git_mv_still_denies():
    payload = _payload(
        "git mv state/lessons/a.yaml state/lessons/archive/a.yaml",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git mv (index-mutating rename)" in reason


def test_git_mv_deny_message_offers_plain_mv():
    payload = _payload("git mv a.yaml b.yaml", agent_type="coordinator:executor")
    reason = guard.check(payload)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mv A B" in reason
    assert "INTENDED route" in reason
    # Negative: the generic default-deny text must NOT be what a `git mv`
    # caller sees -- that regression is the whole point of this change.
    assert "unrecognized git verb" not in reason


def test_git_mv_deny_message_substitutes_real_paths():
    # 2026-07-29 duty-of-care promotion: the guard already holds the real
    # source/dest paths (`cmd`) -- the offer should reproduce them
    # concretely, ready to run, not just repeat the generic `mv A B`
    # template.
    payload = _payload(
        "git mv state/lessons/a.yaml state/lessons/archive/a.yaml",
        agent_type="coordinator:executor",
    )
    reason = guard.check(payload)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mv state/lessons/a.yaml state/lessons/archive/a.yaml" in reason
    assert "Run it now, with your own paths already substituted in" in reason


def test_git_mv_deny_message_quotes_spaced_real_paths():
    payload = _payload(
        'git mv "old dir/f 1.md" "new dir/f 2.md"',
        agent_type="coordinator:executor",
    )
    reason = guard.check(payload)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mv 'old dir/f 1.md' 'new dir/f 2.md'" in reason


def test_git_mv_deny_message_drops_flags_from_corrected_command():
    payload = _payload("git mv -f a.md b.md", agent_type="coordinator:executor")
    reason = guard.check(payload)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mv a.md b.md" in reason


def test_git_mv_deny_message_keeps_hyphen_led_dest_path_after_end_of_options():
    # 2026-07-30 M13/M19 review finding: a bare `-`-startswith filter with
    # no `--` end-of-options awareness dropped a REAL path operand that
    # merely happens to start with a hyphen, the same way `mv`/`git mv`
    # themselves would misparse it without `--` -- the corrected command
    # must keep both real operands, not silently lose one.
    payload = _payload(
        "git mv -- -weird-file.txt normal-dest.txt",
        agent_type="coordinator:executor",
    )
    reason = guard.check(payload)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mv -weird-file.txt normal-dest.txt" in reason


def test_git_mv_deny_message_still_drops_flags_before_end_of_options():
    # Companion negative: a `-`-prefixed token BEFORE `--` (or with no `--`
    # at all) is still filtered as a flag -- this fix closes the dropped-
    # real-operand case, it does not stop filtering ordinary flags.
    payload = _payload("git mv -f -- a.md b.md", agent_type="coordinator:executor")
    reason = guard.check(payload)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mv a.md b.md" in reason


def test_git_mv_malformed_falls_back_to_generic_template_not_a_guess():
    # Fewer than two path operands (a malformed `git mv` real git would
    # itself reject) -- the guard must not fabricate a corrected command;
    # it falls back to the pre-existing generic `mv A B` wording.
    payload = _payload("git mv a.md", agent_type="coordinator:executor")
    reason = guard.check(payload)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Move the file with plain `mv A B`" in reason
    assert "Run it now, with your own paths already substituted in" not in reason


def test_unrecognized_git_verb_still_default_denies_generically():
    # The named `mv` branch must not weaken default-deny for anything else.
    payload = _payload("git mvfoo x", agent_type="coordinator:executor")
    reason = guard.check(payload)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unrecognized git verb (default-deny)" in reason


def test_git_show_sha_path_allows():
    # DoE memo's second P2 ask (read-only `git show <sha>:<path>` for a
    # before/after diff) needed no change -- `show` was already safe-listed.
    # Pinned so a future allowlist edit cannot silently retract it.
    payload = _payload(
        "git show 1234abc:coordinator_core/ops/foo.py",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# COMMAND-POSITION GIT-TOKEN FIX (2026-07-28) -- confirmed live: a subagent's
# plain `grep -n "git " file | head -50` (the token "git " appearing only
# inside a double-quoted grep PATTERN, never invoked as a command) was denied
# as "unrecognized git verb (default-deny)". Root cause: the anchored
# per-segment classifier searched every shlex token in a segment for one
# equal to `git`, not just the COMMAND-POSITION token; when no exact match
# existed it fell over to the free-text LEGACY classifier, whose own
# default-deny fires on nothing more than the segment containing the
# free-text word "git" (module docstring "COMMAND-POSITION GIT-TOKEN FIX").
#
# Per the class-precedent lesson this fix's commit message cites (a
# deny-guard tested only on the side that denies lets a pattern that merely
# RESEMBLES its target through unnoticed), this section is deliberately
# split into an ALLOW half (the previously-missing coverage) and a DENY half
# (regression pins for every verb this guard's own deny ladder enumerates,
# now exercised through the SAME command-position code path).
# ---------------------------------------------------------------------------


# --- ALLOW side: "git" present as data, never invoked as a command --------


def test_grep_quoted_git_pattern_allows():
    # The exact live-denied shape: "git " as a double-quoted grep PATTERN
    # operand, piped to another command.
    payload = _payload(
        'grep -n "git " somefile.md | head -50', agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_grep_git_word_boundary_pattern_allows():
    # A bare (unquoted-adjacent-word) "git" token as a grep pattern operand,
    # not the invoked command.
    payload = _payload("grep git somefile.md", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_grep_regex_alternation_containing_git_allows():
    # "git" as one branch of a regex alternation inside a grep pattern --
    # the exact shape from the live sidecar reproduction.
    payload = _payload(
        'grep -E "(status|git|push)" somefile.md', agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_ripgrep_git_pattern_single_quoted_allows():
    payload = _payload("rg 'git verb' somefile.md", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_double_quoted_string_containing_git_allows():
    payload = _payload('echo "this mentions git in prose"', agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_single_quoted_string_containing_git_allows():
    payload = _payload("echo 'this mentions git in prose'", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_gitignore_filename_allows():
    payload = _payload("cat .gitignore", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_digit_substring_allows():
    payload = _payload("echo digit", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_legit_substring_allows():
    payload = _payload("echo legit", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_crypt_filename_as_argument_allows():
    payload = _payload("ls git-crypt", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_crypt_as_invoked_command_allows():
    # `git-crypt` invoked AS THE COMMAND itself (not `git` with a `-crypt`
    # suffix -- a distinct binary) must not be treated as a git invocation.
    payload = _payload("git-crypt status", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_as_longer_word_substring_allows():
    # "git" as part of a longer unquoted word, with no word boundary at all
    # -- never matches the guard's own `\bgit\b` gate, included for
    # completeness of the "as part of a longer word" AC.
    payload = _payload("echo digitalgitized", agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_git_subcommand_and_remaining_for_segment_not_a_git_invocation():
    # Direct unit coverage of the sentinel contract: a command-position head
    # that is NOT `git` returns the `_NOT_A_GIT_INVOCATION` sentinel with
    # `parse_ok=True` (allow outright), never `parse_ok=False` (which would
    # route to the legacy free-text classifier and reintroduce the bug).
    subcmd, parse_ok, remaining = guard._git_subcommand_and_remaining_for_segment(
        'grep -n "git " file'
    )
    assert parse_ok is True
    assert subcmd is guard._NOT_A_GIT_INVOCATION
    assert remaining == []


# --- DENY side: every subshell/env/pipe-adjacent shape must still deny ----


def test_git_push_force_after_pipe_still_denies():
    payload = _payload(
        "echo hi | git push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_push_force_after_and_and_still_denies():
    payload = _payload(
        "echo hi && git push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_push_force_after_or_or_still_denies():
    payload = _payload(
        "false || git push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_push_force_after_semicolon_still_denies():
    payload = _payload(
        "echo hi ; git push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_push_force_env_prefixed_still_denies():
    payload = _payload(
        "env FOO=1 git push --force", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_push_force_glued_subshell_still_denies():
    payload = _payload("(git push --force)", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_push_force_spaced_subshell_still_denies():
    payload = _payload("( git push --force )", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git push --force" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_rebase_still_denies_command_position():
    payload = _payload("git rebase -i HEAD~3", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git rebase" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_stash_unscoped_still_denies():
    payload = _payload("git stash", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git stash (unscoped)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_reset_hard_still_denies():
    payload = _payload("git reset --hard HEAD~1", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git reset --hard" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_branch_dash_d_upper_still_denies():
    payload = _payload("git branch -D somebranch", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git branch -D" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_commit_amend_still_denies():
    payload = _payload("git commit --amend -m x", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git commit --amend" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_worktree_remove_still_denies():
    payload = _payload("git worktree remove /tmp/foo", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git worktree remove" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_mv_still_denies_command_position():
    payload = _payload("git mv a.yaml b.yaml", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert "git mv (index-mutating rename)" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_restore_word_in_commit_message_does_not_deny():
    # Regression: `_RESTORE_KEYWORD_RE` used to be free-text `\brestore\b`,
    # matching the word ANYWHERE in the segment -- including inside a
    # quoted `-m` commit-message operand -- rather than an actual `git
    # restore` subcommand invocation. `block_subagent_commit.py`'s own
    # module docstring documents this collision under "Known pre-existing
    # false-positive NOT inherited here".
    payload = _payload(
        'git commit -m "restore the carve-out"', agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


def test_restore_word_in_commit_message_within_indirection_payload_does_not_deny():
    # Same false positive, hit via the `strict=False` indirection-payload
    # scan (`_unwrap_and_classify`, e.g. a `python3 -c '...'` one-liner
    # whose Python source text embeds a git-commit-with-message string) --
    # this is the shape that reproduced live during this guard's own
    # census probe.
    payload = _payload(
        "python3 -c \"cmd = 'git commit -m \\\"restore the carve-out\\\"'\"",
        agent_type="coordinator:executor",
    )
    assert guard.check(payload) is None


def test_git_restore_working_tree_still_denies():
    payload = _payload("git restore path/to/file.txt", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git restore (working tree)" in reason
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_git_subcommand_and_remaining_for_segment_bare_git_still_none_subcmd():
    # Bare `git` (command-position head IS git, no subcommand) is a DISTINCT
    # state from `_NOT_A_GIT_INVOCATION` and must still route through the
    # anchored default-deny ladder, not the new allow-outright branch.
    subcmd, parse_ok, remaining = guard._git_subcommand_and_remaining_for_segment("git")
    assert parse_ok is True
    assert subcmd is None
    assert remaining == []


# ---------------------------------------------------------------------------
# FAIL-OPEN OBSERVABILITY (2026-07-29 addition) -- pins that each of the
# three fail-open branches now records an identity-resolution tuple to the
# settings-home fail-open log, that a fail-open on a NON-flagged command
# logs nothing (cost-gating preserved), that normal deny/allow behavior is
# byte-for-byte unchanged, and that a broken logging path never raises or
# changes a verdict. Every test here redirects `guard._fail_open_log_path`
# to a tmp file so no test writes into the real settings-home log.
# ---------------------------------------------------------------------------


def test_fail_open_no_agent_id_key_logs_branch(tmp_path, monkeypatch):
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git rm --cached secret.txt"},
        "session_id": "sess1",
        "cwd": "/some/repo",
    }
    assert guard.check(payload) is None
    content = log_path.read_text(encoding="utf-8")
    assert "FAIL-OPEN" in content
    assert "branch='no-agent-id-key'" in content
    assert "git rm --cached secret.txt" in content


def test_kind_unresolved_unparseable_agent_id_logs_branch_and_denies(tmp_path, monkeypatch):
    # _resolve_subagent_identity resolves to "" AND no agent_type leg is
    # present -- effective_type ends up genuinely empty, so this is a true
    # kind-resolution failure (unlike the known-agent_type case covered by
    # test_unparseable_agent_id_with_known_agent_type_still_denies, which no
    # longer reaches this log branch at all since its PRIMARY leg already
    # resolves a kind). Fixed 2026-07-30: denies (fail-closed), and is still
    # recorded for frequency measurement -- the branch tag changed from the
    # old 'unresolvable-agent-id' (a fail-open marker) to
    # 'kind-unresolved-unparseable-agent-id'.
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "")
    payload = _payload("git rebase -i", agent_type=None)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    content = log_path.read_text(encoding="utf-8")
    assert "branch='kind-unresolved-unparseable-agent-id'" in content
    assert "raw_agent_id='deadbeef0123'" in content


def test_kind_unresolved_empty_effective_type_logs_branch_and_denies(tmp_path, monkeypatch):
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: "")
    payload = _payload("git rebase -i", agent_type=None)
    payload["agent_id"] = "deadbeef0123"
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    content = log_path.read_text(encoding="utf-8")
    # Branch tag changed from the old fail-open marker 'empty-effective-type'
    # to 'kind-unresolved-empty-effective-type' (2026-07-30 fix).
    assert "branch='kind-unresolved-empty-effective-type'" in content
    assert "agent_id='deadbeef0123'" in content
    assert "git_root='/fake/git-root'" in content
    # PRIMARY leg absent from the payload, SECONDARY leg computed-but-empty --
    # the two must be distinguishable, not collapsed to the same marker.
    assert "agent_type='<absent>'" in content
    assert "subagent_type=''" in content


def test_fail_open_non_destructive_command_logs_nothing(tmp_path, monkeypatch):
    # Cost-gating: a command that never trips Layer 1 must never reach the
    # fail-open logging code at all, even with no agent_id key present.
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la && grep foo bar.txt"},
        "session_id": "sess1",
    }
    assert guard.check(payload) is None
    assert not log_path.exists()


def test_fail_open_logging_active_normal_deny_still_denies(tmp_path, monkeypatch):
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    payload = _payload("git rebase -i HEAD~3", agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "git rebase" in result["hookSpecificOutput"]["permissionDecisionReason"]
    # A resolved deny is not a fail-open -- nothing should be logged.
    assert not log_path.exists()


def test_fail_open_logging_active_normal_allow_still_allows(tmp_path, monkeypatch):
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    payload = _payload("git status", agent_type="coordinator:executor")
    assert guard.check(payload) is None
    # A resolved (non-fail-open) allow via the safe-forward allowlist is not
    # a fail-open either -- nothing should be logged.
    assert not log_path.exists()


def test_fail_open_logging_failure_does_not_raise_or_change_verdict(monkeypatch):
    def _boom():
        raise OSError("simulated unwritable settings-home")

    monkeypatch.setattr(guard, "_fail_open_log_path", _boom)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git rm --cached secret.txt"},
        "session_id": "sess1",
    }
    # Must not raise, and the verdict (fail-open allow) must be unchanged.
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# MACHINE-LOCAL REGISTRY WRITE DENY (2026-08-03, DR-125 implementation --
# code-reviewer Finding 3, narrow-subagent-commit-confinement-two-classes.md
# chunk C2 review). Keyed on COMMAND SHAPE for EVERY resolved subagent type,
# NOT on `_helpers._CONFINED_FINDINGS_AGENTS` membership -- see the module
# comment above `_MACHINE_LOCAL_WRITE_SUBCOMMANDS`. Subcommand list verified
# against the real CLI's `main()` subparser dispatch table
# (`<settings-home>/bin/_machine_local.py`), not guessed.
# ---------------------------------------------------------------------------


def test_machine_local_set_denies_executor():
    payload = _payload(
        "machine-local set repos.doe_claude /evil", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "machine-local set" in reason


def test_machine_local_set_denies_code_reviewer():
    payload = _payload(
        "machine-local set repos.doe_claude /evil", agent_type="coordinator:code-reviewer"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_machine_local_set_denies_unresolved_unknown_subagent_type(monkeypatch):
    # agent_id resolves truthy, but BOTH the PRIMARY leg (payload agent_type,
    # absent here) and the SECONDARY leg (_read_backpointer_subagent_type)
    # resolve empty -- effective_type ends up "". Layer 2's machine-local
    # check does not gate on effective_type (only the AMBIGUOUS override
    # does), so an unresolved subagent kind still denies -- fail-closed,
    # same posture as every other Layer-2 deny in this module.
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: "")
    payload = _payload("machine-local set repos.doe_claude /evil", agent_type=None)
    payload["agent_id"] = "deadbeef0123"
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "machine-local array-append publish.targets repo:evil",
        "machine-local array-set publish.targets repo:evil",
        "machine-local migrate-publish-mirrors",
    ],
)
def test_machine_local_other_write_subcommands_deny(cmd):
    payload = _payload(cmd, agent_type="coordinator:executor")
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "machine-local get repos.doe_claude",
        "machine-local has repos.doe_claude",
        "machine-local keys --prefix repos",
        "machine-local path",
        "machine-local dir",
    ],
)
def test_machine_local_read_subcommands_allow(cmd):
    payload = _payload(cmd, agent_type="coordinator:executor")
    assert guard.check(payload) is None


def test_machine_local_path_prefixed_spelling_denies():
    payload = _payload(
        "/usr/local/bin/machine-local set repos.doe_claude /evil",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_machine_local_windows_cmd_twin_spelling_denies():
    payload = _payload(
        "machine-local.cmd set repos.doe_claude /evil", agent_type="coordinator:executor"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_machine_local_chained_segment_denies():
    payload = _payload(
        "ls -la ; machine-local set repos.doe_claude /evil",
        agent_type="coordinator:executor",
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_machine_local_set_em_main_loop_unaffected():
    # No agent_id key at all -> top-level EM Bash call -> allow, before any
    # identity-resolution cost is paid.
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "machine-local set repos.doe_claude /evil"},
        "session_id": "sess1",
    }
    assert guard.check(payload) is None


def test_machine_local_set_mutation_check_matcher_removed_flips_to_allow(monkeypatch):
    # Confirms the deny is load-bearing on _MACHINE_LOCAL_WRITE_SUBCOMMANDS,
    # not on some other incidental gate -- emptying the set must flip the
    # exact same payload from deny to allow.
    monkeypatch.setattr(guard, "_MACHINE_LOCAL_WRITE_SUBCOMMANDS", frozenset())
    payload = _payload(
        "machine-local set repos.doe_claude /evil", agent_type="coordinator:executor"
    )
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# POWERSHELL-DIALECT CASES (C4d, 2026-08-07). Every command here is one of
# the twelve measured returning bare `None` in the spike verdict record
# (docs/research/spike-verdicts/2026-08-07-powershell-guard-detection-and-
# tokenizer-mechanism.md, Table 1), plus the one that already denied
# (`rm -Recurse -Force <path>`, kept here as a regression pin on the "do not
# duplicate an alias collision" finding) and the object-pipeline blind spot
# (docs/reference/guard-dialect-coverage.md, "Object-pipeline defeats").
# `requires_powershell_grammar` skips these cleanly on a peer/clean-install
# box that lacks tree-sitter-pwsh (C8's dependency, not yet declared) --
# see that marker's own docstring in test_command_tokenizer_length_ceiling.py.
# ---------------------------------------------------------------------------
import importlib.util

from coordinator_core.bash_guards._verdict import collecting, was_silent

_PS_GRAMMAR_PRESENT = all(
    importlib.util.find_spec(name) is not None
    for name in ("tree_sitter", "tree_sitter_pwsh")
)
requires_powershell_grammar = pytest.mark.skipif(
    not _PS_GRAMMAR_PRESENT,
    reason=(
        "PowerShell grammar package not installed; C8 declares it in "
        "pyproject.toml."
    ),
)


def _ps_payload(command, agent_id="deadbeef0123", agent_type=None, session_id="sess1"):
    p = {
        "tool_name": "PowerShell",
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": None,
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _assert_denies(payload):
    result = guard.check(payload)
    assert result is not None, "expected a deny verdict, got a bare clean"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


@requires_powershell_grammar
def test_powershell_remove_item_recurse_force_denies():
    _assert_denies(
        _ps_payload(
            "Remove-Item -Recurse -Force C:/scratch/target",
            agent_type="coordinator:executor",
        )
    )


@requires_powershell_grammar
def test_powershell_remove_item_prefix_flags_denies():
    # Prefix-set matching (finding 2): `-r`/`-fo` are the working PowerShell
    # spellings -- `rm -rf` (clustered) cannot execute in PowerShell at all.
    _assert_denies(
        _ps_payload("Remove-Item -r -fo C:/scratch/target", agent_type="coordinator:executor")
    )


@requires_powershell_grammar
def test_powershell_ri_alias_denies():
    _assert_denies(
        _ps_payload("ri -Recurse -Force C:/scratch/target", agent_type="coordinator:executor")
    )


@requires_powershell_grammar
def test_powershell_rd_slash_flags_denies():
    _assert_denies(_ps_payload("rd /s /q C:/scratch/target", agent_type="coordinator:executor"))


@requires_powershell_grammar
def test_powershell_del_slash_flags_denies():
    _assert_denies(
        _ps_payload("del /f /s /q C:/scratch/target", agent_type="coordinator:executor")
    )


@requires_powershell_grammar
def test_powershell_icacls_denies():
    _assert_denies(
        _ps_payload(
            "icacls C:/scratch/target /grant Everyone:F /T", agent_type="coordinator:executor"
        )
    )


@requires_powershell_grammar
def test_powershell_backtick_continuation_denies():
    cmd = "Remove-Item -Recurse -Force `\n  C:/scratch/target"
    _assert_denies(_ps_payload(cmd, agent_type="coordinator:executor"))


@requires_powershell_grammar
def test_powershell_env_path_target_denies():
    _assert_denies(
        _ps_payload(
            "Remove-Item -Recurse -Force $env:TEMP/scratch-target",
            agent_type="coordinator:executor",
        )
    )


@requires_powershell_grammar
def test_powershell_semicolon_chain_denies():
    _assert_denies(
        _ps_payload(
            "Set-Location C:/; Remove-Item -Recurse -Force C:/scratch/target",
            agent_type="coordinator:executor",
        )
    )


@requires_powershell_grammar
def test_powershell_call_operator_prefix_denies():
    _assert_denies(
        _ps_payload(
            "& Remove-Item -Recurse -Force C:/scratch/target", agent_type="coordinator:executor"
        )
    )


@requires_powershell_grammar
def test_powershell_call_operator_quoted_verb_denies():
    _assert_denies(
        _ps_payload(
            "& 'Remove-Item' -Recurse -Force C:/scratch/target",
            agent_type="coordinator:executor",
        )
    )


@requires_powershell_grammar
def test_powershell_call_operator_double_quoted_verb_denies():
    _assert_denies(
        _ps_payload(
            '& "Remove-Item" -Recurse -Force C:/scratch/target',
            agent_type="coordinator:executor",
        )
    )


@requires_powershell_grammar
def test_powershell_call_operator_quoted_icacls_denies():
    _assert_denies(
        _ps_payload(
            "& 'icacls' C:/scratch/target /grant Everyone:F /T",
            agent_type="coordinator:executor",
        )
    )


@requires_powershell_grammar
def test_powershell_call_operator_quoted_stop_process_denies():
    _assert_denies(
        _ps_payload(
            "& 'Stop-Process' -Id 1234 -Force", agent_type="coordinator:executor"
        )
    )


@requires_powershell_grammar
def test_powershell_backtick_inside_verb_denies():
    # `Rem`ove-Item` -- a backtick before an ordinary character is a no-op
    # escape PowerShell itself resolves to `Remove-Item` at runtime.
    _assert_denies(
        _ps_payload(
            "Rem`ove-Item -Recurse -Force C:/scratch/target",
            agent_type="coordinator:executor",
        )
    )


@requires_powershell_grammar
def test_powershell_call_operator_parenthesized_quoted_verb_denies():
    _assert_denies(
        _ps_payload(
            "&('Remove-Item') -Recurse -Force C:/scratch/target",
            agent_type="coordinator:executor",
        )
    )


@requires_powershell_grammar
def test_powershell_call_operator_get_command_denies():
    _assert_denies(
        _ps_payload(
            "&(Get-Command Remove-Item) -Recurse -Force C:/scratch/target",
            agent_type="coordinator:executor",
        )
    )


@requires_powershell_grammar
def test_powershell_stop_process_denies():
    _assert_denies(
        _ps_payload("Stop-Process -Id 1234 -Force", agent_type="coordinator:executor")
    )


@requires_powershell_grammar
def test_powershell_rm_alias_denies_via_existing_dialect_neutral_probe():
    # `rm` is a real PowerShell alias for Remove-Item. The Bash-leg raw-text
    # `_RM_SURFACE_RE`/`_RM_DENY_RE` probes are never reached for a
    # `tool_name == "PowerShell"` dispatch (the PowerShell leg is a fully
    # separate branch off `check()`), so `rm` is included directly in the
    # PowerShell verb table (`_PS_REMOVE_VERBS`) -- see that constant's own
    # comment for why this is genuinely new coverage, not a duplicate.
    _assert_denies(
        _ps_payload("rm -Recurse -Force C:/scratch/target", agent_type="coordinator:executor")
    )


@requires_powershell_grammar
def test_powershell_object_pipeline_target_records_silent_not_bare_clean():
    # The structurally-unfixable blind spot (guard-dialect-coverage.md,
    # "Object-pipeline defeats"): the target lives entirely in the object
    # pipeline, never as a token. Verdict may be a genuine allow (None), but
    # the guard must have DECLINED to rule, not silently cleared it.
    payload = _ps_payload(
        "Get-ChildItem C:/scratch | Remove-Item -Force", agent_type="coordinator:executor"
    )
    with collecting() as silences:
        result = guard.check(payload)
    assert result is None
    assert was_silent("block_subagent_destructive_action", silences), (
        "object-pipeline-fed Remove-Item must record SILENT, not a bare clean"
    )


@requires_powershell_grammar
def test_powershell_benign_command_allows_no_silent():
    payload = _ps_payload("Get-ChildItem C:/scratch", agent_type="coordinator:executor")
    with collecting() as silences:
        result = guard.check(payload)
    assert result is None
    assert not was_silent("block_subagent_destructive_action", silences)


def test_powershell_no_agent_id_allows_even_destructive():
    payload = {
        "tool_name": "PowerShell",
        "tool_input": {"command": "Remove-Item -Recurse -Force C:/scratch/target"},
        "session_id": "sess1",
    }
    assert guard.check(payload) is None
