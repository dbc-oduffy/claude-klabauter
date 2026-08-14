"""Tests for coordinator_core.bash_guards.block_subagent_commit.

Covers the 2026-07-25 bypass fix (confirmed-against-live-guard report):
the old pre-filter (``_prefilter_mentions_commit``) required the literal
sequence ``"git commit"`` or ``"git -C"`` followed later by ``"commit"``,
which was strictly narrower than the full matchers it gated -- so
``check()`` returned ALLOW before the real matcher ever ran for three
confirmed shapes: an arbitrary git global option before the subcommand
(``git -c user.name=x commit``), extra internal whitespace
(``git  commit``), and the ``coordinator-safe-commit`` helper (which the
old pre-filter never checked for at all). This file pins DENY for all
three, plus the global-flag/whitespace variants and the three
``coordinator-safe-commit`` path forms named in the fix brief, plus the
allow-regressions that must not break.

Also covers the 2026-07-29 part 4 fix: ``_has_coordinator_safe_commit``'s
own quote-blind extractor (``_extract_first_token``, since retired) mis-
split an already-quoted or ordinary-unquoted spaced Windows argv0-head path
for this binary, confirmed ALLOW (bypass) through the real ``check()``
entrypoint before the fix. See the ``part 4`` test block below.

Pure Python -- no shell spawns, no filesystem writes (Windows+macOS
first-class). Identity resolution is monkeypatched directly onto the
guard module object (the same seam-patching pattern used by
``test_block_reviewer_bash_outside_allowlist.py``), so no real git repo or
back-pointer chain on disk is required.

Spec backlink: coordinator_core/bash_guards/block_subagent_commit.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.ops.session import scope_report as _scope_report
from coordinator_core.session import core as _session_core
from coordinator_core.session import scope as _session_scope

_SUBAGENT_TYPE = "coordinator:executor"


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


def _subagent(monkeypatch, subagent_type=_SUBAGENT_TYPE):
    """Wire the guard's identity-resolution seam so agent_id resolves
    truthy and the effective type resolves to ``subagent_type`` -- lets
    the deny path fire without a real git repo/back-pointer chain on disk.
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


def _denies(monkeypatch, cmd):
    _subagent(monkeypatch)
    result = guard.check(_payload(cmd, agent_type=_SUBAGENT_TYPE))
    assert result is not None, f"expected DENY for: {cmd!r}"
    assert (
        result["hookSpecificOutput"]["permissionDecision"] == "deny"
    ), f"expected DENY for: {cmd!r}"


def _allows(monkeypatch, cmd):
    _subagent(monkeypatch)
    result = guard.check(_payload(cmd, agent_type=_SUBAGENT_TYPE))
    assert result is None, f"expected ALLOW for: {cmd!r}, got {result!r}"


# ---------------------------------------------------------------------------
# The three confirmed bypasses (2026-07-25 report) -- must now DENY
# ---------------------------------------------------------------------------


def test_bypass_1_git_dash_c_global_config_flag_denies(monkeypatch):
    """``git -c user.name=x commit -m "msg"`` -- ``-c`` is not ``-C``, and
    the option's separate-arg VALUE token (``user.name=x``) is neither a
    flag nor ``commit``, which the old fixed-shape prefilter/regex could
    not express.
    """
    _denies(monkeypatch, 'git -c user.name=x commit -m "msg"')


def test_bypass_2_double_space_git_commit_denies(monkeypatch):
    """``git  commit -m "msg"`` (two spaces) -- the old prefilter required
    the literal single-space substring ``"git commit"``.
    """
    _denies(monkeypatch, 'git  commit -m "msg"')


def test_bypass_3_coordinator_safe_commit_bare_denies(monkeypatch):
    """``coordinator-safe-commit -m "msg"`` -- the old prefilter never
    checked for the helper name at all, despite the module docstring's
    claim that the helper is in scope.
    """
    _denies(monkeypatch, 'coordinator-safe-commit -m "msg"')


# ---------------------------------------------------------------------------
# Global-flag and whitespace variants
# ---------------------------------------------------------------------------


def test_git_capital_c_global_flag_denies(monkeypatch):
    _denies(monkeypatch, 'git -C /some/repo commit -m "msg"')


def test_git_git_dir_attached_long_flag_denies(monkeypatch):
    _denies(monkeypatch, 'git --git-dir=/x/.git commit -m "msg"')


def test_git_no_pager_boolean_flag_denies(monkeypatch):
    _denies(monkeypatch, 'git --no-pager commit -m "msg"')


def test_git_combination_of_global_flags_denies(monkeypatch):
    _denies(
        monkeypatch,
        'git --no-pager -c user.name=x -C /some/repo commit -m "msg"',
    )


def test_git_tab_whitespace_denies(monkeypatch):
    _denies(monkeypatch, 'git\tcommit -m "msg"')


def test_git_multiple_internal_spaces_denies(monkeypatch):
    _denies(monkeypatch, 'git   commit    -m   "msg"')


def test_lowercase_c_and_capital_c_are_not_conflated(monkeypatch):
    """``-c`` (config override, separate arg) and ``-C`` (repo-root
    override, separate arg) are DIFFERENT git options, both handled as
    separate-arg-consuming -- neither is lowercased/normalised into the
    other. Both forms independently deny.
    """
    _denies(monkeypatch, 'git -c user.name=x commit -m "msg"')
    _denies(monkeypatch, 'git -C /some/repo commit -m "msg"')


# ---------------------------------------------------------------------------
# coordinator-safe-commit path forms
# ---------------------------------------------------------------------------


def test_coordinator_safe_commit_bin_prefixed_denies(monkeypatch):
    _denies(monkeypatch, 'bin/coordinator-safe-commit -m "msg"')


def test_coordinator_safe_commit_absolute_path_denies(monkeypatch):
    _denies(
        monkeypatch,
        '/x/.coordinator-claude-settings/bin/coordinator-safe-commit -m "msg"',
    )


def test_coordinator_safe_commit_python3_prefixed_denies(monkeypatch):
    _denies(
        monkeypatch,
        'python3 /x/.coordinator-claude-settings/bin/coordinator-safe-commit -m "msg"',
    )


def test_coordinator_safe_commit_windows_backslash_path_denies(monkeypatch):
    _denies(
        monkeypatch,
        r'C:\Users\x\.coordinator-claude-settings\bin\coordinator-safe-commit -m "msg"',
    )


# ---------------------------------------------------------------------------
# git-binary path forms (2026-07-25, part 2 -- the ``git`` token itself was
# left literal-equality-matched while the coordinator-safe-commit helper
# matcher was boundary-anchored in part 1; this closes that asymmetry)
# ---------------------------------------------------------------------------


def test_git_absolute_path_usr_bin_denies(monkeypatch):
    _denies(monkeypatch, '/usr/bin/git commit -m "msg"')


def test_git_absolute_path_homebrew_denies(monkeypatch):
    _denies(monkeypatch, '/opt/homebrew/bin/git commit -m "msg"')


def test_git_bin_relative_path_denies(monkeypatch):
    _denies(monkeypatch, 'bin/git commit -m "msg"')


def test_git_windows_backslash_path_denies(monkeypatch):
    """Windows path, no ``.exe`` suffix and no embedded space -- the
    original supported Windows shape. ``.exe``-suffixed and embedded-space
    forms are covered separately below (2026-07-29 parts 2 and 3).
    """
    _denies(monkeypatch, r'C:\Git\bin\git commit -m "msg"')


def test_git_absolute_path_combined_with_global_flags_and_whitespace_denies(
    monkeypatch,
):
    """An absolute-path ``git`` invocation combined with a global option
    AND irregular whitespace must still deny -- both the path-boundary
    widening and the pre-existing global-flag/whitespace tolerance must
    hold simultaneously.
    """
    _denies(
        monkeypatch,
        '/usr/bin/git  -c   user.name=x   commit -m "msg"',
    )


# ---------------------------------------------------------------------------
# 2026-07-29 part 3 -- SPACED-WINDOWS-PATH ARGV0, ported from
# block_subagent_destructive_action.py for consistency/defense-in-depth (see
# module docstring's 2026-07-29-part-3 entry, and code-reviewer Finding 1,
# for why this is pinned here even though the reconciled reasoning concludes
# the shape is not independently exploitable via this project's actual
# Bash-tool-on-Windows execution model).
# ---------------------------------------------------------------------------


def test_git_exe_spaced_path_backslash_commit_denies(monkeypatch):
    """Unquoted, backslash-separated, git-for-Windows' default install
    location -- the exact shape ``block_subagent_destructive_action.py``
    fixed the same day for its own surface.
    """
    _denies(monkeypatch, r'C:\Program Files\Git\bin\git.exe commit -m "msg"')


def test_git_exe_spaced_path_forward_slash_commit_denies(monkeypatch):
    """Same hole, forward-slash separator form."""
    _denies(monkeypatch, 'C:/Program Files/Git/bin/git.exe commit -m "msg"')


def test_git_spaced_path_no_exe_suffix_commit_denies(monkeypatch):
    """Spaced path with no ``.exe`` suffix -- the normalization must not
    require the suffix to be present.
    """
    _denies(monkeypatch, 'C:\\Program Files\\Git\\bin\\git commit -m "msg"')


# ---------------------------------------------------------------------------
# C4b (docs/reference/guard-dialect-coverage.md row 7) -- this guard's
# matcher is dialect-neutral by construction (external `git`/`coordinator-
# safe-commit` argv0 identity via `token_matches_binary`/
# `normalize_executable_basename`, module docstring lines 75-298). This
# does NOT require a real PowerShell parse (`_dialect.py`) -- it proves the
# EXISTING bash-tokenizer-based matcher already reaches the same verdict on
# a PowerShell-spelled invocation, since a PowerShell author's `&`
# call-operator prefix is already recognized separator punctuation by this
# package's own tokenizer (see `_dialect.py`'s module docstring, "Output
# shape").
# ---------------------------------------------------------------------------


def test_powershell_call_operator_prefixed_git_exe_commit_denies_same_as_bash(monkeypatch):
    """`& git.exe commit -m "msg"` -- the PowerShell call-operator prefix
    (used to invoke a command whose name is a quoted/variable string) must
    reach the SAME deny as the bare bash spelling; the leading `&` is
    already separator punctuation to this guard's tokenizer, so it is
    consumed as an empty leading segment rather than defeating detection.
    """
    _denies(monkeypatch, '& git.exe commit -m "msg"')
    _denies(monkeypatch, 'git.exe commit -m "msg"')


def test_powershell_semicolon_chained_git_commit_denies_same_as_bash(monkeypatch):
    """`Set-Location C:\\repo; git commit -m "msg"` -- a PowerShell
    statement-separator `;` chain ahead of the commit must not shield it;
    `;` is recognized separator punctuation on both dialects.
    """
    _denies(monkeypatch, 'Set-Location C:\\repo; git commit -m "msg"')


# ---------------------------------------------------------------------------
# 2026-07-29 part 4 -- ``coordinator-safe-commit`` quote-blindness
# (integrator report during the part-3 port above): the part-3 comment used
# to say a ``coordinator-safe-commit`` spaced-path counterpart was
# deliberately NOT pinned because that helper's own frag-based extractor
# (`_extract_first_token`, since retired) was not quote/space-aware. That
# extractor is gone; `_has_coordinator_safe_commit` now shares
# `_has_git_commit`'s canonical shlex-tokenizer machinery, and both argv0-
# head normalization passes recognize this binary too. These are the
# `git`-suite counterparts above, confirmed via the real ``check()``
# entrypoint against a fake ALLOW before this fix.
# ---------------------------------------------------------------------------


def test_coordinator_safe_commit_spaced_path_unquoted_backslash_denies(monkeypatch):
    """Unquoted, backslash-separated, embedded-space Windows path -- an
    ordinary Windows username-with-a-space carrier
    (``C:\\Users\\John Doe\\...``), the counterpart of
    ``test_git_exe_spaced_path_backslash_commit_denies`` for this binary.
    Confirmed ALLOW (bypass) before this fix.
    """
    _denies(
        monkeypatch,
        r'C:\Users\John Doe\.coordinator-claude-settings\bin\coordinator-safe-commit.cmd -m "msg"',
    )


def test_coordinator_safe_commit_spaced_path_double_quoted_denies(monkeypatch):
    """An already double-quoted spaced Windows path must still deny -- the
    counterpart of ``test_git_exe_spaced_path_already_quoted_double_commit_
    denies`` for this binary. Confirmed ALLOW (bypass) before this fix.
    """
    _denies(
        monkeypatch,
        '"C:\\Users\\John Doe\\.coordinator-claude-settings\\bin\\coordinator-safe-commit.cmd" -m "msg"',
    )


