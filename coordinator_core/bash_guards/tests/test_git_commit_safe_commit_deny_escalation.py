"""C7's oracle for `check_git_commit_safe_commit_advise`'s index-probe
escalation (`dispatch_checks.py`).

Spec backlink: pln-advisory-firing-shape-predicat-802b35
§ C7, AC1 (deny leg), AC2 (oracle rows), AC10.

`test_git_commit_safe_commit_firing_shape.py` (C1a) already covers the
FIRING-SHAPE axis for this check (does it fire at all) with no index-based
reasoning anywhere -- that module's own negative-spec says so explicitly.
This module's remit is the ORTHOGONAL axis C7 adds on top: for the shapes
that already fire (the bare-commit-half, `git add -- <paths> && git
commit -m "x"`, and -- as of the 2026-08-15 fourth-recurrence promotion --
the solo bare `git commit -m "x"` with no preceding `add` at all), does it
fire as ADVISORY or escalate to DENY, and under what index state.

Each row below uses a real, isolated `tmp_path` git repo (never the
Claude-klabauter checkout itself -- this check's probe reads the ACTUAL
`git diff --cached` state of whatever cwd it resolves, so a test that ran
against the live checkout would be hostage to whatever this session
happens to have staged at test time). `-C <repo>` on both segments is how
each row tells the probe which repo to read, exactly as a caller's own
`git -C <dir> ...` would in production (`_bt_git_dash_c_value`).

Negative-spec: this module does NOT re-assert the firing-SHAPE table
(C1a's job) and does NOT test message-accuracy of the pre-existing
advisory text (`test_deny_message_accuracy.py`'s job) -- only the
advisory-vs-deny verdict under a controlled index.
"""

from __future__ import annotations

import json
import shlex
import subprocess

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import dispatch_checks

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def _stage(repo, name, content="x"):
    path = repo / name
    path.write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True)


def _verdict(cmd: str) -> str:
    """"advisory" | "deny" | "none" -- the three reachable outcomes."""
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-c7")
    if out is None:
        return "none"
    decision = out["hookSpecificOutput"]["permissionDecision"]
    assert decision in ("allow", "deny")
    return "deny" if decision == "deny" else "advisory"


def _compound_cmd(repo, own_paths, commit_flags='-m "x"'):
    repo_q = shlex.quote(str(repo))
    paths_q = " ".join(shlex.quote(p) for p in own_paths)
    return (
        "git -C %s add -- %s && git -C %s commit %s"
        % (repo_q, paths_q, repo_q, commit_flags)
    )


def test_deny_when_index_holds_foreign_staged_paths(tmp_path):
    """AC1/AC10: the compound bare-commit-half escalates to DENY when the
    index carries a path the command's own `git add` never named -- a
    concurrent session's staged work sitting alongside this one's."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"])
    assert _verdict(cmd) == "deny"


def test_deny_when_own_add_names_paths_positionally_without_separator(tmp_path):
    """Regression: `git add <paths>` (no `--` separator) is exactly as scoped
    as `git add -- <paths>`, and must escalate identically.

    Requiring the separator inverted this guard in production: the careful
    spelling denied while the common one -- `git add a.py && git commit -m x`,
    the shape that actually swept a live peer's staged work -- fell through to
    advisory, because `_bt_git_add_own_pathspec` returned `None` and the
    escalation predicate short-circuited on an empty own-pathspec."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    repo_q = shlex.quote(str(repo))
    cmd = 'git -C %s add own.txt && git -C %s commit -m "x"' % (repo_q, repo_q)
    assert _verdict(cmd) == "deny"


def test_advisory_when_separatorless_add_covers_the_whole_index(tmp_path):
    """The separator-less arm must not over-fire either: same spelling, but
    the index holds only what this command's own add names."""
    repo = _init_repo(tmp_path)
    _stage(repo, "own.txt")
    repo_q = shlex.quote(str(repo))
    cmd = 'git -C %s add own.txt && git -C %s commit -m "x"' % (repo_q, repo_q)
    assert _verdict(cmd) == "advisory"


