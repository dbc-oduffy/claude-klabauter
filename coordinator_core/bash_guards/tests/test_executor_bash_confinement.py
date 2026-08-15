"""Regression pins for ``coordinator:executor``'s Bash confinement history
via ``block_reviewer_bash_outside_allowlist``.

``coordinator:executor`` was a member of ``_helpers._CONFINED_FINDINGS_AGENTS``
from 2026-08-01 (Amendment 1, docs/plans/2026-08-01-confine-subagent-bash-by-
allowlist.md) to 2026-08-03, when it was REMOVED again on the PM ruling
recorded at DR-125 (docs/plans/2026-08-03-narrow-subagent-commit-confinement-
two-classes.md, chunk C2): Bash-allowlist confinement is the wrong control
for a commit-shaped bypass once ``block_subagent_commit`` independently
denies the commit/coordinator-safe-commit/invoke shape regardless of how it
is invoked, and ``block_subagent_destructive_action`` independently denies
force-push/stash/reset regardless of confinement membership here.

This file's own history, preserved for a future reader diffing the two
chunks:

  - ``coordinator:executor`` is the FIRST (and, post-C2, ONLY former) member
    of ``_helpers._CONFINED_FINDINGS_AGENTS`` besides
    ``coordinator:code-reviewer`` -- see that module's docstring item 2(a)
    and this guard module's own docstring (Divergence 9) for the Amendment 1
    interpreter-discrimination design this file used to exercise while
    executor was confined.
  - Three buckets of post-C2 disposition apply (chunk C2's three-way rule,
    not a blanket inversion):

      1. RE-KEYED to ``coordinator:code-reviewer`` -- cases asserting a
         confinement-POLICY property that must survive regardless of WHICH
         type is confined (``test_python3_dash_c_denies_even_with_attacker_
         shaped_ruleset``, its ``-e`` sibling,
         ``test_python3_dash_m_unlisted_module_denies``). Inverting these to
         "allows" would assert nothing; the property they protect still
         matters for the type that remains confined.
      2. REWRITTEN to a two-guard assertion -- the commit/push/stash/reset
         cases. This guard (``block_reviewer_bash_outside_allowlist``) no
         longer denies an executor for these shapes at all (empirically
         confirmed: it no-ops unconditionally for a non-confined type), but
         a plain "commit/push/stash/reset now allows" claim is FACTUALLY
         WRONG for the shapes a DIFFERENT guard still independently denies
         -- ``block_subagent_commit`` for commit/coordinator-safe-commit/the
         ``coordinator_core.invoke`` committing-op shape,
         ``block_subagent_destructive_action`` for force-push/bare-stash/
         ``reset --hard``. A PLAIN (non-force) ``git push`` is the one shape
         in this family that becomes genuinely, fully allowed post-C2 --
         verified empirically (see ``test_git_push_denies`` below) --
         consistent with DR-125's own framing ("confinement only for
         destructive actions that would degrade a machine, or a commit"): a
         non-force push is neither.
      3. INVERTED -- plain allowlist allow/deny shape cases with no
         confinement-policy or commit/destructive-guard entanglement.
         ``coordinator:executor`` is simply no longer confined by THIS guard
         at all; ``coordinator:code-reviewer`` still is.

Follows the payload/monkeypatch idiom of the sibling oracle
``test_block_reviewer_bash_outside_allowlist.py`` (``_confine`` patches the
guard's own identity-resolution seam so no real git repo / back-pointer
chain on disk is required).

Spec backlink: coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py
docs/plans/2026-08-01-confine-subagent-bash-by-allowlist.md § Amendment 1, C1
docs/plans/2026-08-03-narrow-subagent-commit-confinement-two-classes.md § C2
"""

from __future__ import annotations

from typing import Any, Dict

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)
from coordinator_core.bash_guards import block_subagent_commit as commit_guard
from coordinator_core.bash_guards import (
    block_subagent_destructive_action as destructive_guard,
)
from coordinator_core.bash_guards import _helpers

_CONFINED_TYPE = "coordinator:executor"
_REVIEWER_TYPE = "coordinator:code-reviewer"


def _payload(command: str, agent_id: str = "deadbeef0123", agent_type: str = _CONFINED_TYPE) -> Dict[str, Any]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
        "agent_id": agent_id,
        "agent_type": agent_type,
    }