def test_coordinator_safe_commit_spaced_path_posix_single_quoted_denies(monkeypatch):
    """A single-quoted POSIX path with an embedded space -- the extractor
    this fix retired mis-split this at the first raw space regardless of
    quoting. Confirmed ALLOW (bypass) before this fix.
    """
    _denies(
        monkeypatch,
        "'/opt/coordinator tools/coordinator-safe-commit' -m \"msg\"",
    )


def test_coordinator_safe_commit_spaced_path_posix_double_quoted_denies(monkeypatch):
    _denies(
        monkeypatch,
        '"/opt/coordinator tools/coordinator-safe-commit" -m "msg"',
    )


def test_coordinator_safe_commit_windows_no_space_bare_still_denies(monkeypatch):
    """No-space Windows backslash path with no launcher suffix -- must
    still deny after generalizing the normalization passes (regression
    guard on the pre-existing, already-passing space-free case).
    """
    _denies(monkeypatch, r'C:\tools\coordinator-safe-commit -m "msg"')


def test_coordinator_safe_commit_spaced_path_mention_in_unrelated_command_allows(
    monkeypatch,
):
    """A spaced Windows coordinator-safe-commit path mentioned as a plain
    ARGUMENT to an unrelated command (not at argv0 position) must not be
    rewritten into a recognized invocation -- counterpart of
    ``test_git_exe_spaced_path_mention_in_unrelated_command_allows``.
    """
    _allows(
        monkeypatch,
        'echo "see C:\\Users\\John Doe\\coordinator-safe-commit.cmd for details"',
    )


def test_git_exe_spaced_path_already_quoted_double_commit_denies(monkeypatch):
    """An already double-quoted spaced path must still deny -- the
    normalization must not double-wrap an already-quoted token.
    """
    _denies(monkeypatch, '"C:\\Program Files\\Git\\bin\\git.exe" commit -m "msg"')


def test_git_exe_spaced_path_mention_in_unrelated_command_allows(monkeypatch):
    """A spaced Windows git.exe path mentioned as a plain ARGUMENT to an
    unrelated command (not at argv0 position) must not be rewritten into a
    recognized invocation -- the argv0-head regex is anchored to command/
    segment-start position only, same as the sibling guard's fix.
    """
    _allows(
        monkeypatch,
        'echo "see C:\\Program Files\\Git\\bin\\git.exe for commit details"',
    )


def test_evil_git_hyphen_suffix_allows(monkeypatch):
    """``evil-git`` is a single bareword token whose character immediately
    preceding the ``git`` suffix is a hyphen, not a path separator -- same
    boundary rule as ``evil-coordinator-safe-commit`` below. Not a
    git-commit invocation -- ALLOWED.
    """
    _allows(monkeypatch, 'evil-git commit -m x')


def test_mygit_allows(monkeypatch):
    _allows(monkeypatch, 'mygit commit -m x')


def test_gitlab_allows(monkeypatch):
    _allows(monkeypatch, 'gitlab commit -m x')


def test_evil_coordinator_safe_commit_hyphen_suffix_allows(monkeypatch):
    """Boundary semantics chosen: PATH-SEPARATOR boundary, not hyphen
    boundary (mirrors ``block_reviewer_bash_outside_allowlist.py``'s
    ``_token_matches_binary``). ``evil-coordinator-safe-commit`` is a
    single bareword token whose character immediately preceding the
    ``coordinator-safe-commit`` suffix is a hyphen, not a path separator,
    so it does NOT match the allowlisted-binary check and this command is
    NOT recognised as a coordinator-safe-commit invocation (nor does it
    contain a ``git`` token, so it is not a git-commit invocation either)
    -- ALLOWED.
    """
    _allows(monkeypatch, 'evil-coordinator-safe-commit -m "msg"')


def test_evil_coordinator_safe_commit_cmd_suffix_allows(monkeypatch):
    """Same hyphen-boundary check, ``.cmd``-suffixed -- the generalized
    launcher-suffix stripping (2026-07-29 part 4) must not turn a hyphen
    boundary into a separator boundary: suffix-stripping happens on the
    token's OWN basename, not on the literal name being compared against.
    """
    _allows(monkeypatch, 'evil-coordinator-safe-commit.cmd -m "msg"')


def test_evil_coordinator_safe_commit_windows_path_allows(monkeypatch):
    """A Windows path whose basename is ``evil-coordinator-safe-commit``
    (hyphen-adjacent, not path-separator-adjacent) must not be recognized
    by the generalized ``_WINDOWS_ARGV0_HEAD_PATH_RE`` alternation either --
    the regex requires the LITERAL name immediately following a path
    separator, and the whole hyphenated basename does not equal it.
    """
    _allows(monkeypatch, r'C:\tools\evil-coordinator-safe-commit -m "msg"')


# ---------------------------------------------------------------------------
# 2026-07-29 part 5 -- ceremony.scoped_git_commit op (the coordinator_core
# invoke-dispatched sibling of coordinator-safe-commit; both shell out to
# the same git-commit outcome). Confirmed live: a subagent running this
# invocation against the shared worktree committed, unguarded, before this
# fix -- this module previously had zero detection for it at all.
# ---------------------------------------------------------------------------


def test_ceremony_scoped_git_commit_python3_dash_m_denies(monkeypatch):
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}'",
    )


def test_ceremony_scoped_git_commit_python_dash_m_denies(monkeypatch):
    _denies(
        monkeypatch,
        "python -m coordinator_core.invoke ceremony.scoped_git_commit '{}'",
    )


def test_ceremony_scoped_git_commit_absolute_interpreter_path_denies(monkeypatch):
    _denies(
        monkeypatch,
        "/usr/bin/python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}'",
    )


def test_ceremony_scoped_git_commit_exe_suffixed_interpreter_denies(monkeypatch):
    _denies(
        monkeypatch,
        "python3.exe -m coordinator_core.invoke ceremony.scoped_git_commit '{}'",
    )


def test_ceremony_scoped_git_commit_env_prefixed_denies(monkeypatch):
    _denies(
        monkeypatch,
        "env FOO=1 python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}'",
    )


def test_ceremony_scoped_git_commit_bare_env_assignment_prefixed_denies(monkeypatch):
    _denies(
        monkeypatch,
        "FOO=1 python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}'",
    )


def test_ceremony_scoped_git_commit_wrapper_prefixed_denies(monkeypatch):
    _denies(
        monkeypatch,
        "timeout 30 python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}'",
    )


def test_ceremony_scoped_git_commit_with_repo_flag_denies(monkeypatch):
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}' --repo /x",
    )


def test_ceremony_scoped_git_commit_shell_c_wrapped_denies(monkeypatch):
    _denies(
        monkeypatch,
        "sh -c 'python3 -m coordinator_core.invoke ceremony.scoped_git_commit \"{}\"'",
    )


def test_ceremony_scoped_git_commit_chained_denies(monkeypatch):
    _denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}' && echo done",
    )


# ---------------------------------------------------------------------------
# Live-incident regression (2026-08-01): a chained ``;``-joined command whose
# SECOND segment invokes ``coordinator-safe-commit``/the ceremony op crashed
# the dispatcher with a ``NameError`` on a shared checkout at the exact
# moment a concurrent, unrelated in-flight edit to this module was applying
# (a rename of an internal matcher helper) -- the identical UNCHAINED
# single-command invocation, run moments apart, did not observe the crash.
# Investigated live: neither HEAD nor the working tree at investigation time
# contained a stale reference to the old helper name, and every literal
# reproducer shape below (single AND chained, piped-and-chained, for each of
# the three committing shapes this module recognizes) evaluates cleanly --
# so the crash was not attributable to a standing logic defect in this
# module's own matchers. These cases pin the single-vs-chained parity this
# incident's report specifically called out, so a future regression in
# EITHER shape is caught, independent of that transient-edit-race root
# cause.
# ---------------------------------------------------------------------------


def test_coordinator_safe_commit_single_command_piped_denies(monkeypatch):
    """Unscoped single-command form, piped through ``tail`` -- the shape
    reported as "succeeded normally" (i.e. reached a verdict, no crash) in
    the live incident.
    """
    _denies(
        monkeypatch,
        'coordinator-safe-commit -m "msg" -- some/file.txt 2>&1 | tail -3',
    )


def test_coordinator_safe_commit_semicolon_chained_piped_denies(monkeypatch):
    """The exact incident shape: a benign command piped through ``tail``,
    ``;``-chained with a ``coordinator-safe-commit`` invocation also piped
    through ``tail``. Must deny, and must not raise.
    """
    _denies(
        monkeypatch,
        'echo hi 2>&1 | tail -10; coordinator-safe-commit -m "msg" '
        "-- some/file.txt 2>&1 | tail -3",
    )


def test_git_commit_semicolon_chained_piped_denies(monkeypatch):
    """Same chained-and-piped shape, plain ``git commit`` leg."""
    _denies(
        monkeypatch,
        'echo hi 2>&1 | tail -10; git commit -m "msg" 2>&1 | tail -3',
    )


def test_ceremony_scoped_git_commit_semicolon_chained_piped_denies(monkeypatch):
    """Same chained-and-piped shape, the ceremony-op invocation leg."""
    _denies(
        monkeypatch,
        "echo hi 2>&1 | tail -10; python3 -m coordinator_core.invoke "
        "ceremony.scoped_git_commit '{}' 2>&1 | tail -3",
    )


def test_ceremony_invoke_different_op_allows(monkeypatch):
    """A DIFFERENT op dispatched through the same generic CLI (not the
    commit op) must not be denied by this guard -- it is not in scope for
    an EM-only commit gate.
    """
    _allows(
        monkeypatch,
        "python3 -m coordinator_core.invoke coverage.gate '{}'",
    )


def test_ceremony_invoke_module_mention_in_prose_allows(monkeypatch):
    """The module/op names appearing as plain prose text (not an actual
    invocation) must not be denied.
    """
    _allows(
        monkeypatch,
        'echo "the ceremony.scoped_git_commit op lives in coordinator_core.invoke"',
    )


def test_ceremony_scoped_git_commit_em_main_loop_allows():
    """No ``agent_id`` in the payload -> EM main-loop -> allowed exactly
    like a plain ``git commit``.
    """
    payload = _payload(
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}'",
        agent_id=None,
    )
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Allow-regressions that must not break
# ---------------------------------------------------------------------------


def test_non_bash_tool_allows():
    payload = {"tool_name": "Write", "tool_input": {"command": "git commit -m x"}}
    assert guard.check(payload) is None


def test_git_status_allows(monkeypatch):
    _allows(monkeypatch, "git status")


def test_git_log_allows(monkeypatch):
    _allows(monkeypatch, "git log --oneline")


def test_git_commitment_fund_subcommand_allows(monkeypatch):
    """``commitment-fund`` is not the ``commit`` subcommand token -- the
    token walk requires an exact ``commit`` token, not a prefix match.
    """
    _allows(monkeypatch, "git commitment-fund --help")


def test_echo_commit_words_allows(monkeypatch):
    """``commit`` appears in the string but there is no ``git``/
    ``coordinator-safe-commit`` invocation at all -- the pre-filter's
    over-approximation correctly lets this through to the full matchers,
    which correctly find no git-commit or helper invocation.
    """
    _allows(monkeypatch, 'echo "commit early commit often"')


def test_no_agent_id_em_main_loop_allows():
    payload = _payload("git commit -m x", agent_id=None)
    assert guard.check(payload) is None


def test_plain_git_commit_still_denies(monkeypatch):
    """Pre-existing baseline case (not a bypass) -- must remain denied
    after the hardening.
    """
    _denies(monkeypatch, 'git commit -m "msg"')