def test_flag_only_add_contributes_no_own_scope(tmp_path):
    """Negative-spec for the arm above: an add with NO path operands
    (`git add -A`, `git add -u`) is genuinely unscoped and still contributes
    no own-pathspec -- the set-difference has nothing to compare against, so
    the shape stays advisory rather than denying on an unbounded add."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    repo_q = shlex.quote(str(repo))
    for add_flags in ("-A", "-u", "--all"):
        cmd = 'git -C %s add %s && git -C %s commit -m "x"' % (
            repo_q, add_flags, repo_q
        )
        assert _verdict(cmd) == "advisory", add_flags


def test_pathspec_from_file_add_fails_open(tmp_path):
    """`--pathspec-from-file` names paths this predicate never reads, so no
    set-difference is computable -- fail open (advisory), never deny on an
    unknown pathspec."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    repo_q = shlex.quote(str(repo))
    cmd = 'git -C %s add --pathspec-from-file list.txt && git -C %s commit -m "x"' % (
        repo_q, repo_q
    )
    assert _verdict(cmd) == "advisory"


def test_advisory_when_index_holds_only_the_commands_own_pathspec(tmp_path):
    """The safe single-session case: the index holds exactly what THIS
    command's own `git add` staged and nothing else -- stays advisory
    (allowed), never denied."""
    repo = _init_repo(tmp_path)
    _stage(repo, "own.txt")
    cmd = _compound_cmd(repo, ["own.txt"])
    assert _verdict(cmd) == "advisory"


def test_advisory_when_index_holds_exactly_the_union_of_two_own_adds(tmp_path):
    """Regression: a compound command may run MORE THAN ONE `git add --
    <paths>` before its commit. "The command's own pathspec" is the UNION
    of every such preceding add, not just the last one -- a two-add compound
    where the index holds exactly both files (and nothing else) must stay
    advisory, never escalate to deny on the earlier add's path."""
    repo = _init_repo(tmp_path)
    _stage(repo, "one.txt")
    _stage(repo, "two.txt")
    repo_q = shlex.quote(str(repo))
    cmd = (
        "git -C %s add -- one.txt && git -C %s add -- two.txt && "
        'git -C %s commit -m "x"' % (repo_q, repo_q, repo_q)
    )
    assert _verdict(cmd) == "advisory"


def test_advisory_when_nothing_staged_at_all(tmp_path):
    """An empty index has no foreign paths to hold by construction."""
    repo = _init_repo(tmp_path)
    cmd = _compound_cmd(repo, ["own.txt"])
    assert _verdict(cmd) == "advisory"


def test_solo_bare_commit_denies_when_index_holds_any_staged_paths(tmp_path):
    """2026-08-15 fourth-recurrence promotion (state/lessons/2026-08-03-
    git-add-mine-then-bare-git-commit-sweeps-70d1438f8f01.yaml): a solo
    bare `git commit`, with NO preceding `git add` anywhere in the
    command, supplies no pathspec of its own for C7's set-difference
    formula to compare against -- but that also means NOTHING in the
    index is verifiable as this command's own staging. Promoted from
    advisory to DENY: any non-empty index is unverifiable, not "probably
    mine" (see `_bt_solo_bare_commit_index_nonempty`'s own docstring)."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -m "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_solo_bare_commit_stays_advisory_when_index_is_empty(tmp_path):
    """Negative-spec for the promotion above: an empty index holds nothing
    unverifiable, so the solo bare shape stays at its pre-existing
    advisory (never silenced -- C1a's unconditional firing is unchanged)."""
    repo = _init_repo(tmp_path)
    cmd = 'git -C %s commit -m "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def test_solo_bare_commit_amend_denies_when_index_holds_foreign_paths(tmp_path):
    """P1 fix (2026-08-15, coordinator:code-reviewer): a prior version of
    this guard excluded bare `--amend` from the new deny on the premise
    that amend always reuses HEAD's tree. That premise is false -- bare
    `git commit --amend -m "x"` (no `-a`, no pathspec) commits the CURRENT
    INDEX amended onto HEAD, exactly like a plain bare commit, so it sweeps
    a peer's staged work the same way. `--amend` must now be denied here
    too, same as the bare form."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit --amend -m "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_solo_bare_commit_amend_only_with_pathspec_never_fires(tmp_path):
    """Negative spec for the fix above: `--amend --only -- <paths>` DOES
    restrict to an explicit pathspec (git rejects a no-paths `--only`
    outright), so it exits earlier via `_bt_commit_has_explicit_pathspec`
    (which suppresses the check entirely, same as any other scoped commit)
    and never reaches the index-based deny -- the one narrow amend shape
    that is genuinely safe stays untouched.

    HEAD carries THIS test's own `sess-c7` `Session-Id:` trailer (as
    `ceremony.scoped_git_commit` would stamp it) so the amend-ownership
    gate (2026-08-15, `check_git_commit_safe_commit_advise`'s amend gate,
    evaluated ahead of the pathspec early return) reads HEAD as provably
    this session's and lets the pathspec check below decide -- see
    `test_amend_scoped_denies_when_head_is_not_this_session` for the
    companion row where HEAD is NOT this session's and the same command
    now denies instead of falling through silently."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    subprocess.run(
        ["git", "commit", "-m", "base\n\nSession-Id: sess-c7"], cwd=repo, check=True
    )
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    assert _verdict(cmd) == "none"