#: 2026-08-11 order-dependency fix -- see this module's own docstring
#: addendum below (`_FAKE_ENUMERATED_TYPES`) for the incident this closes.
_FAKE_ENUMERATED_TYPES = frozenset({_CONFINED_TYPE, _REVIEWER_TYPE})


def _fake_is_confined_by_roster_absence(effective_type: str) -> bool:
    """Deterministic stand-in for ``_helpers.is_confined_by_roster_absence``.

    2026-08-11 (order-dependency fix, this dispatch): every test in this
    module previously left ``block_reviewer_bash_outside_allowlist``'s leg-3
    roster check (``_is_confined_type`` -> ``is_confined_by_roster_absence``
    -> the REAL, unmocked ``resolve_roster()``) unmocked, so the allow/deny
    verdict this file pins depended on ambient process state this suite does
    not control: whether ``resolve_roster()`` can actually resolve the DoE
    root and read its roster sources from THIS process's environment.
    ``coordinator_core/conftest.py``'s autouse ``_quarantine_real_home``
    fixture repoints ``HOME``/``USERPROFILE`` at a throwaway tmp dir for
    every test in the suite, which -- absent a ``COORDINATOR_SETTINGS_HOME``
    override surviving that quarantine -- makes ``read_doe_root_pointer()``
    resolve to ``""`` and ``resolve_roster()`` fail closed (``roster is
    None``), which makes ``is_confined_by_roster_absence`` return ``True``
    for EVERY non-empty ``effective_type`` -- including
    ``coordinator:executor``, which this file exists to pin as UNconfined.
    Reproduced deterministically (100% of runs, not merely "sometimes") on a
    machine with no ``COORDINATOR_SETTINGS_HOME`` surviving the quarantine;
    plausibly order/host-dependent on a machine where some earlier fixture
    or test leaks a working override into a later test's process state.
    Either way, a real-disk-dependent leg has no place deciding a pinned
    allow/deny table -- this test module already monkeypatches the OTHER two
    identity-resolution legs (``resolve_git_root``,
    ``_read_backpointer_subagent_type``) for exactly this reason, and leg 3
    was simply the one gap. Fixed by injecting a deterministic stand-in
    keyed on the SAME two types (``coordinator:executor``,
    ``coordinator:code-reviewer``) this file's own payloads ever construct --
    both are genuinely enumerated on a healthy roster, so this mirrors what
    ``resolve_roster()`` returns when it CAN resolve, without this test
    suite depending on whether it actually can on any given host or run.
    """
    if not effective_type:
        return False
    return effective_type not in _FAKE_ENUMERATED_TYPES


def _confine(monkeypatch, subagent_type: str = _CONFINED_TYPE) -> None:
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, **kw: subagent_type,
    )
    monkeypatch.setattr(
        guard, "is_confined_by_roster_absence", _fake_is_confined_by_roster_absence
    )


def _wire_other_guard(monkeypatch, other_guard, subagent_type: str = _CONFINED_TYPE) -> None:
    """Same identity-resolution seam as ``_confine``, but on a DIFFERENT
    guard module's own imported attributes -- ``block_subagent_commit`` and
    ``block_subagent_destructive_action`` each import ``resolve_git_root``/
    ``_resolve_subagent_identity``/``_read_backpointer_subagent_type`` into
    their own namespace, so patching ``guard``'s copy (the bash-allowlist
    guard) does not reach them."""
    monkeypatch.setattr(other_guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        other_guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        other_guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, **kw: subagent_type,
    )


def _assert_denied(result):
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _assert_allowed(result):
    assert result is None


# ---------------------------------------------------------------------------
# Bucket 2 (chunk C2 three-way rule) -- commit/push/stash/reset shapes.
# This guard (block_reviewer_bash_outside_allowlist) now allows an executor
# unconditionally; the two-guard assertion is that a DIFFERENT, still-live
# guard independently denies the shapes that remain genuinely dangerous.
# ---------------------------------------------------------------------------


def test_git_commit_denies(monkeypatch):
    """block_reviewer_bash_outside_allowlist no longer confines executor;
    block_subagent_commit still denies the commit shape regardless."""
    _confine(monkeypatch)
    _wire_other_guard(monkeypatch, commit_guard)
    payload = _payload('git commit -m "x"')
    _assert_allowed(guard.check(payload))
    _assert_denied(commit_guard.check(payload))