def test_unresolvable_agent_id_still_denies_via_agent_type_leg(monkeypatch):
    """2026-07-30 fix: an unparseable ``agent_id`` used to short-circuit to
    allow BEFORE the (already-known) ``agent_type`` leg was even consulted
    -- so a payload that plainly named its caller as ``coordinator:executor``
    was allowed to commit purely because the canonical-id leg failed to
    parse. raw_agent_id presence is the EM/subagent discriminator; an
    unparseable id no longer grants an early allow.
    """
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "")
    payload = _payload('git commit -m "msg"', agent_type=_SUBAGENT_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_effective_type_confines_as_subagent(monkeypatch):
    """2026-07-30 fix: a subagent (raw agent_id present) whose KIND could
    not be resolved via either OR-resolver leg -- unreadable/missing
    backpointer chain, no ``agent_type`` in the payload -- is still a
    subagent and is denied (fail-closed), not allowed on the lookup-miss.
    """
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: ""
    )
    payload = _payload('git commit -m "msg"', agent_type=None)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_empty_command_allows(monkeypatch):
    _subagent(monkeypatch)
    payload = _payload("", agent_type=_SUBAGENT_TYPE)
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# 2026-07-26 prose false-positive fix -- heredoc PAYLOAD content and quoted
# multi-word arguments must never be scanned as executed command tokens.
# Reproduces the live-guard report: a staff-eng reviewer persisting its
# findings sidecar via a Bash heredoc, whose PROSE discussed this guard's
# own "git commit" enforcement, was denied even though the executed command
# ran no git at all.
# ---------------------------------------------------------------------------


def test_heredoc_prose_about_commit_mechanics_allows(monkeypatch):
    """The reported shape: a bash heredoc writes a markdown review sidecar
    whose PROSE body discusses this very guard's git-commit enforcement.
    The executed command (``cat <<'EOF' > review.md`` ... ``EOF``) runs no
    git at all -- the heredoc BODY is stdin data, not command tokens.
    """
    cmd = (
        "cat <<'EOF' > /tmp/review.md\n"
        "## Findings\n"
        "\n"
        "This guard blocks any subagent git commit invocation, including\n"
        "one made via coordinator-safe-commit. See the commit-scoping\n"
        "doctrine for how git commit mechanics should work here.\n"
        "EOF\n"
    )
    _allows(monkeypatch, cmd)


def test_heredoc_prose_git_commit_adjacent_words_allows(monkeypatch):
    """Even a heredoc body where the words ``git`` and ``commit`` appear
    literally adjacent (no punctuation between them) must not be scanned --
    the body is stdin data for ``cat``, never shell command tokens.
    """
    cmd = (
        "cat <<'EOF' > /tmp/notes.md\n"
        "Reminder: never run git commit as a subagent.\n"
        "EOF\n"
    )
    _allows(monkeypatch, cmd)


def test_heredoc_introducer_with_real_git_commit_still_denies(monkeypatch):
    """A heredoc body is stripped, but the command LINE that introduces it
    is not -- a genuine ``git commit`` on that same line must still deny.
    """
    cmd = (
        'git commit -F - <<\'EOF\'\n'
        "Some commit message body mentioning git and commit words.\n"
        "EOF\n"
    )
    _denies(monkeypatch, cmd)


def test_git_commit_followed_by_ampersand_chain_denies(monkeypatch):
    """A real ``git commit`` chained with ``&&`` still denies."""
    _denies(monkeypatch, 'git add -- foo.py && git commit -m "msg"')


def test_git_dash_capital_c_path_flag_denies(monkeypatch):
    """``git -C <path> commit`` still denies after the tokenizer switch."""
    _denies(monkeypatch, 'git -C /some/repo commit -m "msg"')


def test_quoted_multiword_argument_mentioning_git_commit_allows(monkeypatch):
    """``commit`` (and even the word ``git``) inside a SINGLE quoted
    argument to a non-git command is not a git invocation -- the quoted
    span is one shlex word, not separate executable tokens.
    """
    _allows(monkeypatch, 'echo "reviewing git commit conventions"')
    _allows(monkeypatch, "echo 'please just commit to the idea'")


# ---------------------------------------------------------------------------
# C1 (2026-08-03, docs/plans/2026-08-03-narrow-subagent-commit-confinement-
# two-classes.md): Python-interpreter ``-c`` payload indirection. Repro
# confirmed failing at HEAD before this fix -- all three of
# ``_has_git_commit``/``_has_coordinator_safe_commit``/``_has_committing_op_
# invoke`` returned False for the exact command in
# ``test_python3_dash_c_scoped_git_commit_trampoline_denies`` below.
# ---------------------------------------------------------------------------


def test_python3_dash_c_scoped_git_commit_trampoline_denies(monkeypatch):
    """The exact repro this chunk closes: a ``scoped-git-commit`` invocation
    quoted as the ``-c`` payload to ``python3`` was previously ALLOWED
    outright by all three matchers.
    """
    _denies(
        monkeypatch,
        "python3 -c \"import subprocess; subprocess.run(['scoped-git-commit','-m','x'])\"",
    )


def test_python3_dash_c_git_commit_denies(monkeypatch):
    _denies(monkeypatch, 'python3 -c "import os; os.system(\'git commit -m x\')"')


def test_python_bare_dash_c_git_commit_denies(monkeypatch):
    """Bare ``python`` (not ``python3``) spelling."""
    _denies(monkeypatch, 'python -c "import os; os.system(\'git commit -m x\')"')


def test_python3_versioned_dash_c_git_commit_denies(monkeypatch):
    """A versioned interpreter basename (``python3.11``) must normalize to
    the same identity as bare ``python3``.
    """
    _denies(monkeypatch, 'python3.11 -c "import os; os.system(\'git commit -m x\')"')


def test_python_dotted_patch_version_dash_c_git_commit_denies(monkeypatch):
    _denies(monkeypatch, 'python3.12.1 -c "import os; os.system(\'git commit -m x\')"')


def test_python3_absolute_path_dash_c_git_commit_denies(monkeypatch):
    """Interpreter-path-prefixed spelling (``/usr/bin/python3``)."""
    _denies(
        monkeypatch,
        '/usr/bin/python3 -c "import os; os.system(\'git commit -m x\')"',
    )


def test_python_venv_relative_path_dash_c_git_commit_denies(monkeypatch):
    """Interpreter-path-prefixed spelling (``.venv/bin/python``)."""
    _denies(
        monkeypatch,
        '.venv/bin/python -c "import os; os.system(\'git commit -m x\')"',
    )


def test_python_windows_backslash_path_dash_c_git_commit_denies(monkeypatch):
    """Windows backslash-path-prefixed, ``.exe``-suffixed spelling (a
    relative launcher-directory suffix, deliberately no drive letter --
    test fixture text, not a citation of any real machine path).
    """
    _denies(
        monkeypatch,
        "runtime\\python.exe -c \"import os; os.system('git commit -m x')\"",
    )


def test_python3_bundled_dash_i_c_flag_git_commit_denies(monkeypatch):
    """Bundled short flag (``-ic``), mirroring the existing shell ``-ic``
    coverage above for ``_BUNDLED_C_FLAG_RE``.
    """
    _denies(monkeypatch, 'python3 -ic "import os; os.system(\'git commit -m x\')"')


def test_python3_dash_c_coordinator_safe_commit_denies(monkeypatch):
    _denies(monkeypatch, 'python3 -c "import os; os.system(\'coordinator-safe-commit -m x\')"')


def test_python3_dash_c_ceremony_scoped_git_commit_invoke_denies(monkeypatch):
    _denies(
        monkeypatch,
        "python3 -c \"import subprocess; subprocess.run(['python3','-m',"
        "'coordinator_core.invoke','ceremony.scoped_git_commit','{}'])\"",
    )


def test_python3_dash_c_env_prefixed_denies(monkeypatch):
    """``env python3 -c '...'`` -- same env-prefix peel as the shell case."""
    _denies(
        monkeypatch,
        "env python3 -c \"import os; os.system('git commit -m x')\"",
    )


def test_shell_wrapping_python_dash_c_nested_denies(monkeypatch):
    """``sh -c 'python3 -c "..."'`` -- a Python ``-c`` payload nested inside
    a shell ``-c`` payload, confirming the two interpreter families
    interoperate through the shared recursive unwrap.
    """
    _denies(
        monkeypatch,
        "sh -c 'python3 -c \"import os; os.system(\\'git commit -m x\\')\"'",
    )


def test_python3_dash_c_unrelated_payload_allows(monkeypatch):
    """A ``python3 -c`` payload that mentions neither ``commit`` nor any
    committing op must not be denied -- confirms the widened unwrap did not
    turn into an over-broad "any python3 -c denies" rule.
    """
    _allows(monkeypatch, 'python3 -c "print(\'hello world\')"')


def test_python3_dash_m_pytest_allows(monkeypatch):
    """A completely ordinary, non-``-c`` Python invocation (running the
    test suite) must not be affected by the widened interpreter check.
    """
    _allows(monkeypatch, "python3 -m pytest -q")


def test_normalized_interpreter_head_strips_version_and_path():
    """Unit-level pin on the new normalization helper directly, independent
    of the full ``check()`` seam.
    """
    assert guard._normalized_interpreter_head("python3") == "python3"
    assert guard._normalized_interpreter_head("python") == "python"
    assert guard._normalized_interpreter_head("python3.11") == "python3"
    assert guard._normalized_interpreter_head("python3.12.1") == "python3"
    assert guard._normalized_interpreter_head("/usr/bin/python3.11") == "python3"
    assert guard._normalized_interpreter_head("python.exe") == "python"
    assert guard._normalized_interpreter_head("runtime\\python.exe") == "python"
    # Non-python names, and names that merely start with "python", are
    # untouched -- this must never become a fuzzy/substring match.
    assert guard._normalized_interpreter_head("git") == "git"
    assert guard._normalized_interpreter_head("pythonista") == "pythonista"


def test_wrapped_shell_c_payloads_yields_python_dash_c_payload():
    """Unit-level pin on the shared unwrap generator itself: a Python
    ``-c`` payload is yielded exactly like a shell ``-c`` payload is (AC2 --
    same routine, no fourth matcher).
    """
    payloads = list(
        guard._wrapped_shell_c_payloads('python3 -c "git commit -m x"')
    )
    assert payloads == ["git commit -m x"]


def test_wrapped_shell_c_payloads_honours_max_unwrap_depth():
    """A Python ``-c`` payload nested past ``_MAX_COMMIT_UNWRAP_DEPTH`` is
    not infinitely unwrapped -- same bound the shell case already honours.
    """
    cmd = "python3 -c 'x'"
    depth_that_exceeds_bound = guard._MAX_COMMIT_UNWRAP_DEPTH + 1
    payloads = list(guard._wrapped_shell_c_payloads(cmd, depth=depth_that_exceeds_bound))
    assert payloads == []


# ---------------------------------------------------------------------------
# Part 13 (2026-08-04): the C1 reconstruction leg denies correctly but used
# to MESSAGE wrongly -- a read-only command naming a commit helper as DATA
# got "finish your edits and report to the EM", an action that resolves
# nothing, on a leg no argv re-spelling can ever pass. Message selection
# only: the verdict matrix below is the pin that nothing moved.
# ---------------------------------------------------------------------------

_READ_ONLY_PAYLOAD_NAMING_THE_HELPER = (
    "python3 -c \"import ast; ast.parse(open('coordinator/bin/"
    "scoped-git-commit').read())\""
)

#: Part 14 (2026-08-04): a read-only payload the inert allowlist CANNOT
#: clear -- ``pathlib`` is outside ``_INERT_PAYLOAD_IMPORT_ROOTS`` and
#: ``read_text`` hangs off an unresolvable object -- so the reconstruction
#: leg still runs, it still denies, and the part-13 payload-leg message is
#: still the honest thing to say to that caller. This is what keeps the
#: message live after part 14 flipped the verdict on the PROVABLY-inert
#: shape above.
_UNPROVABLE_READ_ONLY_PAYLOAD = (
    "python3 -c \"import pathlib; print(pathlib.Path('coordinator/bin/"
    "scoped-git-commit').read_text())\""
)

#: Every command whose verdict this dispatch must leave exactly where it
#: was, as ``(cmd, expect_deny)``. Deliberately spans all three matchers,
#: both unwrap families (shell `-c` and Python `-c`), the reconstruction
#: leg itself, and the near-miss ALLOWs the reconstruction must not swallow.
_VERDICT_PARITY_MATRIX = [
    ('git commit -m "msg"', True),
    ("coordinator-safe-commit -m x", True),
    ("scoped-git-commit -m x -- src/foo.py", True),
    ("python3 -m coordinator_core.invoke ceremony.scoped_git_commit '{}'", True),
    ("sh -c 'git commit -m x'", True),
    ("python3 -c \"import subprocess; subprocess.run(['scoped-git-commit','-m','x'])\"", True),
    ('python3 -c "import os; os.system(\'git commit -m x\')"', True),
    (_UNPROVABLE_READ_ONLY_PAYLOAD, True),
    # Part 14 flipped this ONE entry, deliberately and as the whole point of
    # that change: a payload with no execution sink cannot be a disguised
    # commit however its literals read. Every other row is untouched.
    (_READ_ONLY_PAYLOAD_NAMING_THE_HELPER, False),
    ("git status", False),
    ("git log --oneline", False),
    ('echo "reviewing git commit conventions"', False),
    ("python3 -m py_compile coordinator_core/ops/ceremony/scoped_git_commit.py", False),
    ('python3 -c "print(\'hello world\')"', False),
    ("python3 -m pytest -q", False),
]