# ---------------------------------------------------------------------------
# Amend-ownership gate (2026-08-15, example-retrieval-repo-em cross-repo memo:
# `cross-repo/inbox/2026-08-15-example-retrieval-repo-em-amend-has-no-safe-helper-and-
# the-scope-advisory-reads-generic.md`). `_bt_head_commit_amend_provenance`
# is the predicate; these rows exercise it through the public check
# function, same style as the rest of this module.
# ---------------------------------------------------------------------------


def _commit_with_trailer(repo, message, sid):
    subprocess.run(
        ["git", "commit", "-m", "%s\n\nSession-Id: %s" % (message, sid)],
        cwd=repo,
        check=True,
    )


def test_amend_scoped_denies_when_head_is_not_this_session(tmp_path):
    """The silence bug Finding 2 reports: a SCOPED amend (`--only --
    <paths>`) used to exit via `_bt_commit_has_explicit_pathspec` before
    ownership was ever considered. HEAD here carries no `Session-Id:`
    trailer at all (a peer's plain `git commit`, SC-DR-008 baseline) --
    the amend must now deny instead of falling through silently."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    subprocess.run(["git", "commit", "-m", "peer base"], cwd=repo, check=True)
    _stage(repo, "extra.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-mine")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_amend_scoped_denies_when_head_is_a_different_session(tmp_path):
    """Same shape, but HEAD carries a TRAILER -- just not this session's.
    A mismatched sid must deny exactly like an absent one, never read as
    "close enough"."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    _commit_with_trailer(repo, "peer base", "sess-peer")
    _stage(repo, "extra.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-mine")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_amend_scoped_stays_silent_when_head_is_this_session(tmp_path):
    """Positive companion: HEAD's trailer matches this session's own
    `session_id` -- the amend gate reads HEAD as provably mine and lets
    the pathspec check silence the rest of the function, same as
    pre-2026-08-15 behavior for the legitimate flow."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    _commit_with_trailer(repo, "my base", "sess-mine")
    _stage(repo, "extra.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    assert _verdict_with_sid(cmd, "sess-mine") == "none"


def test_amend_bare_own_head_still_reaches_bare_commit_chain(tmp_path):
    """A BARE amend (no `--only`, no pathspec) that clears the ownership
    gate must still fall through to the existing index-based bare-commit
    deny/advisory chain unchanged -- ownership answers "whose HEAD", not
    "whose index". A foreign path sitting in the index alongside this
    session's own HEAD must still deny, via the pre-existing mechanism."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    _commit_with_trailer(repo, "my base", "sess-mine")
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit --amend -m "x"' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-mine")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_amend_missing_session_id_fails_closed(tmp_path):
    """An empty `session_id` can never read as "owns HEAD" -- even a HEAD
    carrying no trailer of its own must deny, not vacuously match."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True)
    _stage(repo, "extra.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_amend_probe_failure_fails_closed_never_silent(monkeypatch, tmp_path):
    """The deliberate INVERSION of this module's other probe-failure rows
    (`test_probe_failure_fails_open_never_denies` et al.): every other
    probe in this check fails OPEN. The amend-ownership probe fails
    CLOSED -- a forced `_run_git` failure must still deny, since an
    unprovable HEAD under `--amend` is an irreversible rewrite with no
    remedy under live peers, not a "probably fine" index read."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    _commit_with_trailer(repo, "my base", "sess-mine")
    _stage(repo, "extra.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))

    def _boom(*args, **kwargs):
        return (-1, "")

    monkeypatch.setattr(dispatch_checks, "_run_git", _boom)
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-mine")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_amend_override_key_allows_foreign_head(tmp_path, monkeypatch):
    """`COORDINATOR_ALLOW_GIT_COMMIT_AMEND` unlocks the amend gate
    independently of `COORDINATOR_ALLOW_GIT_COMMIT_BARE` -- the two keys
    must be tunable separately, per the memo's proposal (3)."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    subprocess.run(["git", "commit", "-m", "peer base"], cwd=repo, check=True)
    _stage(repo, "extra.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    monkeypatch.setenv("COORDINATOR_ALLOW_GIT_COMMIT_AMEND", "1")
    assert _verdict_with_sid(cmd, "sess-mine") == "none"


def test_amend_deny_reason_names_the_commit_and_notes_remedy(tmp_path):
    """The message names the specific commit it would rewrite and offers
    the shared-tree-safe `git notes` repair -- never `scoped-git-commit`,
    which cannot amend."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    subprocess.run(["git", "commit", "-m", "peer subject line"], cwd=repo, check=True)
    _stage(repo, "extra.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-mine")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "peer subject line" in reason
    assert "git notes add" in reason
    assert "scoped-git-commit" not in reason


def test_amend_does_not_regress_non_amend_commits(tmp_path):
    """Negative spec: a non-amend commit's routing stays byte-identical --
    the ownership gate never runs for it at all."""
    repo = _init_repo(tmp_path)
    cmd = 'git -C %s commit -m "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def _verdict_with_sid(cmd: str, sid: str) -> str:
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, sid)
    if out is None:
        return "none"
    decision = out["hookSpecificOutput"]["permissionDecision"]
    assert decision in ("allow", "deny")
    return "deny" if decision == "deny" else "advisory"