def test_coordinator_safe_commit_denies(monkeypatch):
    _confine(monkeypatch)
    _wire_other_guard(monkeypatch, commit_guard)
    payload = _payload("coordinator-safe-commit --expected-owner em-only")
    _assert_allowed(guard.check(payload))
    _assert_denied(commit_guard.check(payload))


def test_invoke_committing_op_denies(monkeypatch):
    _confine(monkeypatch)
    _wire_other_guard(monkeypatch, commit_guard)
    payload = _payload(
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit"
    )
    _assert_allowed(guard.check(payload))
    _assert_denied(commit_guard.check(payload))


def test_git_push_denies(monkeypatch):
    """A PLAIN (non-force) push is the one shape in this family that becomes
    genuinely, fully allowed post-C2 -- neither block_reviewer_bash_outside_
    allowlist (executor unconfined) nor block_subagent_commit (push is not a
    commit) nor block_subagent_destructive_action (only a FORCING push form
    is denied -- see that guard's own ``_PUSH_FORCE_RE``/`_PUSH_WORD_RE``
    ladder) treats a plain push as dangerous. This is not a gap: DR-125's
    own framing scopes confinement to "destructive... or a commit", and a
    plain push is neither. A force push remains denied via
    block_subagent_destructive_action, pinned separately below."""
    _confine(monkeypatch)
    _wire_other_guard(monkeypatch, destructive_guard)
    payload = _payload("git push origin main")
    _assert_allowed(guard.check(payload))
    _assert_allowed(destructive_guard.check(payload))


def test_git_push_force_denies(monkeypatch):
    """The genuinely destructive push shape -- force-push -- still denies,
    via block_subagent_destructive_action, unaffected by C2."""
    _confine(monkeypatch)
    _wire_other_guard(monkeypatch, destructive_guard)
    payload = _payload("git push --force origin main")
    _assert_allowed(guard.check(payload))
    _assert_denied(destructive_guard.check(payload))


def test_git_stash_denies(monkeypatch):
    _confine(monkeypatch)
    _wire_other_guard(monkeypatch, destructive_guard)
    payload = _payload("git stash")
    _assert_allowed(guard.check(payload))
    _assert_denied(destructive_guard.check(payload))


def test_git_reset_denies(monkeypatch):
    _confine(monkeypatch)
    _wire_other_guard(monkeypatch, destructive_guard)
    payload = _payload("git reset --hard")
    _assert_allowed(guard.check(payload))
    _assert_denied(destructive_guard.check(payload))


# ---------------------------------------------------------------------------
# Bucket 1 (chunk C2 three-way rule) -- confinement-POLICY properties,
# re-keyed to coordinator:code-reviewer (the type that remains confined).
# Inverting these to "allows" would assert nothing -- the property they
# protect still matters for the type this guard still confines.
# ---------------------------------------------------------------------------


def test_python3_dash_c_denies_even_with_attacker_shaped_ruleset(monkeypatch):
    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)

    def _attacker_ruleset(effective_type: str, policy) -> Dict[str, Any]:
        ruleset = guard._default_ruleset(effective_type)
        ruleset["interpreter_allow_scripts"] = True
        ruleset["interpreter_allowed_modules"] = (
            "os",
            "subprocess",
            "coordinator_core.invoke",
        )
        return ruleset

    monkeypatch.setattr(guard, "_resolve_ruleset", _attacker_ruleset)
    payload = _payload(
        'python3 -c "import os; os.system(\'rm -rf /\')"', agent_type=_REVIEWER_TYPE
    )
    reason = _assert_denied(guard.check(payload))
    assert "-c" in reason


def test_python3_dash_e_denies_even_with_attacker_shaped_ruleset(monkeypatch):
    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)

    def _attacker_ruleset(effective_type: str, policy) -> Dict[str, Any]:
        ruleset = guard._default_ruleset(effective_type)
        ruleset["interpreter_allow_scripts"] = True
        ruleset["interpreter_allowed_modules"] = (
            "os",
            "subprocess",
            "coordinator_core.invoke",
        )
        return ruleset

    monkeypatch.setattr(guard, "_resolve_ruleset", _attacker_ruleset)
    payload = _payload('python3 -e "print(1)"', agent_type=_REVIEWER_TYPE)
    _assert_denied(guard.check(payload))