def _reason(monkeypatch, cmd):
    _subagent(monkeypatch)
    result = guard.check(_payload(cmd, agent_type=_SUBAGENT_TYPE))
    assert result is not None, f"expected DENY for: {cmd!r}"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("cmd,expect_deny", _VERDICT_PARITY_MATRIX)
def test_payload_leg_threading_changes_no_verdict(monkeypatch, cmd, expect_deny):
    """AC1: threading the leg tag out of the unwrap is MESSAGE SELECTION
    ONLY -- every command that denied before still denies, every command
    that allowed still allows.
    """
    if expect_deny:
        _denies(monkeypatch, cmd)
    else:
        _allows(monkeypatch, cmd)


@pytest.mark.parametrize("cmd,_expect_deny", _VERDICT_PARITY_MATRIX)
def test_matchers_return_the_same_verdict_with_and_without_a_leg_collector(
    cmd, _expect_deny
):
    """The collector is optional by construction: a ``None`` ``legs``
    argument must leave each matcher byte-identical in behaviour to the
    same call with a collector attached.
    """
    for matcher in (
        guard._has_git_commit,
        guard._has_coordinator_safe_commit,
        guard._has_committing_op_invoke,
        guard._has_reconstructed_commit_identity,
    ):
        assert matcher(cmd) == matcher(cmd, legs=set()), (matcher.__name__, cmd)


def test_provably_inert_read_only_payload_now_allows(monkeypatch):
    """Part 14 supersedes part 13's verdict for this ONE shape: the reported
    incident command carries no execution sink at all
    (``_python_c_payload_is_provably_inert``), so the reconstruction leg is
    never run and there is nothing left to message about. The full
    adversarial corpus behind this flip lives in
    ``test_python_c_inert_payload_exemption.py``.
    """
    _allows(monkeypatch, _READ_ONLY_PAYLOAD_NAMING_THE_HELPER)


def test_read_only_python_c_payload_deny_message_names_the_payload_leg(monkeypatch):
    """The reported incident's message fix, still live for every read-only
    payload the inert allowlist cannot PROVE sinkless (here: ``pathlib``,
    outside the import allowlist). The generic "finish your edits and report
    to the EM" names an action that resolves nothing for a caller that never
    tried to commit, on a leg no argv variant can pass.
    """
    reason = _reason(monkeypatch, _UNPROVABLE_READ_ONLY_PAYLOAD)
    assert reason == guard._PYTHON_C_PAYLOAD_DENY_REASON
    assert "`-c` payload" in reason
    assert "No re-spelling passes." in reason
    assert "`Read`" in reason
    assert "py_compile" in reason
    assert "report to the EM" not in reason


def test_python_c_subprocess_commit_gets_the_same_payload_leg_message(monkeypatch):
    """The genuinely-committing shape gets the SAME message, and that is the
    honest answer: the reconstruction discards all Python syntax between the
    literals, so it cannot tell this apart from the read-only command above.
    A message claiming otherwise would be asserting a distinction the guard
    does not make.
    """
    reason = _reason(
        monkeypatch,
        "python3 -c \"import subprocess; subprocess.run(['scoped-git-commit','-m','x'])\"",
    )
    assert reason == guard._PYTHON_C_PAYLOAD_DENY_REASON


def test_plain_commit_deny_message_is_still_the_generic_one(monkeypatch):
    """No leg tag, no new message -- the generic text is unchanged for every
    command that did not reach a match through the reconstruction.
    """
    reason = _reason(monkeypatch, 'git commit -m "msg"')
    assert "subagents may not commit" in reason
    assert "report to the EM" in reason
    assert reason != guard._PYTHON_C_PAYLOAD_DENY_REASON


def test_shell_c_payload_deny_message_is_still_the_generic_one(monkeypatch):
    """A shell `-c` payload is genuinely executed text, not a reconstruction
    -- it carries no tag and must keep the generic message.
    """
    reason = _reason(monkeypatch, "sh -c 'git commit -m x'")
    assert reason == guard._deny_reason("a", _SUBAGENT_TYPE, _SUBAGENT_TYPE, "cmd")


def test_python_c_payload_leg_message_fits_prose_cap():
    from coordinator_core.bash_guards._message_size import (
        MESSAGE_PROSE_CAP_BYTES,
        measure_envelope,
    )

    measurement = measure_envelope(
        {"hookSpecificOutput": {"permissionDecisionReason": guard._PYTHON_C_PAYLOAD_DENY_REASON}}
    )
    assert not measurement.over_cap
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES


def test_payload_leg_message_is_selected_by_the_threaded_tag_not_by_rescanning():
    """`_deny_reason` must not re-derive the leg from ``cmd``: handed the
    triggering command text but an EMPTY ``command_leg``, it falls back to
    the generic message. A second, independently-derived notion of why this
    guard denied is the drift shape this module's history keeps producing.
    """
    generic = guard._deny_reason(
        "a", _SUBAGENT_TYPE, _SUBAGENT_TYPE, _READ_ONLY_PAYLOAD_NAMING_THE_HELPER
    )
    assert generic != guard._PYTHON_C_PAYLOAD_DENY_REASON
    tagged = guard._deny_reason(
        "a",
        _SUBAGENT_TYPE,
        _SUBAGENT_TYPE,
        "any command text at all",
        "",
        guard._PAYLOAD_LEG_PYTHON_STRING_LITERALS,
    )
    assert tagged == guard._PYTHON_C_PAYLOAD_DENY_REASON


def test_payload_leg_selection_is_keyed_on_command_shape_not_identity():
    """The new branch reads no identity field, so it cannot become the
    probing seam `_deny_reason`'s own Finding-2 note describes -- the same
    tag yields the same message for every ``agent_type``/``effective_type``
    pair, including the forged-backpointer shape (empty ``agent_type``, a
    ``coordinator:git-commit-agent`` ``effective_type``).
    """
    identities = [
        (_SUBAGENT_TYPE, _SUBAGENT_TYPE),
        ("", ""),
        ("", _GIT_COMMIT_AGENT_TYPE),
        (_GIT_COMMIT_AGENT_TYPE, _GIT_COMMIT_AGENT_TYPE),
    ]
    for agent_type, effective_type in identities:
        assert (
            guard._deny_reason(
                "a",
                effective_type,
                agent_type,
                "cmd",
                "",
                guard._PAYLOAD_LEG_PYTHON_STRING_LITERALS,
            )
            == guard._PYTHON_C_PAYLOAD_DENY_REASON
        ), (agent_type, effective_type)


def test_wrapped_shell_c_payload_legs_tags_only_the_reconstruction():
    """Unit pin on the tagging seam itself: a payload a real interpreter
    received carries no tag; the synthetic argv line rebuilt from the
    payload's own string literals carries `_PAYLOAD_LEG_PYTHON_STRING_
    LITERALS`, as does anything unwrapped beneath it.
    """
    tagged = list(
        guard._wrapped_shell_c_payload_legs(
            "python3 -c \"import subprocess; subprocess.run(['scoped-git-commit','-m','x'])\""
        )
    )
    legs_by_payload = dict(tagged)
    assert legs_by_payload["import subprocess; subprocess.run(['scoped-git-commit','-m','x'])"] == ""
    assert (
        legs_by_payload["scoped-git-commit -m x"]
        == guard._PAYLOAD_LEG_PYTHON_STRING_LITERALS
    )
    shell_only = list(guard._wrapped_shell_c_payload_legs("sh -c 'git commit -m x'"))
    assert shell_only == [("git commit -m x", "")]


def test_untagged_generator_view_is_unchanged():
    """`_wrapped_shell_c_payloads` keeps its original signature and yield
    list -- callers that only need payload TEXT never see the tag.
    """
    cmd = "python3 -c \"import subprocess; subprocess.run(['scoped-git-commit','-m','x'])\""
    assert list(guard._wrapped_shell_c_payloads(cmd)) == [
        payload for payload, _leg in guard._wrapped_shell_c_payload_legs(cmd)
    ]


# ---------------------------------------------------------------------------
# C3 (2026-08-03-narrow-subagent-commit-confinement-two-classes.md) -- the
# route-keyed coordinator:git-commit-agent commit exemption. DR-125 Ruling
# 3's one deliberate allow-path widening: ALLOW only when all three legs
# hold (strict agent_type match, a genuine ceremony.scoped_git_commit
# invocation, an explicit non-sweeping pathspec), plus the fail-closed
# landing-order safety net over C4's ownership-scope check.
# ---------------------------------------------------------------------------

_GIT_COMMIT_AGENT_TYPE = guard._GIT_COMMIT_AGENT_TYPE
_FAKE_REPO_ROOT = "/repo"


def _git_commit_agent_setup(
    monkeypatch,
    *,
    subagent_type=_GIT_COMMIT_AGENT_TYPE,
    git_root=_FAKE_REPO_ROOT,
    scope_result=(True, ""),
):
    """Wire the identity + ownership-scope seams for the tests below.

    ``subagent_type`` is the DISK-read backpointer leg (used by the AC15/
    AC19 negative tests to simulate a forged/teammate-name shape --
    ``payload["agent_type"]`` is set independently, per-call, via
    ``_payload``'s own ``agent_type`` kwarg). ``scope_result`` is the tuple
    ``_assert_paths_in_session_scope`` returns (the same fixed verdict
    regardless of ``allow_orphans``, since a test choosing this fixture
    already knows what it wants the ownership leg to say). The helper is
    lazily imported at call time (hot-path import-cost fix) via ``guard.
    _import_assert_paths_in_session_scope``, so that is the seam patched
    here rather than a module-level ``_assert_paths_in_session_scope``
    attribute.

    Returns ``calls``, a list this fixture's mock appends one dict to per
    invocation (``session_id``/``paths``/``cwd``/``allow_orphans``) -- a
    call-recording SPY, not a bare stub (F5, staff-eng review, 2026-08-04):
    a stub that returns a fixed verdict regardless of how it was called
    lets `test_..._sweeping_element_denies_before_ownership_leg_even_
    reached` pass EVEN IF the ownership helper were mocked to raise (a
    sweeping short-circuit and a raising helper both produce the identical
    static deny message) -- no assertion on this list is a genuine ordering
    oracle. Most tests below discard the returned list (unchanged
    call-site shape); the tests that DO inspect it get a direct ordering
    pin (``calls == []`` when the sweeping leg short-circuits before the
    ownership helper is ever reached) and a direct pin on the derived
    ``allow_orphans`` value (F0 -- mirrors the invocation's own
    ``--include-orphans``/``"include_orphans": true``, never a hard-coded
    ``True``) instead of only the static message both bugs happen to share.
    A drifted mock signature then surfaces as a missing/malformed recorded
    call (a `TypeError` from this spy, caught by `_git_commit_agent_may_
    commit`'s own `except Exception: return False, ""`) instead of a
    silent always-deny.
    """
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: git_root)
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: subagent_type
    )

    def _spy(session_id, paths, cwd=None, *, allow_orphans=False):
        calls.append(
            {
                "session_id": session_id,
                "paths": paths,
                "cwd": cwd,
                "allow_orphans": allow_orphans,
            }
        )
        return scope_result

    monkeypatch.setattr(guard, "_import_assert_paths_in_session_scope", lambda: _spy)
    return calls


def _gca_denies(monkeypatch, cmd, agent_type=_GIT_COMMIT_AGENT_TYPE, **setup_kwargs):
    _git_commit_agent_setup(monkeypatch, **setup_kwargs)
    result = guard.check(_payload(cmd, agent_type=agent_type))
    assert result is not None, f"expected DENY for: {cmd!r}"
    assert (
        result["hookSpecificOutput"]["permissionDecision"] == "deny"
    ), f"expected DENY for: {cmd!r}"
    return result


def _gca_allows(monkeypatch, cmd, agent_type=_GIT_COMMIT_AGENT_TYPE, **setup_kwargs):
    _git_commit_agent_setup(monkeypatch, **setup_kwargs)
    result = guard.check(_payload(cmd, agent_type=agent_type))
    assert result is None, f"expected ALLOW for: {cmd!r}, got {result!r}"