def test_solo_bare_commit_dash_a_excluded_from_new_deny(tmp_path):
    """`-a`/`--all` stay excluded from the new solo-bare-commit deny too,
    same unconditional exclusion PM Ruling 2 already applies to C7."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def test_solo_bare_commit_deny_reason_offers_scoped_forms(tmp_path):
    """The new deny's message leads with the runnable trailing-pathspec
    form and the scoped-git-commit fallback, matching the register the
    compound-shape deny already uses (guard-messaging.md § Register)."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -m "the subject"' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-c7")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "the subject" in reason
    assert "git commit -m" in reason and " -- <paths>" in reason
    assert "scoped-git-commit" in reason


def test_solo_bare_commit_probe_failure_fails_open_never_denies(monkeypatch, tmp_path):
    """Same fail-open posture as C7's own probe (test_probe_failure_fails_
    open_never_denies above): a forced `_run_git` failure degrades this
    new deny to advisory too, never to deny."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -m "x"' % shlex.quote(str(repo))

    def _boom(*args, **kwargs):
        return (-1, "")

    monkeypatch.setattr(dispatch_checks, "_run_git", _boom)
    assert _verdict(cmd) == "advisory"


def test_dash_a_excludes_from_escalation_even_with_foreign_staged_paths(tmp_path):
    """PM Ruling 2, finding 6: `-a` sweeps from the WORKTREE at commit
    time, invisible to a clean-index probe -- unconditionally excluded
    from index-based reasoning, deny-side included."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"], commit_flags='-am "x"')
    assert _verdict(cmd) == "advisory"


