"""Smoke tests for coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist.

Covers the post-2026-07-22 coordinator-doc-new naked-Python-migration
divergence (the ``bash `` interpreter-prefix acceptance swapped for
``python3 ``, and the deny-message "Accepted invocation forms" block
modernized to offer the dispatch-time EM-resolved absolute-path form) plus
the byte-for-byte-preserved allow/deny ladder (metacharacter set,
word-boundary ``--type review-findings`` check, confined-agent OR-resolver).

Pure Python -- no shell spawns, no filesystem writes (Windows+macOS
first-class). Identity resolution is monkeypatched directly onto the guard
module object (the same seam-patching pattern used by the sibling test file
``test_block_subagent_destructive_action.py``), so no real git repo or
back-pointer chain on disk is required.

Spec backlink: coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py
"""

from __future__ import annotations

from typing import Any, Dict

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)

# SSOT confined-findings-agent type string, per _helpers._CONFINED_FINDINGS_AGENTS.
_CONFINED_TYPE = "coordinator:code-reviewer"
# NOTE (Amendment 1, 2026-08-01): coordinator:executor JOINED the confined
# set this same dispatch (see docs/plans/2026-08-01-confine-subagent-bash-by-
# allowlist.md), so it is no longer a valid stand-in for "any non-confined
# type" -- swapped to a genuinely non-confined type so
# test_non_confined_agent_type_allows (below) still exercises what its name
# says. test_secondary_leg_backpointer_confinement_denies (further down)
# still passes unaffected either way, since it asserts DENY regardless of
# which type is used for the (deliberately non-confined) primary leg.
_NON_CONFINED_TYPE = "coordinator:enricher"

_CLAUDE_KLABAUTER_ABS_PATH = "/x/claude-klabauter/coordinator/bin/coordinator-doc-new"


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


def _confine(monkeypatch, subagent_type=_CONFINED_TYPE):
    """Wire the guard's identity-resolution seam so agent_id resolves truthy
    and the secondary (back-pointer) leg resolves to ``subagent_type`` --
    lets the confined-agent path fire without a real git repo/back-pointer
    chain on disk.
    """
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id: subagent_type,
    )


# ---------------------------------------------------------------------------
# Allow cases
# ---------------------------------------------------------------------------


def test_non_bash_tool_allows():
    payload = {"tool_name": "Write", "tool_input": {"command": "rm -rf /"}}
    assert guard.check(payload) is None


def test_no_agent_id_allows():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
        "session_id": "sess1",
    }
    assert guard.check(payload) is None