def test_python3_dash_m_unlisted_module_denies(monkeypatch):
    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload("python3 -m http.server", agent_type=_REVIEWER_TYPE)
    reason = _assert_denied(guard.check(payload))
    assert "http.server" in reason


# ---------------------------------------------------------------------------
# Bucket 3 (chunk C2 three-way rule) -- plain allowlist allow/deny shape
# cases with no confinement-policy or commit/destructive-guard entanglement.
# "executor IS confined" becomes "executor is NOT confined while
# coordinator:code-reviewer still IS" -- both halves asserted per test.
# ---------------------------------------------------------------------------


def test_python3_dash_c_inline_code_allows_executor_denies_reviewer(monkeypatch):
    cmd = 'python3 -c "import os; os.system(\'rm -rf /\')"'
    _confine(monkeypatch)
    _assert_allowed(guard.check(_payload(cmd)))

    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    reason = _assert_denied(guard.check(_payload(cmd, agent_type=_REVIEWER_TYPE)))
    assert "-c" in reason


def test_python3_dash_e_inline_code_allows_executor_denies_reviewer(monkeypatch):
    cmd = 'python3 -e "print(1)"'
    _confine(monkeypatch)
    _assert_allowed(guard.check(_payload(cmd)))

    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    _assert_denied(guard.check(_payload(cmd, agent_type=_REVIEWER_TYPE)))


# ---------------------------------------------------------------------------
# AC2 -- an executor's legitimate work is unaffected. Unchanged by C2: this
# guard already allowed these shapes for executor before, and continues to
# (now unconditionally, rather than via a ruleset carve-out).
# ---------------------------------------------------------------------------


def test_python3_dash_m_pytest_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("python3 -m pytest -q")
    assert guard.check(payload) is None


def test_python3_dash_m_pytest_with_stderr_redirect_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("python3 -m pytest -q 2>&1")
    assert guard.check(payload) is None


def test_python3_script_path_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("python3 myscript.py")
    assert guard.check(payload) is None


def test_readonly_git_status_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("git status")
    assert guard.check(payload) is None


def test_readonly_git_diff_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("git diff HEAD~1")
    assert guard.check(payload) is None


def test_readonly_git_log_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("git log -5")
    assert guard.check(payload) is None


def test_readonly_git_show_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("git show HEAD")
    assert guard.check(payload) is None


def test_readonly_git_rev_parse_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("git rev-parse --show-toplevel")
    assert guard.check(payload) is None


def test_ls_no_write_flags_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("ls -la coordinator_core/bash_guards")
    assert guard.check(payload) is None


def test_grep_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload('grep -rn "foo" coordinator_core')
    assert guard.check(payload) is None


def test_find_no_write_flags_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload('find . -name "*.py"')
    assert guard.check(payload) is None


def test_coordinator_doc_new_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload(
        "/x/claude-klabauter/coordinator/bin/coordinator-doc-new.py --type run-report"
    )
    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Bucket 3 -- machine-local Tier A. Was policy-gated to executor's ruleset
# while confined; now a plain "no confinement at all" allow, dual-asserted
# against coordinator:code-reviewer (still confined, still denies write
# subcommands).
# ---------------------------------------------------------------------------


def test_machine_local_get_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("machine-local get repos.doe_claude")
    assert guard.check(payload) is None


def test_machine_local_has_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("machine-local has repos.doe_claude")
    assert guard.check(payload) is None


def test_machine_local_keys_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("machine-local keys repos")
    assert guard.check(payload) is None


def test_machine_local_path_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("machine-local path repos.doe_claude")
    assert guard.check(payload) is None