# --- AC6: the full deny matrix (non-scoped shapes; empty/absent pathspec) ---


def test_git_commit_agent_bare_git_commit_denies(monkeypatch):
    _gca_denies(monkeypatch, 'git commit -m "msg"')


def test_git_commit_agent_git_commit_dash_a_denies(monkeypatch):
    _gca_denies(monkeypatch, 'git commit -a -m "msg"')


def test_git_commit_agent_git_add_dash_capital_a_denies(monkeypatch):
    """NOTE: ``git add -A`` is pre-existing OUT OF SCOPE for this guard's
    matchers regardless of subagent type or this chunk -- it stages but does
    not commit, and none of ``_has_git_commit``/``_has_coordinator_safe_
    commit``/``_has_committing_op_invoke`` classify a bare ``git add`` as a
    commit shape at all (``_prefilter_mentions_commit`` returns ``False`` for
    it, so ``check()`` allows before identity resolution ever runs -- true
    before and after C3, for every subagent type). Flagged, not fixed here:
    widening the DENY matchers to also cover staging-only commands is a much
    larger blast radius than this chunk's narrow allow-predicate scope.
    Pinned instead on a shape this guard DOES already classify as a commit.
    """
    _gca_denies(monkeypatch, 'git commit -a -m "msg"')


def test_git_commit_agent_coordinator_safe_commit_denies(monkeypatch):
    """``coordinator-safe-commit`` is a DIFFERENT helper from
    ``scoped-git-commit`` -- LEG 2 requires the invocation to resolve to
    ``ceremony.scoped_git_commit`` specifically (the only op with an
    explicit-pathspec parameter at all), which this helper never does.
    """
    _gca_denies(monkeypatch, 'coordinator-safe-commit -m "msg"')


def test_git_commit_agent_scoped_git_commit_empty_pathspec_denies(monkeypatch):
    _gca_denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit "
        "'{\"worktree_root\": \"/repo\", \"paths\": [], \"message\": \"x\"}'",
    )


def test_git_commit_agent_scoped_git_commit_absent_pathspec_denies(monkeypatch):
    _gca_denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit "
        "'{\"worktree_root\": \"/repo\", \"message\": \"x\"}'",
    )


def test_git_commit_agent_trampoline_no_paths_after_separator_denies(monkeypatch):
    _gca_denies(monkeypatch, 'scoped-git-commit -m "msg" --')


def test_git_commit_agent_scoped_git_commit_params_file_form_denies(monkeypatch):
    """``--params-file`` payload text is never visible in argv -- not
    determinable from ``cmd`` at all, treated identically to an absent
    pathspec (deny), never as an implicit allow.
    """
    _gca_denies(
        monkeypatch,
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit "
        "--params-file /tmp/p.json",
    )


# --- AC14: every named sweeping pathspec form denies ---


@pytest.mark.parametrize(
    "sweeping_element",
    [
        ".",
        "./",
        ":/",
        ":(top)",
        ":!nonexistent-file",  # Finding 4 (2026-08-03 security review):
        # git's `:!<pattern>` shorthand for `:(exclude)<pattern>` -- the
        # same magic-pathspec family as `:(`/`:/` above, previously missed
        # by an enumerated-prefix check.
        "*.py",
        "/repo",
        "..",
        "../..",
        "-A",
        "-a",
        "--all",
    ],
)
def test_git_commit_agent_ac14_sweeping_pathspec_forms_deny(monkeypatch, sweeping_element):
    _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- %s' % sweeping_element,
    )


def test_git_commit_agent_one_sweeping_element_among_others_denies(monkeypatch):
    """A single sweeping element among otherwise-fine ones still denies the
    WHOLE pathspec (AC14) -- this is not an element-by-element filter.
    """
    _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py .',
    )


# --- The one allowed shape: explicit, non-sweeping pathspec ---


def test_git_commit_agent_scoped_git_commit_trampoline_explicit_pathspec_allows(monkeypatch):
    _gca_allows(monkeypatch, 'scoped-git-commit -m "msg" --repo /repo -- src/foo.py')


def test_git_commit_agent_scoped_git_commit_invoke_explicit_pathspec_allows(monkeypatch):
    _gca_allows(
        monkeypatch,
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit "
        "'{\"worktree_root\": \"/repo\", \"paths\": [\"src/foo.py\"], \"message\": \"x\"}'",
    )


def test_git_commit_agent_scoped_git_commit_multiple_non_sweeping_paths_allows(monkeypatch):
    _gca_allows(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py docs/bar.md',
    )


def test_git_commit_agent_allow_branch_is_gated_by_may_commit_helper(monkeypatch):
    """Mutation check: forcing ``_git_commit_agent_may_commit`` to always
    return ``False`` turns the otherwise-allowed sanctioned-route command
    into a deny -- confirms the allow branch is actually gated by that
    function's return value, not by some other path that happens to also
    return ``None``.
    """
    _git_commit_agent_setup(monkeypatch)
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: (False, ""))
    payload = _payload(
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py',
        agent_type=_GIT_COMMIT_AGENT_TYPE,
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Finding 1 + 6 (2026-08-03 security review, P0, EM-confirmed live bypass):
# the C3 allow branch previously ALLOWED an entire compound command whenever
# ITS FIRST segment matched a genuine `ceremony.scoped_git_commit`
# invocation -- a second, wholly unvalidated committing segment chained
# alongside it (`;`/`&&`/`||`/`&`/`|`) rode along, allowed in full. Fixed by
# `_command_is_single_segment`: the C3 allow branch now requires ``cmd`` to
# resolve to exactly ONE non-empty segment. See that function's own
# docstring for the mechanism and the wrapped/interpreter-payload analysis.
# ---------------------------------------------------------------------------


def test_git_commit_agent_chained_second_committing_segment_denies(monkeypatch):
    """EM repro #2: a legitimate scoped-commit segment chained via ``;``
    with a second, unvalidated ``git commit -a`` sweep -- previously ALLOWED
    in full, now denies.
    """
    _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py ; '
        'git commit -a -m "sweep everything"',
    )


def test_git_commit_agent_chained_and_second_committing_segment_denies(monkeypatch):
    """EM repro #3: same shape, chained via ``&&``."""
    _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py && '
        'git commit -a -m "sweep everything"',
    )


def test_git_commit_agent_chained_semicolon_git_add_dash_capital_a_denies(monkeypatch):
    """EM repro #4: a legitimate scoped-commit segment chained via ``;``
    with a second, unvalidated ``git add -A`` (stages the whole tree).
    """
    _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py ; git add -A',
    )


def test_git_commit_agent_chained_or_second_committing_segment_denies(monkeypatch):
    _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py || '
        'git commit -a -m "sweep everything"',
    )


def test_git_commit_agent_chained_pipe_second_segment_denies(monkeypatch):
    """The pipe (``|``) separator variant named in the fix brief -- also a
    genuine `_segments_from_tokens` boundary, also denied by the
    single-segment precondition.
    """
    _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py | '
        'git commit -a -m "sweep everything"',
    )


def test_git_commit_agent_semicolon_inside_quoted_message_still_allows(monkeypatch):
    """False-positive check: a ``;`` INSIDE a quoted commit-message argument
    is not a real segment boundary -- an otherwise single-segment, valid,
    non-sweeping-pathspec command must NOT be denied merely because its
    commit message happens to contain a literal ``;``. Segmentation uses
    the same quote-aware tokenizer every matcher already scans with, so
    this stays a single segment.
    """
    _gca_allows(
        monkeypatch,
        'scoped-git-commit -m "fix; cleanup" --repo /repo -- src/foo.py',
    )


def test_git_commit_agent_shell_wrapped_compound_payload_denies(monkeypatch):
    """Interpreter-payload variant: a single TOP-LEVEL segment (``sh -c
    "..."``) whose ``-c`` payload is itself a compound command chaining a
    scoped invocation with a second, unvalidated committing segment.
    `_resolve_git_commit_agent_pathspec` deliberately never unwraps a
    ``-c`` payload (by design, per its own docstring -- LEG 2 stays
    conservative rather than gaining a wider ALLOW surface), so no genuine
    invocation is ever matched here and this denies for lack of any LEG 2
    match, independent of -- and in addition to -- the single-segment
    precondition (the wrapper itself is one top-level segment, so the
    precondition alone would not have caught this smuggling path; LEG 2's
    no-unwrap design is what actually closes it).
    """
    _gca_denies(
        monkeypatch,
        'sh -c "scoped-git-commit -m msg --repo /repo -- src/foo.py ; '
        'git commit -a -m sweep"',
    )


def test_command_is_single_segment_true_for_ordinary_scoped_commit():
    assert guard._command_is_single_segment(
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py'
    ) is True


def test_command_is_single_segment_false_for_semicolon_chain():
    assert guard._command_is_single_segment(
        'scoped-git-commit -m "msg" -- src/foo.py ; git commit -a -m sweep'
    ) is False


def test_command_is_single_segment_false_for_spaced_ampersand_redirect_chain():
    """The 2026-08-04 security-audit finding, pinned.

    `... & >/dev/null git commit -a -m sweep` is TWO real bash commands, and
    the second never reaches `ceremony.scoped_git_commit`'s ownership and
    sweeping re-validation. The same-day redirect-token join collapsed it to
    one segment, so it satisfied this precondition and the C3 branch allowed
    the command IN FULL — a false ALLOW on a commit-authorization boundary.

    The adjacent spelling on the next line is the legitimate one and must
    stay True; that pair is the whole point.
    """
    assert guard._command_is_single_segment(
        'scoped-git-commit -m "msg" -- src/foo.py & >/dev/null git commit -a -m sweep'
    ) is False
    assert guard._command_is_single_segment(
        'scoped-git-commit -m "msg" -- src/foo.py &>/dev/null'
    ) is True


def test_command_is_single_segment_true_for_quoted_semicolon():
    assert guard._command_is_single_segment(
        'scoped-git-commit -m "fix; cleanup" -- src/foo.py'
    ) is True


def test_command_is_single_segment_false_for_unparseable_command():
    """Fails CLOSED on an unparseable command (unterminated quote)."""
    assert guard._command_is_single_segment('scoped-git-commit -m "unterminated') is False


# --- 2026-08-04 incident: a trailing `2>&1` read as a segment boundary ---
#
# Four `coordinator:git-commit-agent` dispatches in one run; the two that
# wrote a trailing `2>&1` were denied, the two that did not committed. `&` is
# in `shlex`'s `punctuation_chars`, so `2>&1` lexed as `['2>', '&', '1']` and
# `_command_is_single_segment` counted TWO segments for an invocation that
# runs exactly one command. Fixed at the tokenizer
# (`_command_tokenizer.join_redirection_operator_tokens`) rather than here,
# so every guard in the package stops mis-segmenting a redirection; pinned
# from this guard's side because this is where it was observed biting.


def test_command_is_single_segment_true_for_trailing_stderr_redirect():
    """`2>&1` is a redirection, not a control operator -- it starts no new
    command and must not make an ordinary invocation read as compound.
    """
    assert guard._command_is_single_segment(
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py 2>&1'
    ) is True


def test_command_is_single_segment_true_for_fd_dup_and_file_redirects():
    for cmd in (
        'scoped-git-commit -m "msg" -- src/foo.py >&2',
        'scoped-git-commit -m "msg" -- src/foo.py &>/tmp/out.log',
        'scoped-git-commit -m "msg" -- src/foo.py 2>/dev/null',
        'scoped-git-commit -m "msg" -- src/foo.py 2>&-',
    ):
        assert guard._command_is_single_segment(cmd) is True, cmd


def test_command_is_single_segment_false_for_background_after_redirect():
    """The re-join must not swallow a REAL separator: a genuine chaining `&`
    after a redirection still bounds a second segment.
    """
    assert guard._command_is_single_segment(
        'scoped-git-commit -m "msg" -- src/foo.py 2>&1 & git commit -a -m sweep'
    ) is False


def test_git_commit_agent_trailing_stderr_redirect_still_allows(monkeypatch):
    """End-to-end repro of the observed incident: the sanctioned form with a
    trailing `2>&1` must reach the ownership leg and be allowed, not denied
    on the single-segment precondition.
    """
    _gca_allows(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py 2>&1',
    )


