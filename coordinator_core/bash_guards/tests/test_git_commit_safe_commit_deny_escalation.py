"""C7's oracle for `check_git_commit_safe_commit_advise`'s index-probe
escalation (`dispatch_checks.py`).

Spec backlink: pln-advisory-firing-shape-predicat-802b35
§ C7, AC1 (deny leg), AC2 (oracle rows), AC10.

`test_git_commit_safe_commit_firing_shape.py` (C1a) already covers the
FIRING-SHAPE axis for this check (does it fire at all) with no index-based
reasoning anywhere -- that module's own negative-spec says so explicitly.
This module's remit is the ORTHOGONAL axis C7 adds on top: for the ONE
shape that already fires (the bare-commit-half, `git add -- <paths> &&
git commit -m "x"`), does it fire as ADVISORY or escalate to DENY, and
under what index state.

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

import shlex
import subprocess

from coordinator_core.bash_guards import dispatch_checks


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


def test_solo_bare_commit_stays_advisory_even_with_foreign_staged_paths(tmp_path):
    """Documented scope decision (see `_bt_c7_index_holds_foreign_paths`'s
    own docstring): a solo bare `git commit`, with NO preceding `git add`
    anywhere in the command, has no "own pathspec" for the set-difference
    formula to compare against, so it is never escalated -- even when the
    index plainly holds someone else's staged work. Still fires
    (advisory), unchanged from C1a."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -m "x"' % shlex.quote(str(repo))
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