def test_non_confined_agent_type_allows(monkeypatch):
    _confine(monkeypatch, subagent_type="")
    payload = _payload("rm -rf /", agent_type=_NON_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_absolute_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f"{_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings --plan p.md"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_python3_prefixed_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f"python3 {_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_legacy_bareword_invocation_still_allows(monkeypatch):
    # Still mechanically accepted for legacy dispatch prompts -- no longer
    # offered in the deny message, but not removed from the predicate.
    _confine(monkeypatch)
    payload = _payload(
        "coordinator-doc-new --type review-findings", agent_type=_CONFINED_TYPE
    )
    assert guard.check(payload) is None


def test_confined_type_arg_at_end_of_command_allows(monkeypatch):
    # Exercises the _REQUIRED_TYPE_ARG_END branch (cmd.endswith(...)),
    # distinct from the _REQUIRED_TYPE_ARG_MID substring branch.
    _confine(monkeypatch)
    payload = _payload(
        f"{_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings", agent_type=_CONFINED_TYPE
    )
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Deny cases
# ---------------------------------------------------------------------------


def test_bash_prefixed_invocation_now_denies(monkeypatch):
    # bash-prefix acceptance was retired 2026-07-22 -- coordinator-doc-new is
    # a naked Python CLI now, so `bash <path>` would fail at runtime.
    _confine(monkeypatch)
    cmd = f"bash {_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    # First token is literally "bash" -- never stripped, so the deny reason
    # must name it as the (wrong) first token.
    assert "bash" in reason
    assert _CLAUDE_KLABAUTER_ABS_PATH in reason


def test_metacharacter_command_denies(monkeypatch):
    _confine(monkeypatch)
    cmd = f"{_CLAUDE_KLABAUTER_ABS_PATH} --type review-findings; rm -rf /"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "shell-chaining metacharacter"
        in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_missing_type_arg_denies(monkeypatch):
    _confine(monkeypatch)
    payload = _payload(_CLAUDE_KLABAUTER_ABS_PATH, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "--type review-findings"
        in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_word_boundary_violation_denies(monkeypatch):
    # --type review-findingsXYZ must NOT match the word-boundary check.
    _confine(monkeypatch)
    cmd = f"{_CLAUDE_KLABAUTER_ABS_PATH} --type review-findingsXYZ"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_empty_command_denies(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("", agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "command could not be parsed"
        in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_arbitrary_command_denies(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("rm -rf /", agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_python3_prefixed_lookalike_suffix_denies(monkeypatch):
    # Review F1/F2: a free-text token whose tail literally spells
    # "coordinator-doc-new" (no path-separator boundary before the suffix)
    # must deny -- a bare endswith() check would mechanically allow this.
    _confine(monkeypatch)
    cmd = "python3 evil-coordinator-doc-new --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "evil-coordinator-doc-new" in reason


def test_unprefixed_lookalike_suffix_denies(monkeypatch):
    # Same suffix-bypass shape without the python3 prefix -- confirms the
    # boundary check fires regardless of interpreter-prefix stripping.
    _confine(monkeypatch)
    cmd = "evil-coordinator-doc-new --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_windows_path_prefixed_invocation_allows(monkeypatch):
    # Windows is first-class (per CLAUDE.md): a dispatch prompt may inject a
    # backslash-separated absolute path. A backslash is not in the banned
    # metacharacter tuple (`; && || | \` $( > < &`), so this path is clean
    # of metacharacters and the boundary check (F1) must accept the `\`
    # separator immediately preceding the suffix.
    _confine(monkeypatch)
    cmd = r"C:\claude-klabauter\coordinator\bin\coordinator-doc-new --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_bin_relative_path_invocation_allows(monkeypatch):
    # bin/-relative POSIX form: char immediately before the suffix is `/`.
    _confine(monkeypatch)
    cmd = "bin/coordinator-doc-new --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Quoted-binary-path cases (M6 forcing-gap fix, 2026-07-24)
# ---------------------------------------------------------------------------


def test_confined_quoted_absolute_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f'"{_CLAUDE_KLABAUTER_ABS_PATH}" --type review-findings'
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_quoted_var_expanded_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = (
        '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}'
        '/bin/coordinator-doc-new" --type review-findings'
    )
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_python3_quoted_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f'python3 "{_CLAUDE_KLABAUTER_ABS_PATH}" --type review-findings'
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_confined_single_quoted_path_invocation_allows(monkeypatch):
    _confine(monkeypatch)
    cmd = f"'{_CLAUDE_KLABAUTER_ABS_PATH}' --type review-findings"
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_quoted_non_allowlisted_binary_denies(monkeypatch):
    # A quote must not become an allowlist-bypass vector: a quoted binary
    # that isn't coordinator-doc-new (or a Tier A read-only command -- note
    # "/usr/bin/ls" would now legitimately match Tier A post-2026-07-25,
    # so this uses a binary that is on neither tier) is still denied.
    _confine(monkeypatch)
    cmd = '"/usr/bin/whoami" -la'
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_quoted_metacharacter_injection_still_denies(monkeypatch):
    # A quote must not become a smuggling vector for arbitrary execution --
    # the OUTCOME (still denied) is unchanged, but the REASON changed
    # legitimately with the 2026-07-25 quote-aware metacharacter fix.
    #
    # `"; rm -rf" --type review-findings` -- from a REAL shell's point of
    # view, `"; rm -rf"` is ONE quoted, literal argument (the semicolon is
    # data, not a chain operator); it is not actually an injection at all,
    # it is an attempt to invoke a program literally named `; rm -rf`,
    # which doesn't exist. Pre-fix, the raw substring scan denied this via
    # the metacharacter gate (a false-positive-shaped coincidence: it
    # happened to deny a harmless-if-executed string for the wrong reason).
    # Post-fix, the metacharacter gate correctly finds nothing live (the
    # semicolon is quoted), so the command falls through to Tier A/B, where
    # `_extract_first_token`'s approximate quote-strip -- NOT a full shell
    # parser, see its own docstring -- leaves a bare `;` as the first
    # token, which matches no Tier A binary and isn't `coordinator-doc-new`,
    # so it still denies, now via the "first command token is not
    # coordinator-doc-new" path. Both reasons are correct engineering; this
    # assertion tracks the new (also correct) one rather than asserting a
    # stale implementation detail.
    _confine(monkeypatch)
    cmd = '"; rm -rf" --type review-findings'
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "first command token is not coordinator-doc-new"
        in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


# ---------------------------------------------------------------------------
# Quote-aware metacharacter gate (2026-07-25 fix): a metacharacter INSIDE a
# quoted argument is literal data and must be allowed; the SAME
# metacharacter unquoted must still deny. Both directions covered here.
# ---------------------------------------------------------------------------


def test_grep_double_quoted_pipe_alternation_allows(monkeypatch):
    # Real-world false positive #1: regex alternation inside a double-quoted
    # pattern must not be treated as a shell pipe.
    _allow('grep -n "A|B" file', monkeypatch)


def test_git_log_single_quoted_format_pipe_allows(monkeypatch):
    # Real-world false positive #2: a `|` inside a single-quoted --format
    # value is literal formatting syntax, not a pipe.
    _allow("git log --format='%h|%s'", monkeypatch)


def test_grep_double_quoted_semicolon_allows(monkeypatch):
    # Real-world false positive #3: a quoted ";" search target is literal
    # data, not a command separator.
    _allow('grep -c ";" file', monkeypatch)


def test_grep_unquoted_pipe_alternation_denies(monkeypatch):
    # The unquoted counterpart of the first false positive above: without
    # quotes, `|` genuinely is a shell pipe and must still deny.
    result = _deny("grep -n A|B file", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_grep_escaped_unquoted_redirect_allows(monkeypatch):
    # A backslash-escaped metacharacter outside quotes is literal data to a
    # real shell (the backslash strips its special meaning), not an
    # operator -- e.g. `grep foo \> bar` passes a literal ">" argument to
    # grep, it does not redirect output.
    _allow(r"grep foo \> bar", monkeypatch)


def test_grep_command_substitution_inside_double_quotes_denies(monkeypatch):
    # Double quotes suppress word-splitting/globbing but NOT command
    # substitution -- $(...) still executes even inside a double-quoted
    # string in a real shell, so this must still deny.
    result = _deny('grep "$(whoami)" file', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_grep_backtick_inside_single_quotes_allows(monkeypatch):
    # Single quotes fully suppress ALL substitution, including backticks --
    # unlike double quotes, a backtick inside single quotes is inert.
    _allow("grep 'a`b' file", monkeypatch)


def test_unterminated_double_quote_denies_fail_closed(monkeypatch):
    # An unbalanced quote must fail CLOSED rather than guess at the
    # intended shell parse.
    result = _deny('grep foo "unterminated', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unbalanced/unterminated quote" in reason


def test_unterminated_single_quote_denies_fail_closed(monkeypatch):
    result = _deny("grep foo 'unterminated", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unbalanced/unterminated quote" in reason


def test_newline_in_command_denies_regardless_of_quoting(monkeypatch):
    # Newline is NOT made quote-aware by this fix -- it denies
    # unconditionally, exactly as before.
    result = _deny('grep "foo\nbar" file', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_nested_mixed_quoting_concatenation_allows(monkeypatch):
    # One word built from unquoted+quoted+unquoted segments -- a real shell
    # concatenates adjacent quoted/unquoted runs into ONE word, so the
    # quoted "|" here is literal data, not a pipe.
    _allow('grep a"|"b file', monkeypatch)


def test_quote_adjacent_unquoted_pipe_denies(monkeypatch):
    # An unquoted operator immediately following a closing single quote is
    # still a live operator -- adjacency to a quote does not suppress it.
    result = _deny("grep 'a'|'b' file", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_quote_adjacent_unquoted_pipe_after_double_quote_denies(monkeypatch):
    result = _deny('grep "a"|b file', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_ansi_c_quoting_escaped_apostrophe_denies_pinned_limitation(monkeypatch):
    # Pinned limitation (review Finding 2): $'...' ANSI-C quoting is NOT
    # recognized as its own quote form -- a `'` is always treated as a
    # plain POSIX single-quote, so the escaped `\'` inside this ANSI-C
    # string closes the scanner's inferred quote span one character sooner
    # than real bash's ANSI-C parse, and the trailing `;` is (wrongly, but
    # safe-direction-only) flagged as live. This is an intentional,
    # documented limitation, not an aspiration -- if a future parser change
    # makes this ALLOW instead, that is a deliberate decision to revisit
    # the negative spec in _scan_for_unquoted_metacharacter's docstring,
    # not a silent regression to shrug off.
    result = _deny(r"grep $'a\'; rm -rf /'", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_backslash_newline_line_continuation_denies_unconditionally(monkeypatch):
    # Real multi-line command joining via a backslash immediately followed
    # by a newline -- distinct from a literal newline inside double quotes.
    # Newline denies unconditionally regardless of context (unchanged,
    # not made quote-aware by the 2026-07-25 fix), so this must still deny.
    result = _deny("grep foo \\\nbar", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_trailing_lone_unquoted_backslash_denies_fail_closed(monkeypatch):
    # Review Finding 5 fix: a trailing unquoted backslash with nothing
    # following is an incomplete/ambiguous shell fragment -- fails closed,
    # consistent with the unterminated-quote handling.
    result = _deny("grep foo \\", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unquoted backslash" in reason


# ---------------------------------------------------------------------------
# Duty-of-care promotion (2026-07-29): unterminated-quote / trailing-
# backslash denies now name the exact unbalanced quote (or dangling
# backslash) and offer a corrected, copy-pasteable command -- the cheapest
# D-rung rows in the package promoted to a real B-rung offer instead of a
# bare "denied fail-closed" refusal.
# ---------------------------------------------------------------------------


def test_unterminated_double_quote_names_offset_and_offers_corrected_command(monkeypatch):
    result = _deny('grep foo "unterminated', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    # Still fails closed, exactly as before (verdict unchanged).
    assert "unbalanced/unterminated quote" in reason
    # New: names which quote character and where it opens.
    assert "unmatched \" opens at character 9" in reason
    # New: the corrected command is the exact original command with the
    # missing close-quote appended -- copy-pasteable, not a template.
    assert "'grep foo \"unterminated\"'" in reason


def test_unterminated_single_quote_names_offset_and_offers_corrected_command(monkeypatch):
    result = _deny("grep foo 'unterminated", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unbalanced/unterminated quote" in reason
    assert "unmatched ' opens at character 9" in reason
    assert '"grep foo \'unterminated\'"' in reason


def test_unterminated_quote_corrected_command_actually_tokenizes(monkeypatch):
    # Liveness check, not just string-shape: the offered fix must be a
    # command the shared tokenizer can actually parse -- the original
    # (denied) command cannot.
    from coordinator_core.bash_guards._command_tokenizer import tokenize_full_command

    original = 'grep foo "unterminated'
    assert tokenize_full_command(original) is None
    corrected = original + '"'
    assert tokenize_full_command(corrected) == ["grep", "foo", "unterminated"]


def test_unterminated_quote_corrected_command_survives_200_char_truncation(monkeypatch):
    """2026-07-30 M13/M19 review finding: the closing quote used to be
    appended BEFORE `_sanitize_cmd_for_reason`'s 200-char truncation, so for
    any command AT OR OVER the cap the appended quote was cut away by the
    slice and the message presented the unchanged (still-unterminated)
    original as "corrected". A command long enough to trip the cap must
    still get a corrected string that actually ENDS with the closing quote,
    not with the sanitizer's bare truncation ellipsis."""
    long_prefix = "grep " + ("x" * 250) + " \"unterminated"
    result = _deny(long_prefix, monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unbalanced/unterminated quote" in reason
    # The corrected command offered in the reason must end with the closing
    # quote -- not with a bare truncation ellipsis (quote lost to the cut).
    assert '..."\'' in reason, (
        "corrected command lost its closing quote to truncation: %r" % reason
    )


def test_trailing_backslash_offers_corrected_command(monkeypatch):
    result = _deny("grep foo \\", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unquoted backslash" in reason
    # New: the corrected command drops the dangling backslash.
    assert "'grep foo '" in reason


def test_trailing_backslash_corrected_command_actually_tokenizes(monkeypatch):
    from coordinator_core.bash_guards._command_tokenizer import tokenize_full_command

    original = "grep foo \\"
    assert tokenize_full_command(original) is None
    corrected = original[:-1]
    assert tokenize_full_command(corrected) == ["grep", "foo"]


def test_empty_command_deny_reason_names_the_fix(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("", agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "could not be parsed" in reason
    assert "Resend the Bash call with a non-empty command" in reason


def test_deny_reason_offers_resolved_absolute_path_form(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("curl https://evil.example/x", agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "<claude-klabauter-root>/coordinator/bin/coordinator-doc-new" in reason
    assert "python3 <claude-klabauter-root>/coordinator/bin/coordinator-doc-new" in reason
    # Retired forms are no longer offered.
    assert "  bin/coordinator-doc-new --type review-findings ...\n" not in reason
    assert "bash /abs/path/to/coordinator-doc-new" not in reason
    assert "  coordinator-doc-new --type review-findings ...\n" not in reason


# ---------------------------------------------------------------------------
# Tier A: read-only git/filesystem escape hatch (2026-07-25)
# ---------------------------------------------------------------------------


def _allow(cmd: str, monkeypatch) -> None:
    _confine(monkeypatch)
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    assert guard.check(payload) is None, f"expected allow for: {cmd!r}"


def _deny(cmd: str, monkeypatch) -> Dict[str, Any]:
    _confine(monkeypatch)
    payload = _payload(cmd, agent_type=_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None, f"expected deny for: {cmd!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def test_git_show_sha_allows(monkeypatch):
    _allow("git show abc1234", monkeypatch)


def test_git_diff_head_allows(monkeypatch):
    _allow("git diff HEAD~1", monkeypatch)


def test_git_log_oneline_allows(monkeypatch):
    _allow("git log --oneline -5", monkeypatch)


def test_git_show_stat_sha_allows(monkeypatch):
    _allow("git show --stat abc1234", monkeypatch)


def test_ls_directory_allows(monkeypatch):
    _allow("ls coordinator_core", monkeypatch)


def test_cat_file_allows(monkeypatch):
    _allow("cat pyproject.toml", monkeypatch)


def test_find_name_pattern_allows(monkeypatch):
    _allow("find . -name '*.py'", monkeypatch)


def test_git_commit_denies(monkeypatch):
    _deny("git commit -m x", monkeypatch)


def test_git_push_denies(monkeypatch):
    _deny("git push", monkeypatch)


def test_git_add_denies(monkeypatch):
    _deny("git add .", monkeypatch)


def test_git_checkout_branch_denies(monkeypatch):
    _deny("git checkout -b y", monkeypatch)


def test_git_dash_c_commit_denies(monkeypatch):
    # git -C <path> <subcommand> shape -- must still resolve "commit" as
    # the subcommand, not the -C value, and deny it.
    _deny("git -C /tmp commit -m x", monkeypatch)


def test_git_config_denies(monkeypatch):
    _deny("git config user.name x", monkeypatch)


def test_git_stash_denies(monkeypatch):
    _deny("git stash", monkeypatch)


def test_find_delete_denies(monkeypatch):
    _deny("find . -delete", monkeypatch)


def test_find_exec_denies(monkeypatch):
    # The literal `;` here is also a banned metacharacter -- either gate
    # firing is a correct deny; this asserts the overall deny outcome.
    _deny("find . -exec rm {} ;", monkeypatch)


def test_git_show_piped_to_tee_denies_metacharacter(monkeypatch):
    result = _deny("git show abc1234 | tee out", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_git_diff_redirected_denies_metacharacter(monkeypatch):
    result = _deny("git diff > /tmp/d", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_cat_and_rm_chained_denies_metacharacter(monkeypatch):
    result = _deny("cat x && rm y", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_ls_semicolon_git_push_denies_metacharacter(monkeypatch):
    result = _deny("ls; git push", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_rm_rf_unknown_binary_denies(monkeypatch):
    _deny("rm -rf /", monkeypatch)


def test_curl_unknown_binary_denies(monkeypatch):
    _deny("curl https://evil.example/x", monkeypatch)


def test_python3_script_unknown_binary_denies(monkeypatch):
    _deny("python3 foo.py", monkeypatch)


def test_git_suffix_bypass_denies(monkeypatch):
    # "evil-git" must not boundary-match "git" via a bare endswith() check.
    _deny("evil-git show", monkeypatch)


def test_ls_suffix_bypass_denies(monkeypatch):
    # "notls" must not boundary-match "ls" via a bare endswith() check.
    _deny("notls", monkeypatch)


# ---------------------------------------------------------------------------
# Tier A: grep addition (2026-07-25, example-doctrine-repo correction memo)
# ---------------------------------------------------------------------------


def test_grep_recursive_search_allows(monkeypatch):
    _allow('grep -rn "foo" coordinator_core', monkeypatch)


def test_grep_suffix_bypass_denies(monkeypatch):
    # "grepfoo" must not boundary-match "grep" via a bare endswith() check.
    _deny("grepfoo bar", monkeypatch)


def test_grep_redirected_denies_metacharacter(monkeypatch):
    result = _deny("grep foo bar > out.txt", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


# ---------------------------------------------------------------------------
# Tier A option-surface hardening (2026-07-25, P0 fix): git subcommand-level
# write/exec flags (--output/-o/--ext-diff) and disallowed global options
# (-c, --exec-path, --paginate/-p, --namespace, --config-env) must deny even
# though the subcommand itself is on the read-only allowlist.
# ---------------------------------------------------------------------------


def test_git_show_output_equals_denies(monkeypatch):
    result = _deny("git show --output=/tmp/x abc123", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--output=/tmp/x" in reason


def test_git_show_output_space_denies(monkeypatch):
    result = _deny("git show --output /tmp/x abc123", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--output" in reason


def test_git_log_output_equals_denies(monkeypatch):
    _deny("git log --output=/tmp/x", monkeypatch)


def test_git_diff_output_equals_denies(monkeypatch):
    _deny("git diff --output=/tmp/x", monkeypatch)


def test_git_show_dash_o_attached_denies(monkeypatch):
    result = _deny("git show -o/tmp/x abc123", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-o/tmp/x" in reason


def test_git_show_dash_o_space_denies(monkeypatch):
    result = _deny("git show -o /tmp/x abc123", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-o" in reason


def test_git_show_ext_diff_denies(monkeypatch):
    result = _deny("git show --ext-diff HEAD", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--ext-diff" in reason


def test_git_show_output_attached_no_equals_denies(monkeypatch):
    # Attached long form with no `=` -- the gap this fix closes. Not honored
    # by real git ("fatal: unrecognized argument"), but the guard must not
    # depend on a downstream parser's tolerance to stay fail-closed.
    result = _deny("git show --output/tmp/x abc", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--output/tmp/x" in reason


def test_git_log_output_attached_no_equals_denies(monkeypatch):
    _deny("git log --output/tmp/x", monkeypatch)


def test_git_diff_output_attached_no_equals_denies(monkeypatch):
    _deny("git diff --output/tmp/x", monkeypatch)


def test_git_dash_c_core_pager_denies(monkeypatch):
    result = _deny("git -c core.pager=evil log", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-c" in reason


def test_git_dash_c_diff_command_denies(monkeypatch):
    result = _deny("git -c diff.x.command=evil show HEAD", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-c" in reason


def test_git_exec_path_equals_denies(monkeypatch):
    result = _deny("git --exec-path=/tmp/evil show HEAD", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--exec-path" in reason


def test_git_exec_path_space_denies(monkeypatch):
    result = _deny("git --exec-path /tmp/evil show HEAD", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--exec-path" in reason


def test_git_paginate_denies(monkeypatch):
    result = _deny("git --paginate log", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--paginate" in reason


def test_git_dash_p_global_denies(monkeypatch):
    result = _deny("git -p log", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-p" in reason


def test_git_namespace_denies(monkeypatch):
    result = _deny("git --namespace foo show", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--namespace" in reason


def test_git_config_env_denies(monkeypatch):
    result = _deny("git --config-env=x=y log", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--config-env" in reason


# ---- Regression guards: these must NOT break ----


def test_git_log_dash_p_subcommand_position_allows(monkeypatch):
    _allow("git log -p", monkeypatch)


def test_git_log_dash_p_dash_5_allows(monkeypatch):
    _allow("git log -p -5", monkeypatch)


def test_git_show_stat_allows(monkeypatch):
    _allow("git show --stat abc1234", monkeypatch)


def test_git_diff_name_only_allows(monkeypatch):
    _allow("git diff --name-only HEAD~1", monkeypatch)


def test_git_log_oneline_dash_5_allows(monkeypatch):
    _allow("git log --oneline -5", monkeypatch)


def test_git_log_format_allows(monkeypatch):
    _allow("git log --format=%H", monkeypatch)


def test_git_show_output_indicator_new_allows(monkeypatch):
    # Not a substring match for --output -- a real, non-write git flag.
    _allow("git show --output-indicator-new=X abc1234", monkeypatch)


def test_git_show_output_indicator_old_allows(monkeypatch):
    _allow("git show --output-indicator-old=X abc1234", monkeypatch)


def test_git_show_output_indicator_context_allows(monkeypatch):
    _allow("git show --output-indicator-context=X abc1234", monkeypatch)


def test_git_dash_capital_c_path_show_allows(monkeypatch):
    _allow("git -C /some/path show abc1234", monkeypatch)


def test_git_no_pager_log_allows(monkeypatch):
    _allow("git --no-pager log", monkeypatch)


def test_git_dir_equals_status_allows(monkeypatch):
    _allow("git --git-dir=/x/.git status", monkeypatch)


def test_git_log_double_dash_output_pathspec_allows(monkeypatch):
    # `--` ends option parsing; the token after it is a pathspec (a file
    # literally named "--output=weird-filename"), not a flag.
    _allow("git log -- --output=weird-filename", monkeypatch)


# ---------------------------------------------------------------------------
# C3 gap audit (2026-07-27, docs/plans/2026-07-27-structural-policy-enforcement.md
# chunk C3): AC5 oracle audit against the CURRENT hardcoded enforcement
# surface -- ADDITIVE ONLY, no existing assertion touched. Each block below
# closes one genuinely-missing case found by cross-referencing the module's
# enforcement surface against the pre-existing 99-test suite above; see the
# executor's return message for the full audit table (surface item ->
# covered-by-test-name, or GAP -> test added here).
# ---------------------------------------------------------------------------


# ---- Gap: git read-only subcommands with no direct positive-allow test ----
# (status was only exercised indirectly via a --git-dir combo; blame,
# ls-files, rev-parse, describe had no coverage at all.)


def test_git_status_bare_allows(monkeypatch):
    _allow("git status", monkeypatch)


def test_git_blame_allows(monkeypatch):
    _allow("git blame file.py", monkeypatch)


def test_git_ls_files_allows(monkeypatch):
    _allow("git ls-files", monkeypatch)


def test_git_rev_parse_allows(monkeypatch):
    _allow("git rev-parse HEAD", monkeypatch)


def test_git_describe_allows(monkeypatch):
    _allow("git describe --tags", monkeypatch)


# ---- Gap: --work-tree global option (only -C/--git-dir/--no-pager had
# direct allow coverage; --work-tree, also in _GIT_VALUE_TAKING_OPTIONS,
# had none). ----


def test_git_work_tree_equals_allows(monkeypatch):
    _allow("git --work-tree=/x/wt status", monkeypatch)


def test_git_work_tree_space_form_allows(monkeypatch):
    _allow("git --work-tree /x/wt status", monkeypatch)


# ---- Gap: read-only fs binaries with no allow coverage at all
# (ls/cat/find/grep were covered; head/tail/wc/file/stat were not). ----


def test_head_file_allows(monkeypatch):
    _allow("head -n 20 pyproject.toml", monkeypatch)


def test_tail_file_allows(monkeypatch):
    _allow("tail -n 20 pyproject.toml", monkeypatch)


def test_wc_file_allows(monkeypatch):
    _allow("wc -l pyproject.toml", monkeypatch)


def test_file_binary_allows(monkeypatch):
    _allow(
        "file coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py",
        monkeypatch,
    )


def test_stat_file_allows(monkeypatch):
    _allow("stat pyproject.toml", monkeypatch)


# ---- Gap: 4 of the 9 metacharacters had no UNQUOTED-deny test at all
# (`;`, `&&`, `|`, `>` were covered; `||`, bare unquoted backtick, bare
# unquoted `$(`, `<`, and bare `&` were not -- command substitution was
# only exercised INSIDE double quotes, not standalone). ----


def test_double_pipe_unquoted_denies(monkeypatch):
    result = _deny("git status || rm -rf /", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_bare_unquoted_backtick_denies(monkeypatch):
    result = _deny("cat `whoami`", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_bare_unquoted_command_substitution_denies(monkeypatch):
    result = _deny("cat $(whoami)", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_unquoted_less_than_redirect_denies(monkeypatch):
    result = _deny("cat < /etc/passwd", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_bare_unquoted_ampersand_denies(monkeypatch):
    result = _deny("cat pyproject.toml &", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


# ---- Gap: the OR-resolver's SECONDARY leg (subagent_type via the
# back-pointer chain) had no test where it alone -- not the payload's
# top-level agent_type -- carries the confined _CONFINED_FINDINGS_AGENTS
# literal. Every existing confined-case test set both agent_type AND the
# monkeypatched back-pointer to the confined type together. ----


def test_secondary_leg_backpointer_confinement_denies(monkeypatch):
    # agent_type (primary leg) is a NON-confined type; only the
    # monkeypatched back-pointer subagent_type (secondary leg) is confined.
    # The OR-resolver must still treat this as a confined findings-agent.
    _confine(monkeypatch, subagent_type=_CONFINED_TYPE)
    payload = _payload("rm -rf /", agent_type=_NON_CONFINED_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Divergence 8 (2026-07-28): quote-aware pipe-vs-data was already correct
# (see the "Quote-aware metacharacter gate" section above); the real false
# positives reported by two independent code-reviewer dispatches were (a) a
# top-level UNQUOTED pipe was an unconditional deny even when every segment
# was independently allowlisted (`git show <rev> | wc -c`), and (b) a plain
# `2>/dev/null` redirect was an unconditional deny. Both are narrow
# carve-outs: a pipeline allows only when EVERY segment is independently
# Tier-A-allowlisted; a redirect allows only when it targets exactly
# /dev/null. Everything else -- command substitution, `;`/`&&`/`||`
# chaining into a non-allowlisted command, redirection to any other path,
# backgrounding -- is unchanged and still denies.
# ---------------------------------------------------------------------------


def test_git_show_piped_to_wc_allows(monkeypatch):
    # Reviewer B's reported false positive: an all-allowlisted pipeline
    # (git show -> wc) must now allow instead of forcing two separate calls.
    _allow("git show abc1234:foo.py | wc -c", monkeypatch)


def test_find_piped_to_wc_allows(monkeypatch):
    _allow('find . -name "*.py" | wc -l', monkeypatch)


def test_cat_piped_to_git_log_allows(monkeypatch):
    # Both pipeline members are Tier A (one filesystem binary, one git
    # read-only subcommand) -- order/mix of the two Tier A families must not
    # matter.
    _allow("cat pyproject.toml | git log --format='%h'", monkeypatch)


def test_git_show_piped_to_tee_denies_non_allowlisted_segment(monkeypatch):
    # tee writes to disk and is not on the Tier A allowlist -- the pipeline
    # must still deny even though the FIRST segment (git show) is clean.
    result = _deny("git show abc1234 | tee out", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pipeline segment" in reason
    assert "'tee out'" in reason


def test_git_show_piped_to_rm_denies_non_allowlisted_segment(monkeypatch):
    result = _deny("git show abc1234 | rm -rf /", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pipeline segment" in reason
    assert "'rm -rf /'" in reason


def test_double_pipe_still_denies_unconditionally(monkeypatch):
    # `||` is a distinct 2-char metacharacter (chaining on failure), not a
    # single pipe -- the pipeline carve-out must not weaken this.
    result = _deny("git status || rm -rf /", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_grep_stderr_redirect_to_devnull_allows(monkeypatch):
    # Reviewer B's other reported false positive.
    _allow("grep foo bar.py 2>/dev/null", monkeypatch)


def test_grep_stdout_redirect_to_devnull_allows(monkeypatch):
    _allow("grep foo bar.py >/dev/null", monkeypatch)


def test_grep_spaced_redirect_to_devnull_allows(monkeypatch):
    _allow("grep foo bar.py > /dev/null", monkeypatch)


def test_git_diff_redirect_to_real_path_still_denies(monkeypatch):
    # Redirection to anything OTHER than /dev/null is still a write vector
    # and must still deny -- the carve-out is exact-target-only.
    result = _deny("git diff > /tmp/d", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_devnull_redirect_then_chained_command_still_denies(monkeypatch):
    # The devnull carve-out only exempts the redirect itself -- a `;`
    # chaining a second, non-allowlisted command after it must still deny.
    result = _deny("echo hi > /dev/null; git push", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


# ---------------------------------------------------------------------------
# Divergence 10 (2026-08-01): the exact stderr-to-stdout fd-duplication
# idiom (`2>&1`) is write-incapable (duplicates an already-open fd, opens no
# file) and exec-incapable (starts no process) -- a sibling carve-out to the
# `/dev/null` exemption immediately above, on identical reasoning. PM ruling:
# this allowlist is about standards/coherence for a Bash-shaped tool, not an
# adversarial security boundary.
# ---------------------------------------------------------------------------


def test_stderr_to_stdout_redirect_allows(monkeypatch):
    _allow("grep foo bar.py 2>&1", monkeypatch)


def test_stdout_to_stderr_redirect_allows(monkeypatch):
    # Mirror image of 2>&1 -- falls out of the same symmetric check for free.
    _allow("grep foo bar.py 1>&2", monkeypatch)


def test_stderr_to_stdout_redirect_then_pipe_allows(monkeypatch):
    # The fd-dup carve-out only exempts the redirect itself -- a trailing
    # top-level `|` into an allowlisted segment must still independently
    # satisfy the pipeline carve-out (Divergence 8).
    _allow("grep foo bar.py 2>&1 | wc -l", monkeypatch)


def test_bare_ampersand_backgrounding_still_denies(monkeypatch):
    result = _deny("git status &", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_double_ampersand_chaining_still_denies(monkeypatch):
    result = _deny("git status && git push", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_semicolon_still_denies(monkeypatch):
    result = _deny("git status; git push", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_pipe_to_non_allowlisted_still_denies(monkeypatch):
    result = _deny("git show HEAD | rm -rf /", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pipeline segment" in reason


def test_redirect_to_real_file_still_denies(monkeypatch):
    result = _deny("git diff > /tmp/evil", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_general_ampersand_digit_not_exact_fd_dup_token_still_denies(monkeypatch):
    # `3>&1` is NOT the exact stderr-to-stdout (`2>&1`) or stdout-to-stderr
    # (`1>&2`) token this carve-out exempts -- the leading digit is neither
    # complement in `_FD_DUP_COMPLEMENT`, so this must still fall through to
    # the unconditional `>` deny.
    result = _deny("git diff 3>&1", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_ampersand_digit_no_redirect_prefix_still_denies(monkeypatch):
    # A bare `&2` with no preceding `>` at all is not this carve-out's shape
    # -- the unquoted `&` alone still denies unconditionally.
    result = _deny("git diff &2", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_command_substitution_in_pipeline_segment_still_denies(monkeypatch):
    # Command substitution must still deny even when it appears inside what
    # would otherwise be an allowlisted pipeline segment.
    result = _deny("git show HEAD $(whoami) | wc -c", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shell-chaining metacharacter" in reason


def test_staging_commit_push_still_deny_with_pipe_present(monkeypatch):
    # Belt-and-braces: a pipeline whose segments are themselves staging/
    # commit/push commands must still deny -- neither is a Tier A binary.
    _deny("git add . | git commit -m x", monkeypatch)
    _deny("git push origin main | cat", monkeypatch)


# ---------------------------------------------------------------------------
# 2026-07-29 P0 fix: this guard's own private ``_extract_first_token``/
# ``_strip_prefix_and_split`` (and ``_git_command_tokens``'s naive
# ``rest.split()``) were quote-BLIND, the same shape just retired from
# ``block_subagent_commit.py`` (bf83ef36). Confirmed live through
# ``check()``: a QUOTED write-flag argument tokenized with its quote
# characters still attached, so ``_helpers.prefix_denies``'s
# ``startswith()`` check never matched -- a total defeat of the Tier A
# option-hardening (Divergence 5) this guard exists to enforce, since a
# real shell strips the quotes and git genuinely writes to the arbitrary
# target either way. Fixed by rebuilding tokenization on the same
# shlex-based ``_command_tokenizer`` machinery ``block_subagent_commit.py``
# uses, plus porting that module's Windows argv0-head normalization
# (generalized here to the policy-resolved ruleset's own binary names,
# since this guard's allowlist is policy-driven, not a fixed pair).
# ---------------------------------------------------------------------------


def test_double_quoted_output_flag_denies(monkeypatch):
    # Red case: confirmed live ALLOW before the fix.
    _deny('git show "--output=/tmp/evil" HEAD', monkeypatch)


def test_single_quoted_output_flag_denies(monkeypatch):
    # Red case: confirmed live ALLOW before the fix.
    _deny("git show '--output=/tmp/evil' HEAD", monkeypatch)


def test_single_quoted_ext_diff_flag_denies(monkeypatch):
    _deny("git show '--ext-diff' HEAD", monkeypatch)


def test_double_quoted_dash_o_short_flag_denies(monkeypatch):
    _deny('git log "-o/tmp/evil"', monkeypatch)


def test_quoted_c_global_option_denies(monkeypatch):
    _deny('git "-c" "core.pager=evil" log', monkeypatch)


def test_quoted_exec_path_global_option_denies(monkeypatch):
    _deny('git "--exec-path=/tmp/evil" show HEAD', monkeypatch)


def test_quoted_find_exec_flag_denies(monkeypatch):
    # Same quote-blindness class in the Tier A fs-binary write-flag scan
    # (_has_find_write_flag previously used a bare cmd.split()).
    _deny('find . -type f "-exec" rm -rf {} +', monkeypatch)


def test_unquoted_output_flag_still_denies(monkeypatch):
    # Pre-existing coverage, re-asserted as a same-file positive control:
    # the unquoted form must still deny exactly as before this fix.
    _deny("git show --output=/tmp/evil HEAD", monkeypatch)


def test_quoted_write_flag_in_pipeline_segment_denies(monkeypatch):
    # The pipeline-segment evaluator (_segment_is_tier_a_allowlisted) shares
    # the same tokenizer -- confirm the quoted-flag bypass is closed there
    # too, not just in the single-command path.
    _deny('git show "--output=/tmp/evil" HEAD | wc -c', monkeypatch)


def test_quoted_legitimate_arg_still_allows(monkeypatch):
    # Negative control: quoting a harmless argument must not newly deny --
    # the fix must not widen the boundary in the other direction.
    _allow('git show HEAD -- "some file.py"', monkeypatch)


def test_quoted_output_indicator_flag_still_allows(monkeypatch):
    # Negative control: --output-indicator-* is a real, non-write git
    # formatting flag exempted by the hyphen-boundary rule in
    # _helpers.prefix_denies -- must stay allowed even quoted.
    _allow('git log "--output-indicator-new=+"', monkeypatch)


# --- Windows argv0-head normalization (ported from block_subagent_commit.py) ---


def test_windows_spaced_username_git_exe_allows(monkeypatch):
    # A spaced Windows username is the DEFAULT profile shape on this
    # project's primary platform, not an exotic construction.
    cmd = r"C:\Users\John Doe\Git\bin\git.exe show HEAD"
    _allow(cmd, monkeypatch)


def test_windows_spaced_username_coordinator_doc_new_cmd_allows(monkeypatch):
    cmd = (
        r"C:\Users\John Doe\.coordinator-claude-settings\bin\coordinator-doc-new.cmd"
        " --type review-findings"
    )
    _allow(cmd, monkeypatch)


def test_windows_quoted_spaced_username_git_allows(monkeypatch):
    cmd = r'"C:\Users\John Doe\Git\bin\git.exe" show HEAD'
    _allow(cmd, monkeypatch)


def test_windows_plain_backslash_git_no_space_allows(monkeypatch):
    cmd = r"C:\Git\bin\git.exe show HEAD"
    _allow(cmd, monkeypatch)


def test_windows_evil_lookalike_directory_still_denies(monkeypatch):
    # Negative control: the Windows normalization must not widen the
    # path-separator boundary -- a directory component that merely
    # CONTAINS "git" as a substring, with no separator immediately before
    # the literal binary name, must still deny.
    cmd = r"C:\Users\evilgit\tool.exe show HEAD"
    _deny(cmd, monkeypatch)


# ---------------------------------------------------------------------------
# Amendment 2 (2026-08-03, PM ruling): "bash confinement should only be for
# destructive actions that would degrade a machine." coordinator:code-
# reviewer gains the SAME python3 -m pytest module allowance
# coordinator:executor already held (Divergence 9) -- both confined types
# hold an unconfined Edit tool, so denying pytest to one and not the other
# bought no containment (see the guard module's own Amendment 2 docstring
# entry). This is the regression this dispatch fixes: pinned here to fail
# against the pre-Amendment-2 module, per _CONFINED_TYPE ==
# "coordinator:code-reviewer" (defined at the top of this file).
# ---------------------------------------------------------------------------


def test_confined_python3_dash_m_pytest_now_allows(monkeypatch):
    # The regression this dispatch fixes: this must fail (deny) before
    # Amendment 2's ruleset override lands, and pass (allow) after.
    _allow("python3 -m pytest -q", monkeypatch)


def test_confined_python3_dash_m_pytest_with_stderr_redirect_allows(monkeypatch):
    # Sibling of the executor's own coverage for the trailing 2>&1 idiom
    # (Divergence 10) -- confirms the fd-duplication carve-out composes with
    # the new pytest allowance for this type too.
    _allow("python3 -m pytest -q 2>&1", monkeypatch)


def test_confined_python3_dash_c_inline_code_still_denies(monkeypatch):
    # The pytest module allowance must not be mistaken for a general
    # interpreter passthrough -- -c/-e stay unconditionally denied
    # (_PY_INLINE_CODE_FLAGS is a bare module constant, not a ruleset
    # lookup, so no per-type override can re-admit it).
    result = _deny('python3 -c "import os; os.system(\'rm -rf /\')"', monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "-c" in reason


def test_confined_python3_dash_e_inline_code_still_denies(monkeypatch):
    _deny('python3 -e "print(1)"', monkeypatch)


def test_confined_python3_dash_m_unlisted_module_still_denies(monkeypatch):
    # Only pytest is on this type's interpreter_allowed_modules -- an
    # unlisted module still denies exactly as before Amendment 2.
    result = _deny("python3 -m http.server", monkeypatch)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "http.server" in reason


def test_confined_python3_script_path_still_denies(monkeypatch):
    # interpreter_allow_scripts is NOT extended to coordinator:code-reviewer
    # by Amendment 2 -- only interpreter_allowed_modules is. A bare script
    # path still denies for this type (still allowed for coordinator:
    # executor, per test_python3_script_path_allows in
    # test_executor_bash_confinement.py).
    _deny("python3 myscript.py", monkeypatch)