def test_machine_local_dir_allows(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("machine-local dir repos.doe_claude")
    assert guard.check(payload) is None


def test_machine_local_set_allowlist_allows_executor_but_destructive_guard_denies_both(
    monkeypatch,
):
    """RENAMED (DR-125, 2026-08-03): the pre-fix asymmetry this test used to
    pin -- executor fully allowed, reviewer denied -- was Finding 3 of the
    C2 code review: `machine-local set` was reachable for an executor
    because THIS guard (the allowlist confinement) was the only guard that
    ever named `machine-local` at all. `block_subagent_destructive_action`
    now denies the write shape for EVERY resolved subagent type, keyed on
    command shape, not on `_CONFINED_FINDINGS_AGENTS` membership -- so this
    guard's own allow/deny split (unaffected by that fix, still asserted
    below) is no longer the only guard in the loop, and the sentence
    "executor allows" is no longer true end-to-end."""
    cmd = "machine-local set repos.doe_claude /x/evil"
    _confine(monkeypatch)
    _wire_other_guard(monkeypatch, destructive_guard)
    _assert_allowed(guard.check(_payload(cmd)))
    _assert_denied(destructive_guard.check(_payload(cmd)))

    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    _wire_other_guard(monkeypatch, destructive_guard, subagent_type=_REVIEWER_TYPE)
    _assert_denied(guard.check(_payload(cmd, agent_type=_REVIEWER_TYPE)))
    _assert_denied(destructive_guard.check(_payload(cmd, agent_type=_REVIEWER_TYPE)))


def test_machine_local_array_append_allows_executor_denies_reviewer(monkeypatch):
    cmd = "machine-local array-append repos.mirrors /x/evil"
    _confine(monkeypatch)
    _assert_allowed(guard.check(_payload(cmd)))

    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    _assert_denied(guard.check(_payload(cmd, agent_type=_REVIEWER_TYPE)))


def test_machine_local_array_set_allows_executor_denies_reviewer(monkeypatch):
    cmd = "machine-local array-set repos.mirrors 0 /x/evil"
    _confine(monkeypatch)
    _assert_allowed(guard.check(_payload(cmd)))

    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    _assert_denied(guard.check(_payload(cmd, agent_type=_REVIEWER_TYPE)))


def test_machine_local_migrate_publish_mirrors_allows_executor_denies_reviewer(monkeypatch):
    cmd = "machine-local migrate-publish-mirrors"
    _confine(monkeypatch)
    _assert_allowed(guard.check(_payload(cmd)))

    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    _assert_denied(guard.check(_payload(cmd, agent_type=_REVIEWER_TYPE)))


def test_machine_local_bare_no_subcommand_allows_executor_denies_reviewer(monkeypatch):
    cmd = "machine-local"
    _confine(monkeypatch)
    _assert_allowed(guard.check(_payload(cmd)))

    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    _assert_denied(guard.check(_payload(cmd, agent_type=_REVIEWER_TYPE)))


# ---------------------------------------------------------------------------
# AC3/AC4 -- coordinator:code-reviewer's existing confinement behaviour is
# bit-for-bit unchanged by narrowing this set. Structural confirmation that
# this guard's ruleset overrides are keyed strictly by effective_type: a
# reviewer-typed payload must NOT pick up executor's former interpreter/
# script allowances, and removing executor from the SET must not itself
# alter a single reviewer verdict.
# ---------------------------------------------------------------------------


def test_reviewer_type_unaffected_by_executor_interpreter_allowance(monkeypatch):
    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload("python3 myscript.py", agent_type=_REVIEWER_TYPE)
    _assert_denied(guard.check(payload))


def test_reviewer_type_shares_the_executors_pytest_module_allowance(monkeypatch):
    """Pin the ``interpreter_allowed_modules`` half of the executor/reviewer
    ruleset, which the script-path test above does not reach.

    (Amendment 2, 2026-08-03, PM ruling: "bash confinement should only be for
    destructive actions that would degrade a machine.") ``coordinator:code-
    reviewer`` shares the SAME ``("pytest",)`` module allowance the former
    ``coordinator:executor`` ruleset override carried, unaffected by C2's
    later removal of executor from the SET membership itself -- that
    per-type ruleset resolution (Divergence 9) is orthogonal to SET
    membership and this test's own scope.
    """
    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload("python3 -m pytest -q", agent_type=_REVIEWER_TYPE)
    assert guard.check(payload) is None


def test_reviewer_type_python3_dash_c_still_denies(monkeypatch):
    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload(
        'python3 -c "import os; os.system(\'rm -rf /\')"',
        agent_type=_REVIEWER_TYPE,
    )
    reason = _assert_denied(guard.check(payload))
    assert "-c" in reason


def test_reviewer_type_unlisted_module_still_denies(monkeypatch):
    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload("python3 -m http.server", agent_type=_REVIEWER_TYPE)
    reason = _assert_denied(guard.check(payload))
    assert "http.server" in reason


#: AC4 (chunk C2, load-bearing) -- the full allow/deny verdict table this
#: file's own tests exercise for coordinator:code-reviewer, captured with
#: coordinator:executor's SET membership restored (simulating pre-C2) and
#: compared byte-identical against the SAME table read off the real,
#: on-disk (post-C2) module state. A change to ANY reviewer verdict here
#: would mean narrowing the SET for executor had a side effect on the type
#: that stays confined -- exactly the property C2's body text names as the
#: load-bearing check.
_AC4_REVIEWER_VERDICT_COMMANDS = (
    "git status",
    "git show HEAD",
    "git log -5",
    "git diff HEAD~1",
    "git rev-parse --show-toplevel",
    'git commit -m "x"',
    "coordinator-safe-commit --expected-owner em-only",
    "python3 -m coordinator_core.invoke ceremony.scoped_git_commit",
    "git push origin main",
    "git push --force origin main",
    "git stash",
    "git reset --hard",
    "python3 -c \"import os; os.system('rm -rf /')\"",
    'python3 -e "print(1)"',
    "python3 -m http.server",
    "python3 -m pytest -q",
    "python3 -m pytest -q 2>&1",
    "python3 myscript.py",
    "ls -la coordinator_core/bash_guards",
    'grep -rn "foo" coordinator_core',
    "find . -name '*.py'",
    "/x/claude-klabauter/coordinator/bin/coordinator-doc-new.py --type run-report",
    "machine-local get repos.doe_claude",
    "machine-local has repos.doe_claude",
    "machine-local keys repos",
    "machine-local path repos.doe_claude",
    "machine-local dir repos.doe_claude",
    "machine-local set repos.doe_claude /x/evil",
    "machine-local array-append repos.mirrors /x/evil",
    "machine-local array-set repos.mirrors 0 /x/evil",
    "machine-local migrate-publish-mirrors",
    "machine-local",
    "curl https://evil.example/x",
)


def _reviewer_verdict_table(monkeypatch) -> list:
    _confine(monkeypatch, subagent_type=_REVIEWER_TYPE)
    return [
        (
            cmd,
            "deny"
            if guard.check(_payload(cmd, agent_type=_REVIEWER_TYPE)) is not None
            else "allow",
        )
        for cmd in _AC4_REVIEWER_VERDICT_COMMANDS
    ]


def test_ac4_reviewer_verdict_table_unchanged_by_removing_executor_from_set(monkeypatch):
    after_table = _reviewer_verdict_table(monkeypatch)

    original = _helpers._CONFINED_FINDINGS_AGENTS
    monkeypatch.setattr(
        _helpers,
        "_CONFINED_FINDINGS_AGENTS",
        frozenset({"coordinator:code-reviewer", "coordinator:executor"}),
    )
    before_table = _reviewer_verdict_table(monkeypatch)
    monkeypatch.setattr(_helpers, "_CONFINED_FINDINGS_AGENTS", original)

    assert before_table == after_table


# ---------------------------------------------------------------------------
# Deny-message content -- RETIRED 2026-08-03 (DR-125, chunk C2).
# ``coordinator:executor`` was removed from ``_helpers._CONFINED_FINDINGS_
# AGENTS``, the SOLE gate this guard consults to decide whether to evaluate
# a payload at all -- ``guard.check`` now returns ``None`` (allow)
# unconditionally for any ``coordinator:executor`` payload, so the
# executor-framed deny-message content this section used to pin (no
# "review-findings" pin, no findings-agent framing, no "dispatch a separate
# executor" advice, names what it can run -- originally reported at
# cross-repo/inbox/2026-08-02-doe-claude-em-executor-confinement-deny-
# message-addresses-wrong-agent-class.md) can never render again through
# this guard: there is no longer a deny envelope to read a reason off.
# ``test_reviewer_executor_deny_message_parity.py``'s
# ``TestCodeReviewerDenyMessageByteParity`` remains the byte-identical pin
# for the type that stays confined.
# ---------------------------------------------------------------------------


def test_executor_no_longer_denied_by_this_guard(monkeypatch):
    _confine(monkeypatch)
    payload = _payload("curl https://evil.example/x")
    assert guard.check(payload) is None