def test_dash_dash_all_excludes_from_escalation(tmp_path):
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"], commit_flags='--all -m "x"')
    assert _verdict(cmd) == "advisory"


def test_probe_failure_fails_open_never_denies(monkeypatch, tmp_path):
    """AC1: probe failure posture is the OPPOSITE of a fail-closed hard
    deny -- a forced `_run_git` failure must degrade to advisory, never
    deny, since a false deny would block real work on a guard-process
    error while a false silence just returns to today's baseline."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"])

    def _boom(*args, **kwargs):
        return (-1, "")  # rc == -1: `_run_git`'s own timeout convention

    monkeypatch.setattr(dispatch_checks, "_run_git", _boom)
    assert _verdict(cmd) == "advisory"


def test_probe_success_still_denies_when_not_forced(tmp_path):
    """Companion oracle row to the forced-failure case above, asserted in
    the SAME module so both postures of AC1's probe-failure requirement
    are visible side by side: an unforced, healthy probe against the same
    foreign-path setup still denies."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"])
    assert _verdict(cmd) == "deny"


def test_explicit_pathspec_on_commit_still_short_circuits_before_any_probe(tmp_path):
    """A commit segment carrying its own explicit `-- <paths>` returns
    `None` before C7's escalation logic is ever reached (C1a's unchanged
    unconditional-silence behavior) -- asserted here so a future change to
    the escalation call site cannot silently start probing a case this
    check has never fired on."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    repo_q = shlex.quote(str(repo))
    cmd = 'git -C %s commit -m "x" -- own.txt' % repo_q
    assert _verdict(cmd) == "none"


def test_add_positional_and_separator_paths_both_count_as_own(tmp_path):
    """Review finding 4 (P2): `git add <pos> -- <pos>` unions BOTH halves as
    the add's own pathspec -- `git add` has no revision argument, so `--`
    there means only "stop parsing options"; both `one.txt` and `two.txt`
    are genuine pathspecs. Pre-fix, only the post-separator token counted,
    so the index-only pre-separator path (`one.txt`) fell into the "foreign"
    set and this compound would have wrongly escalated to deny even though
    both staged paths belong to this command's own add."""
    repo = _init_repo(tmp_path)
    _stage(repo, "one.txt")
    _stage(repo, "two.txt")
    repo_q = shlex.quote(str(repo))
    cmd = (
        'git -C %s add one.txt -- two.txt && git -C %s commit -m "x"'
        % (repo_q, repo_q)
    )
    assert _verdict(cmd) == "advisory"


def test_deny_reason_names_the_shape_and_offers_a_runnable_scoped_form(tmp_path):
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"], commit_flags='-m "the subject"')
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-c7")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "the subject" in reason
    assert "git add -- <paths> && git commit -m" in reason
    assert " -- <paths>" in reason


def test_solo_bare_commit_deny_fires_through_the_real_dispatcher_under_powershell(
    tmp_path,
):
    """2026-08-15 dispatch: 'a guard keyed only on Bash is inert under
    PowerShell'. This runs the FULL dispatcher (`dispatch.evaluate_
    payload_json`), not the check function directly, against a
    `tool_name: "PowerShell"` payload -- pinning the `matchers=("Bash",
    "PowerShell")` widening in `dispatch.py`'s GuardEntry registration,
    not just the check function's own tool-agnostic logic."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -m "x"' % shlex.quote(str(repo))
    payload = json.dumps(
        {
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
            "session_id": "sess-c7-ps",
            "cwd": str(repo),
        }
    )
    out = dispatch.evaluate_payload_json(payload)
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_solo_bare_commit_advisory_still_fires_through_the_real_dispatcher_under_bash(
    tmp_path,
):
    """Companion row, same setup, `tool_name: "Bash"` -- both seams must
    reach the SAME check with the SAME verdict, not just PowerShell newly
    reachable while Bash silently regresses."""
    repo = _init_repo(tmp_path)
    cmd = 'git -C %s commit -m "x"' % shlex.quote(str(repo))
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": "sess-c7-bash",
            "cwd": str(repo),
        }
    )
    out = dispatch.evaluate_payload_json(payload)
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