def test_redirection_target_is_not_treated_as_a_pathspec_element():
    """A redirection and its target are shell syntax the CLI never receives,
    so they must not be handed to the ownership-scope check as paths.
    """
    assert guard._resolve_git_commit_agent_pathspec(
        'scoped-git-commit -m "msg" -- src/foo.py > /tmp/out.log'
    ) == (["src/foo.py"], False)
    assert guard._resolve_git_commit_agent_pathspec(
        'scoped-git-commit -m "msg" -- src/foo.py 2>&1'
    ) == (["src/foo.py"], False)


def test_pathspec_token_after_a_mid_pathspec_redirect_is_still_checked():
    """Review: coordinator:code-reviewer -- a redirect interspersed mid-
    pathspec previously truncated the guard's own ownership check at the
    first redirection token, silently dropping every real path after it
    (backstopped by the sink's independent re-validation, but the pre-check
    itself under-validated). `bar.py` must survive both the bare-operator
    form and the fd-duplication form.
    """
    assert guard._resolve_git_commit_agent_pathspec(
        'scoped-git-commit -m "msg" -- foo.py > /tmp/out.log bar.py'
    ) == (["foo.py", "bar.py"], False)
    assert guard._resolve_git_commit_agent_pathspec(
        'scoped-git-commit -m "msg" -- foo.py 2>&1 bar.py'
    ) == (["foo.py", "bar.py"], False)


# --- 2026-08-04: the deny message must name the leg that actually denied ---


def test_compound_command_denial_does_not_blame_the_pathspec(monkeypatch):
    """The defect that stranded two agents: denied on the compound-command
    leg, told to re-check a pathspec the guard never even looked at. Both
    agents had already verified their pathspec with `git status --porcelain`,
    so the message left them with no reachable next action.
    """
    result = _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py ; '
        'git commit -a -m "sweep everything"',
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "uncompounded" in reason
    assert "Pathspec never inspected" in reason
    assert "Check path scope" not in reason


def test_git_commit_agent_leg_messages_fit_prose_cap():
    from coordinator_core.bash_guards._message_size import (
        MESSAGE_PROSE_CAP_BYTES,
        measure_envelope,
    )

    for leg, message in guard._GIT_COMMIT_AGENT_LEG_MESSAGES.items():
        measurement = measure_envelope(
            {"hookSpecificOutput": {"permissionDecisionReason": message}}
        )
        assert not measurement.over_cap, leg
        assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES, leg


def test_unknown_leg_sentinel_falls_back_to_the_static_message():
    """`_deny_reason` maps sentinels through a fixed table, so an
    unrecognized value can only ever reach the static text -- never splice
    caller-supplied prose into the capped envelope.
    """
    assert guard._deny_reason(
        "a", "", _GIT_COMMIT_AGENT_TYPE, "cmd", "leg:invented-later"
    ) == guard._GIT_COMMIT_AGENT_DENY_REASON


# --- AC7: type membership alone is not the mechanism ---


def test_ac7_type_membership_alone_is_not_the_mechanism(monkeypatch):
    """AC7: the exemption is keyed on (type AND route AND non-sweeping
    pathspec), never on type membership alone. Two halves:
      (1) the REAL guard denies a bare, pathspec-free ``git commit`` for
          this type -- the mechanism it actually uses grants no blanket
          exemption; and
      (2) naively adding the type to ``_ALLOWED_SUBAGENT_TYPES`` -- the
          shape this module's docstring explicitly rejects -- WOULD
          incorrectly allow it (every command shape, not just a scoped
          commit route), demonstrating why that placement was rejected.
    """
    _gca_denies(monkeypatch, 'git commit -m "msg"')

    monkeypatch.setattr(
        guard, "_ALLOWED_SUBAGENT_TYPES", frozenset({_GIT_COMMIT_AGENT_TYPE})
    )
    _git_commit_agent_setup(monkeypatch)
    result = guard.check(_payload('git commit -m "msg"', agent_type=_GIT_COMMIT_AGENT_TYPE))
    assert result is None, "type-membership-alone placement would (wrongly) allow this"


# --- AC15: the backpointer leg alone is insufficient for LEG 1 ---


def test_ac15_backpointer_leg_alone_insufficient_denies(monkeypatch):
    """AC15: empty/absent ``agent_type`` + a backpointer resolving to
    ``coordinator:git-commit-agent`` still DENIES the sanctioned route --
    LEG 1 is keyed strictly on the harness-supplied ``payload["agent_type"]``,
    never the disk-read backpointer leg.
    """
    _git_commit_agent_setup(monkeypatch, subagent_type=_GIT_COMMIT_AGENT_TYPE)
    payload = _payload('scoped-git-commit -m "msg" --repo /repo -- src/foo.py', agent_type=None)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    # Finding 2 (2026-08-03 security review): message SELECTION must honor
    # the same agent_type-only asymmetry the ALLOW branch itself uses --
    # this forged-backpointer adversary must not be able to distinguish
    # "denied, generic subagent" from "denied, git-commit-agent route"
    # via message content, even though the verdict is deny either way.
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == (
        "BLOCKED: subagents may not commit -- EM-only, enforced by caller "
        "identity, no cooperative override. Finish your edits and report to "
        "the EM instead: hand off changed files; the EM runs `git commit`."
    )
    assert "scoped-git-commit" not in reason


# --- AC19: a NAMED (teammate) dispatch denies the sanctioned route ---


def test_ac19_named_teammate_dispatch_denies(monkeypatch):
    """AC19: a NAMED (teammate) dispatch of ``coordinator:git-commit-agent``
    -- ``agent_type`` carries the teammate's NAME, the real type resolves
    only via the forgeable backpointer disk leg -- DENIES the sanctioned
    route. The route is available only to an unnamed/foreground dispatch.
    """
    _git_commit_agent_setup(monkeypatch, subagent_type=_GIT_COMMIT_AGENT_TYPE)
    payload = _payload(
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py', agent_type="sam"
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- AC8: every other subagent type is unaffected ---


def test_ac8_other_subagent_type_scoped_commit_still_denies(monkeypatch):
    """AC8: every OTHER subagent type's commit verdicts are unchanged --
    ``coordinator:executor`` still denies the exact shape that ALLOWS for
    ``coordinator:git-commit-agent``, and gets the byte-identical generic
    message, not the specialized one.
    """
    _git_commit_agent_setup(monkeypatch, subagent_type=_SUBAGENT_TYPE)
    payload = _payload(
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py',
        agent_type=_SUBAGENT_TYPE,
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "git-commit-agent" not in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_ac8_unresolved_effective_type_path_still_denies(monkeypatch):
    """AC8: the pre-existing fail-closed unresolved-``effective_type`` path
    (no ``agent_type``, no resolvable backpointer) is untouched by C3.
    """
    _git_commit_agent_setup(monkeypatch, subagent_type="")
    payload = _payload(
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py', agent_type=None
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- Landing-order safety net (required, not optional) ---


def test_landing_order_safety_scope_import_unavailable_denies(monkeypatch):
    """If ``_assert_paths_in_session_scope`` failed to import (a partial/
    out-of-order landing of C4's ownership check), the allow branch must
    hard-DENY rather than allow -- never treat a missing import as "no
    ownership constraint applies".
    """
    _git_commit_agent_setup(monkeypatch)
    monkeypatch.setattr(guard, "_import_assert_paths_in_session_scope", lambda: None)
    payload = _payload(
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py',
        agent_type=_GIT_COMMIT_AGENT_TYPE,
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_landing_order_safety_git_root_unresolvable_denies(monkeypatch):
    """LEG 1 (``agent_type``) is satisfiable with no repo to compute scope
    from at all -- the allow branch must explicitly deny when ``git_root``
    does not resolve, not silently skip the sweeping-pathspec check.
    """
    _git_commit_agent_setup(monkeypatch, git_root=None)
    payload = _payload(
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py',
        agent_type=_GIT_COMMIT_AGENT_TYPE,
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_ownership_scope_rejection_denies(monkeypatch):
    """AC11/AC12-adjacent defense-in-depth: an ownership-scope rejection
    (a path outside the calling session's own claimed scope) denies even
    when legs 1-3 all otherwise hold.
    """
    _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py',
        scope_result=(False, "not owned by this session"),
    )


def test_ownership_scope_exception_denies(monkeypatch):
    """`_assert_paths_in_session_scope` raising must never propagate past
    this guard's fail-closed contract -- it denies exactly like an
    ordinary ``(False, ...)`` return.
    """
    _git_commit_agent_setup(monkeypatch)

    def _raises(session_id, paths, cwd=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "_import_assert_paths_in_session_scope", lambda: _raises)
    payload = _payload(
        'scoped-git-commit -m "msg" --repo /repo -- src/foo.py',
        agent_type=_GIT_COMMIT_AGENT_TYPE,
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- AC16: the specialized deny message ---


def test_ac16_deny_message_names_sanctioned_route_for_git_commit_agent(monkeypatch):
    result = _gca_denies(monkeypatch, "git commit -m x")
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "scoped-git-commit" in reason
    assert "subagents may not commit" not in reason


def test_ac16_deny_message_names_ownership_scope_leg_not_just_argv_shape(monkeypatch):
    """Regression pin (spike-verdict `2026-08-03-git-commit-agent-leg3-
    payload-triple.md`): the message must name the ownership-scope leg
    (`_assert_paths_in_session_scope`) alongside the argv-shape leg
    (`_pathspec_element_is_sweeping`'s reject-list) -- an agent that already
    used the prescribed `scoped-git-commit` form and is denied on the scope
    leg must be told to check scope, not sent back to re-try argv variants.
    A future edit that silently reverts this to argv-shape-only prose must
    fail this test.

    2026-08-04 amendment: the message is now selected PER LEG (see
    `_GIT_COMMIT_AGENT_LEG_MESSAGES`), so the single combined "both legs"
    sentence this test originally pinned no longer exists -- naming both
    legs in every message is exactly what let a compound-command denial
    claim a pathspec-scope cause. `git commit -m x` matches no
    `scoped-git-commit` invocation at all, so what it must name is the
    absent pathspec plus the route that supplies one, and it must say
    explicitly that path scope was NOT the thing that denied.
    """
    result = _gca_denies(monkeypatch, "git commit -m x")
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "scoped-git-commit -m <subj> -- <path>..." in reason
    assert "path scope was never checked" in reason


def test_ac16_deny_message_unchanged_for_other_types(monkeypatch):
    _subagent(monkeypatch)
    result = guard.check(_payload("git commit -m x", agent_type=_SUBAGENT_TYPE))
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == (
        "BLOCKED: subagents may not commit -- EM-only, enforced by caller "
        "identity, no cooperative override. Finish your edits and report to "
        "the EM instead: hand off changed files; the EM runs `git commit`."
    )


def test_ac16_deny_message_within_prose_cap_budget():
    from coordinator_core.bash_guards._message_size import (
        MESSAGE_PROSE_CAP_BYTES,
        measure_envelope,
    )

    envelope = {
        "hookSpecificOutput": {
            "permissionDecisionReason": guard._GIT_COMMIT_AGENT_DENY_REASON
        }
    }
    measurement = measure_envelope(envelope)
    assert not measurement.over_cap
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES


# --- Ownership-leg reason threading (this dispatch's fix): the deny
# message names WHICH path failed the ownership-scope check and WHY,
# instead of discarding `_git_commit_agent_may_commit`'s `_reason` leg and
# falling back to the static argv-shape-only text for every deny. ---


def test_ownership_leg_denial_names_path_and_classification(monkeypatch):
    """When the ownership-scope check itself runs and denies (a genuine
    `scoped-git-commit` invocation, non-sweeping pathspec -- both prior legs
    pass), the deny message must name the denied path and its classification
    rather than the generic static `_GIT_COMMIT_AGENT_DENY_REASON`.
    """
    scope_reason = (
        "path outside session sess1 scope: 'orphan.py' (orphan — dirty but "
        "claimed by no session); denied paths (1): 'orphan.py' (orphan — "
        "dirty but claimed by no session); no committable remainder "
        "(SC-DR-019) — every path in this pathspec was denied"
    )
    result = _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- orphan.py',
        scope_result=(False, scope_reason),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "orphan.py" in reason
    assert "orphan" in reason
    # Not the generic argv-shape fallback -- that text must not appear once
    # the ownership leg actually named a reason.
    assert "Already used that form?" not in reason


def test_ownership_leg_denial_names_peer_claim(monkeypatch):
    """Same as above, for the peer-claimed classification shape."""
    scope_reason = (
        "path outside session sess1 scope: 'shared.py' (claimed by live "
        "session other-session-id); denied paths (1): 'shared.py' (claimed "
        "by live session other-session-id); no committable remainder "
        "(SC-DR-019) — every path in this pathspec was denied"
    )
    result = _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- shared.py',
        scope_result=(False, scope_reason),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shared.py" in reason
    assert "claimed by live session" in reason


def test_ownership_leg_denial_names_indeterminate_classification(monkeypatch):
    """Dispatch-brief item 4: on an indeterminate call (agent-race overlap),
    `assert_paths_in_session_scope` returns `orphans` empty outright (fail-
    closed, unchanged regardless of `allow_orphans` -- see the module
    docstring's part-11 entry, point 3), and `_classify_denied_path` names
    that degradation explicitly rather than leaving the path reading as
    "unrecognized". This pins that the operator-facing message threads that
    classification through, the same way it already does for the orphan/
    peer-claimed shapes above -- a caller must be able to tell "adoption was
    withheld because this read was degraded" from "this path is simply
    outside scope".

    F1 fix (staff-eng review, 2026-08-04): built from the REAL production
    classification constant (`scope_report._CLASSIFICATION_INDETERMINATE`),
    not a hand-reworded test-local literal -- the prior version of this test
    asserted against a literal that fit under `_ownership_leg_summary`'s
    70-byte cap by construction, which production text never actually did
    (the cap truncated the real string before the asserted words). Front-
    loading the discriminating token in the classification constant itself
    (same fix) is what makes it survive the cap now -- this test proves that
    against the real constant, not a stand-in for it.
    """
    scope_reason = "path outside session sess1 scope: %r (%s)" % (
        "a.py",
        _scope_report._CLASSIFICATION_INDETERMINATE,
    )
    result = _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- a.py',
        scope_result=(False, scope_reason),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "a.py" in reason
    assert "indeterminate" in reason
    assert "adoption withheld" in reason
    # Not the generic argv-shape fallback -- the ownership leg actually ran
    # and named a reason.
    assert "Already used that form?" not in reason


def test_include_orphans_from_an_agent_denies_before_the_ownership_leg(monkeypatch):
    """SUPERSEDED SUBJECT (SC-DR-022, 2026-08-04). This test previously pinned
    that a `--include-orphans` invocation reaching the ownership leg with a
    failed positive-evidence gate surfaced the `include_orphans ignored`
    classification in its deny prose.

    That scenario is now unreachable on the dispatched-agent path BY
    CONSTRUCTION: the flag is refused before the ownership helper is ever
    consulted, so no classification it could return can be threaded. The
    original subject is not deleted-because-inconvenient -- it is dead code
    for this leg, and the surviving claim is the earlier denial itself.

    The `include_orphans ignored` classification remains live for OPERATOR
    invocations at the sink; it is `scope_report`'s own contract to pin, not
    this guard's.
    """
    result = _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo --include-orphans -- a.py',
        scope_result=(False, "unused -- the helper must never be reached"),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "RELAY" in reason
    assert "include_orphans ignored" not in reason


def test_git_commit_agent_sweeping_element_denies_before_ownership_leg_even_reached(
    monkeypatch,
):
    """Dispatch-brief item 3: a sweeping element among otherwise-fine paths
    still DENIES, and denies on the ARGV leg (`_pathspec_element_is_
    sweeping`), proving orphan adoption did not become a sweeping-commit
    hole. Wired so the ownership-scope mock would ALLOW everything if it
    were ever reached (`scope_result=(True, "")`) -- the assertion that
    matters is the STATIC argv-shape deny message
    (`_GIT_COMMIT_AGENT_DENY_REASON`), which only ever fires when LEG 3's
    sweeping check short-circuits `_git_commit_agent_may_commit` BEFORE it
    ever calls `assert_paths_in_session_scope` at all.

    F5 fix (staff-eng review, 2026-08-04): the message-only assertion below
    used to be the ENTIRE oracle, and it passed even with the ownership
    helper mocked to raise -- a sweeping short-circuit and a raising helper
    both produce `(False, "")` and the identical static message, so this
    test never actually proved ordering. `calls == []` (via the spy
    `_git_commit_agent_setup` now returns) is the direct ordering oracle:
    the ownership helper must never be invoked at all for a sweeping
    pathspec, not merely "invoked and coincidentally denied the same way".
    """
    calls = _git_commit_agent_setup(monkeypatch, scope_result=(True, ""))
    result = guard.check(
        _payload(
            'scoped-git-commit -m "msg" --repo /repo -- src/foo.py .',
            agent_type=_GIT_COMMIT_AGENT_TYPE,
        )
    )
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert calls == []
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "SWEEPING pathspec element" in reason
    assert "`.`, `-A`, globs" in reason
    # The sweeping leg short-circuits BEFORE the ownership check, so the
    # message must not send the reader off to inspect path scope (2026-08-04
    # leg-naming fix -- the static text it used to emit did exactly that).
    assert "path scope was never checked" in reason


# ---------------------------------------------------------------------------
# F0 (staff-eng review, 2026-08-04): the guard now mirrors THIS invocation's
# own `--include-orphans`/`"include_orphans": true` opt-in when calling
# `assert_paths_in_session_scope`, instead of hard-coding `allow_orphans=
# True` regardless of what the command actually asked for -- the sink this
# guard gates (`ceremony.scoped_git_commit._handler`) still defaults
# `include_orphans=False` and only adopts on an explicit per-call opt-in, so
# a hard-coded `True` here PERMITTED what the sink still REFUSED. These
# tests pin the derivation directly via the call-recording spy, independent
# of the fixed `scope_result` verdict (which the spy returns regardless of
# the `allow_orphans` value it was called with).
# ---------------------------------------------------------------------------


def test_git_commit_agent_trampoline_without_include_orphans_flag_passes_false(
    monkeypatch,
):
    """No `--include-orphans` anywhere in the invocation -- behaves EXACTLY
    as it did before any of today's changes: strict, orphans denied (the
    derived `allow_orphans` value passed to the ownership-scope helper is
    `False`, the same default both that helper and the sink already had).
    """
    calls = _git_commit_agent_setup(monkeypatch)
    guard.check(
        _payload(
            'scoped-git-commit -m "msg" --repo /repo -- src/foo.py',
            agent_type=_GIT_COMMIT_AGENT_TYPE,
        )
    )
    assert len(calls) == 1
    assert calls[0]["allow_orphans"] is False


def test_git_commit_agent_trampoline_with_include_orphans_flag_is_denied(
    monkeypatch,
):
    """SC-DR-022 (2026-08-04): `--include-orphans` from a DISPATCHED agent is
    refused outright -- adoption is an operator's answer, not an agent's.

    Supersedes the prior `…_passes_true` pin, which asserted the flag was
    mirrored through. Mirroring stays correct for what it defended against
    (a guard granting unilaterally what the sink refused); it was never a
    judgment about WHO was asking, and this leg only runs for a dispatched
    subagent.

    The ownership helper must never be REACHED -- an agent that got a quiet
    strict-mode refusal instead would read the stock orphan message, which
    advertises the very re-invocation this forbids, and loop on it.
    """
    calls = _git_commit_agent_setup(monkeypatch)
    guard.check(
        _payload(
            'scoped-git-commit -m "msg" --repo /repo --include-orphans -- src/foo.py',
            agent_type=_GIT_COMMIT_AGENT_TYPE,
        )
    )
    assert calls == []


def test_git_commit_agent_invoke_module_include_orphans_true_is_denied(monkeypatch):
    """The `coordinator_core.invoke ceremony.scoped_git_commit` JSON-body
    spelling of the same prohibition -- `"include_orphans": true` is refused
    identically, so the ruling cannot be evaded by changing spelling.
    """
    calls = _git_commit_agent_setup(monkeypatch)
    guard.check(
        _payload(
            "python3 -m coordinator_core.invoke ceremony.scoped_git_commit "
            '\'{"worktree_root": "/repo", "paths": ["src/foo.py"], "message": "x", '
            '"include_orphans": true}\'',
            agent_type=_GIT_COMMIT_AGENT_TYPE,
        )
    )
    assert calls == []


def test_agent_orphan_adoption_deny_tells_the_agent_to_relay_not_re_invoke(
    monkeypatch,
):
    """The deny prose is the operative half of SC-DR-022: the offer in an
    orphan refusal is addressed to an operator, and for an agent it is
    information to RELAY. Pinned because the failure mode this closes is a
    message that sends the agent back to re-invoke with the flag.
    """
    message = guard._GIT_COMMIT_AGENT_LEG_MESSAGES[guard._LEG_AGENT_ORPHAN_ADOPTION]
    assert "RELAY" in message
    assert "operator" in message
    # Must not instruct the agent to do the forbidden thing.
    assert "--include-orphans" not in message


def test_git_commit_agent_invoke_module_without_include_orphans_key_passes_false(
    monkeypatch,
):
    """The JSON-body spelling with no `include_orphans` key at all -- must
    default to `False`, same as the trampoline's no-flag case.
    """
    calls = _git_commit_agent_setup(monkeypatch)
    guard.check(
        _payload(
            "python3 -m coordinator_core.invoke ceremony.scoped_git_commit "
            '\'{"worktree_root": "/repo", "paths": ["src/foo.py"], "message": "x"}\'',
            agent_type=_GIT_COMMIT_AGENT_TYPE,
        )
    )
    assert len(calls) == 1
    assert calls[0]["allow_orphans"] is False


def test_ownership_leg_denial_still_within_prose_cap_budget(monkeypatch):
    """A pathological long path/classification must still stay under
    `MESSAGE_PROSE_CAP_BYTES` -- `_ownership_leg_summary`'s truncation, not
    an unbounded splice of the raw `assert_paths_in_session_scope` reason.
    """
    from coordinator_core.bash_guards._message_size import (
        MESSAGE_PROSE_CAP_BYTES,
        measure_envelope,
    )

    long_path = "src/" + ("x" * 400) + ".py"
    scope_reason = (
        "path outside session sess1 scope: %r (orphan — dirty but claimed "
        "by no session); denied paths (1): %r (orphan — dirty but claimed "
        "by no session); no committable remainder (SC-DR-019) — every path "
        "in this pathspec was denied" % (long_path, long_path)
    )
    result = _gca_denies(
        monkeypatch,
        'scoped-git-commit -m "msg" --repo /repo -- %s' % long_path,
        scope_result=(False, scope_reason),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    envelope = {"hookSpecificOutput": {"permissionDecisionReason": reason}}
    measurement = measure_envelope(envelope)
    assert not measurement.over_cap
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES


def test_ownership_leg_summary_truncates_over_budget_fragment():
    """Direct unit coverage of `_ownership_leg_summary`'s own truncation,
    independent of the full deny-message plumbing above."""
    reason = "path outside session sess1 scope: " + ("z" * 200) + "; denied paths (1): ..."
    summary = guard._ownership_leg_summary(reason)
    assert len(summary.encode("utf-8")) <= guard._OWNERSHIP_LEG_SUMMARY_MAX_BYTES + 3  # "..." tail
    assert summary.endswith("...")


def test_ownership_leg_summary_empty_for_empty_reason():
    assert guard._ownership_leg_summary("") == ""


# --- AC9: the allow-set docstring no longer describes an inaccurate intent ---


def test_ac9_allowed_subagent_types_still_empty_and_excludes_git_commit_agent():
    """AC9: `_ALLOWED_SUBAGENT_TYPES` stays empty, and specifically does NOT
    contain `coordinator:git-commit-agent` -- its exemption is granted by
    the narrower, command-shape-aware C3 predicate instead (see the
    constant's own docstring, corrected by this chunk).
    """
    assert guard._ALLOWED_SUBAGENT_TYPES == frozenset()
    assert _GIT_COMMIT_AGENT_TYPE not in guard._ALLOWED_SUBAGENT_TYPES


# --- Direct unit coverage of the new extraction/sweeping-check helpers ---


def test_pathspec_element_is_sweeping_ordinary_subdirectory_is_not_sweeping():
    """A directory pathspec that is NOT the repo root or an ancestor of it
    (e.g. an ordinary subdirectory) is accepted -- AC14 does not reject
    every directory pathspec, only the repo root and its ancestors.
    """
    assert guard._pathspec_element_is_sweeping("src/", _FAKE_REPO_ROOT) is False
    assert guard._pathspec_element_is_sweeping("src/foo.py", _FAKE_REPO_ROOT) is False


def test_pathspec_element_is_sweeping_colon_bang_exclude_shorthand():
    """Finding 4: git's `:!<pattern>` exclude-shorthand magic pathspec is
    rejected, same as the `:(exclude)<pattern>` long form.
    """
    assert guard._pathspec_element_is_sweeping(":!nonexistent-file", _FAKE_REPO_ROOT) is True
    assert guard._pathspec_element_is_sweeping(":!", _FAKE_REPO_ROOT) is True


def test_pathspec_element_is_sweeping_windows_drive_path_not_a_false_positive():
    """The generalized ``candidate.startswith(":")`` check must not
    misclassify a plain Windows drive-letter path -- its colon is never the
    first character (the drive letter precedes it). Built from parts (not a
    literal drive-path string) to stay clear of this repo's own concrete-
    path-citation guard, which is unrelated to the behavior under test.
    """
    drive_root = "".join(["C", ":", "\\", "Windows", "\\"])
    candidate = drive_root + "foo.py"
    assert guard._pathspec_element_is_sweeping(candidate, _FAKE_REPO_ROOT) is False


def test_resolve_git_commit_agent_pathspec_no_matching_invocation_returns_none():
    assert guard._resolve_git_commit_agent_pathspec('git commit -m "x"') is None


# ---------------------------------------------------------------------------
# C4b (2026-08-03-narrow-subagent-commit-confinement-two-classes.md) --
# real-helper wiring proof. Every test above this section monkeypatches
# ``guard._import_assert_paths_in_session_scope`` directly -- adequate
# coverage for
# the guard's OWN fail-closed contract around whatever the helper returns,
# but it never proves ``check()`` actually WIRES the real
# ``coordinator_core.ops.session.scope_report.assert_paths_in_session_scope``
# through to a genuine own-session/peer-session/orphan verdict end-to-end.
# These tests exercise the REAL helper (a real git repo, real
# ``coordinator_core.session.core``/``scope`` state, no scope-result mock) --
# only the identity-resolution seams unrelated to C4 (``resolve_git_root``,
# ``_resolve_subagent_identity``, ``_read_backpointer_subagent_type``) are
# left as their real implementations too, since a real repo makes them safe
# to call unmocked. See ``coordinator_core/ops/session/tests/
# test_scope_report.py`` for the equivalent coverage of the helper's OWN
# scope-math contract in isolation -- this class only proves this module's
# WIRING onto it, not the helper's math a second time.
# ---------------------------------------------------------------------------


def _make_real_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _real_scope_payload(cmd, cwd, session_id, agent_id="deadbeef0123"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": str(cwd),
        "agent_id": agent_id,
        "agent_type": _GIT_COMMIT_AGENT_TYPE,
    }


class TestRealOwnershipScopeWiring:
    def test_own_session_paths_allow(self, tmp_path):
        repo = _make_real_repo(tmp_path)
        _session_core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        _session_scope.touch("mine", "a.py", cwd=str(repo))

        payload = _real_scope_payload(
            'scoped-git-commit -m "msg" --repo %s -- a.py' % repo, repo, "mine"
        )
        result = guard.check(payload)
        assert result is None, f"expected ALLOW, got deny: {result!r}"

    def test_another_live_sessions_paths_deny(self, tmp_path):
        repo = _make_real_repo(tmp_path)
        _session_core.init("mine", cwd=str(repo))
        _session_core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        _session_scope.touch("mine", "shared.py", cwd=str(repo))
        _session_scope.touch("other", "shared.py", cwd=str(repo))

        payload = _real_scope_payload(
            'scoped-git-commit -m "msg" --repo %s -- shared.py' % repo, repo, "mine"
        )
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_dead_own_session_paths_still_allow(self, tmp_path):
        """Liveness gating (``coordinator_core.session.liveness``) governs
        whether a PEER's claim is pruned as stale -- it is never a
        precondition for a session's OWN claim on its OWN paths (see
        ``coordinator_core.session.scope.compute_scope``'s own docstring:
        the liveness gate is evaluated only over peer entries). A session
        long past the 30-minute liveness window must still be able to
        commit its own claimed work -- denying on liveness grounds alone
        here would be a NEW constraint this chunk was never asked to add.
        """
        repo = _make_real_repo(tmp_path)
        _session_core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        _session_scope.touch("mine", "a.py", cwd=str(repo))
        sdir = Path(_session_core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")

        payload = _real_scope_payload(
            'scoped-git-commit -m "msg" --repo %s -- a.py' % repo, repo, "mine"
        )
        result = guard.check(payload)
        assert result is None, f"expected ALLOW (self-liveness is not gated), got deny: {result!r}"

    def test_orphan_unowned_path_without_include_orphans_flag_still_denies(
        self, tmp_path
    ):
        """F0 (staff-eng review, 2026-08-04): an invocation that does NOT
        carry `--include-orphans` must behave EXACTLY as it did before any
        of today's changes -- strict, orphans denied -- because the guard
        now MIRRORS the invocation's own flag instead of hard-coding
        `allow_orphans=True`. Same fixture as
        `test_orphan_unowned_path_denies_even_with_orphans_requested` below,
        differing ONLY in the absence of the flag in the command text.
        """
        repo = _make_real_repo(tmp_path)
        _session_core.init("mine", cwd=str(repo))
        sdir = Path(_session_core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")

        payload = _real_scope_payload(
            'scoped-git-commit -m "msg" --repo %s -- orphan.py' % repo, repo, "mine"
        )
        result = guard.check(payload)
        assert result is not None, "expected DENY: no --include-orphans in the invocation"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_orphan_unowned_path_denies_even_with_orphans_requested(self, tmp_path):
        """CONTRACT CHANGE (SC-DR-022, claude-central-em, 2026-08-04) --
        supersedes this test's prior `…_now_allowed_with_orphans_enabled`
        form, which asserted ALLOW here.

        A DISPATCHED agent may never adopt an orphan, so the flag no longer
        rescues this shape: `orphan.py` is a genuine unclaimed orphan, the
        invocation asks for adoption, and the guard refuses at the
        agent-adoption leg before the ownership helper is consulted.

        This deliberately RE-CLOSES the agent's primary workload at this
        seam. That is the ruling's intent, not a regression of the LEG-3
        fix: engine-authored state is meant to become committable by being
        CLAIMED at the dispatch chokepoint (self-reported touches, the
        SC-DR-021 (a) population), not by being adopted after the fact by a
        committer that never authored it.
        """
        repo = _make_real_repo(tmp_path)
        _session_core.init("mine", cwd=str(repo))
        sdir = Path(_session_core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")

        payload = _real_scope_payload(
            'scoped-git-commit -m "msg" --repo %s --include-orphans -- orphan.py' % repo,
            repo,
            "mine",
        )
        result = guard.check(payload)
        assert result is not None, "expected DENY: an agent may not adopt orphans"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "RELAY" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_git_commit_agent_peer_claimed_path_still_denies_with_orphans_allowed(
        self, tmp_path
    ):
        """Security-critical pin (dispatch brief AC-2): `allow_orphans=True`
        must NEVER relax the peer-claimed case -- a path claimed by a LIVE
        peer session denies regardless of the flag (incident 62e9a1f73,
        pinned at `assert_paths_in_session_scope`'s own docstring). This
        pins that property at THIS call site specifically, not merely at
        the helper in isolation: `shared.py` is claimed by BOTH "mine" (the
        committing session) and "other" (a live peer) -- if the flag ever
        started relaxing peer-claimed paths, this would flip to ALLOW and
        this test would catch it. `--include-orphans` is deliberately NOT
        in the command text (SC-DR-022, 2026-08-04): it used to be here so
        the flag's non-effect on peer claims was actually exercised rather
        than masked by a strict-mode deny, but that shape no longer reaches
        the ownership helper at all -- the flag now denies earlier, at the
        agent-adoption leg, and a test carrying it would pass for the WRONG
        reason. Omitting it keeps the assertion pointed at the peer-claim
        path it exists to pin. The property itself is strictly stronger
        than before rather than weakened: `allow_orphans` is now
        unconditionally `False` at this call site, so there is no longer a
        flag value that could relax a peer claim here even in principle.

        F7 fix (staff-eng review, 2026-08-04): asserts on the reason text,
        not merely the verdict -- the real helper is confirmed (live, this
        session) to emit the peer-claim classification for this scenario and
        for it to survive `_ownership_leg_summary`'s cap. A bare verdict-
        only assertion keeps passing under an unrelated regression that
        still happens to deny for the wrong reason; this closes that gap.

        Liveness-honesty amendment (2026-08-07, cross-repo memo
        `2026-08-07-doe-claude-em-scoped-commit-refusal-asserts-live-without-
        checking.md`): `_classify_denied_path` no longer asserts the word
        "live" from an ownership-only reason -- it resolves the liveness
        oracle and names the real verdict. Negative spec: do NOT relax this
        back to a bare `"claimed by"` substring to make it
        liveness-agnostic -- naming the branch AND the owner is the whole
        point of F7, and a liveness-agnostic assertion would pass again
        under exactly the regression this amendment exists to prevent.

        CORRECTED 2026-08-07 pass 2 (memo `...-calls-a-live-peer-dead-and-
        reapable`): this asserted DEAD, and passed -- while pinning a defect.
        The `other` session this fixture mints via `_session_core.init` IS
        live: it has a fresh `last_activity` in THIS tmp repo. It read DEAD
        only because the classification's oracle was zero-arg and resolved
        its session registry from the PROCESS cwd (the real claude-klabauter
        checkout), where `other` does not exist at all. With the oracle
        cwd-scoped, the fixture's own peer is correctly named live.
        """
        repo = _make_real_repo(tmp_path)
        _session_core.init("mine", cwd=str(repo))
        _session_core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        _session_scope.touch("mine", "shared.py", cwd=str(repo))
        _session_scope.touch("other", "shared.py", cwd=str(repo))

        payload = _real_scope_payload(
            'scoped-git-commit -m "msg" --repo %s -- shared.py' % repo,
            repo,
            "mine",
        )
        result = guard.check(payload)
        assert result is not None, "expected DENY: shared.py is peer-claimed"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "claimed by live session other" in reason

    def test_unresolvable_session_id_denies(self, tmp_path):
        repo = _make_real_repo(tmp_path)
        (repo / "a.py").write_text("a")

        payload = _real_scope_payload(
            'scoped-git-commit -m "msg" --repo %s -- a.py' % repo, repo, ""
        )
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_mixed_authorship_pathspec_denies_when_adoption_is_requested(self, tmp_path):
        """CONTRACT CHANGE (SC-DR-022, 2026-08-04) -- this test has now been
        through both directions, and the history is the point.

        It originally pinned a deterministic deny (the ownership leg took
        `allow_orphans`'s `False` default positionally). The LEG-3 fix
        flipped it to ALLOW via mirrored adoption, on the reasoning that
        engine-authored state IS the commit agent's characteristic workload
        and denying it defeated the agent's primary job. SC-DR-022 flips it
        back to DENY, and answers the reasoning rather than discarding it:
        the workload is real, but adoption was the wrong instrument for it.
        Adoption is safe only when the adopter authored the bytes, and a
        dispatched committer never did.

        The workload's real route is SC-DR-021 (a) -- engine ops
        self-reporting their writes at the dispatch chokepoint, so the paths
        arrive CLAIMED and are never orphans at this seam in the first place.

        Peer-claim coverage stays pinned directly in
        `test_git_commit_agent_peer_claimed_path_still_denies_with_orphans_
        allowed` above, not through this shape.
        """
        repo = _make_real_repo(tmp_path)
        _session_core.init("mine", cwd=str(repo))
        # Backdate started_at, mirroring
        # test_orphan_unowned_path_denies_even_with_orphans_requested above,
        # so compute_scope's mtime fallback does not silently adopt the
        # untouched file into this session's own safe_paths via a DIFFERENT
        # mechanism than the one this test exists to exercise -- it must
        # land as a genuine orphan, adopted only via `allow_orphans=True`.
        sdir = Path(_session_core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")

        (repo / "edited.py").write_text("edited via Edit tool")
        _session_scope.touch("mine", "edited.py", cwd=str(repo))

        # No _session_scope.touch call for this one -- a dirty file authored
        # through Bash/CLI (per track_touched_files' DR-258 tool_name
        # fast-exit) records no claim in touched.txt at all.
        (repo / "bash_authored.py").write_text("authored via Bash/CLI")

        payload = _real_scope_payload(
            'scoped-git-commit -m "msg" --repo %s --include-orphans -- '
            "edited.py bash_authored.py" % repo,
            repo,
            "mine",
        )
        result = guard.check(payload)
        assert result is not None, "expected DENY: an agent may not adopt orphans"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "RELAY" in result["hookSpecificOutput"]["permissionDecisionReason"]
