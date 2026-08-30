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

2026-08-30 PM ruling: the compound bare-commit-half no longer has an index
axis at all -- it denies UNCONDITIONALLY (`_bt_compound_add_bare_commit`),
and the two-`git diff --cached` probe that used to gate it is deleted. Its
rows below therefore assert DENY across every index state, which is the
POINT of those rows, not redundancy: they pin that no index state, and no
probe outcome, can talk the deny back down to advisory. The index axis is
still live for the SOLO bare commit and the `-a`/`--all` sweep.

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
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

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
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    return repo


def _stage(repo, name, content="x"):
    path = repo / name
    path.write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True, **no_console_passthrough_kwargs())


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
    advisory, because the pathspec extractor returned `None` and the
    escalation predicate short-circuited on an empty own-pathspec. Both are
    deleted as of 2026-08-30 and this shape now denies on the compound
    SHAPE alone -- the row stays as the regression pin it always was."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    repo_q = shlex.quote(str(repo))
    cmd = 'git -C %s add own.txt && git -C %s commit -m "x"' % (repo_q, repo_q)
    assert _verdict(cmd) == "deny"


def test_separatorless_add_denies_even_when_it_covers_the_whole_index(tmp_path):
    """2026-08-30 ruling: the index holding ONLY what this command's own add
    names no longer earns an advisory. The index is shared and a peer can
    stage into it between this guard's read and the commit's write, so a
    clean index at guard time is a race outcome, not a property of the
    command."""
    repo = _init_repo(tmp_path)
    _stage(repo, "own.txt")
    repo_q = shlex.quote(str(repo))
    cmd = 'git -C %s add own.txt && git -C %s commit -m "x"' % (repo_q, repo_q)
    assert _verdict(cmd) == "deny"


def test_flag_only_add_denies_too(tmp_path):
    """INVERTED 2026-08-30. An add with no path operands (`git add -A`,
    `git add -u`) used to stay advisory because it contributed no
    own-pathspec for the set-difference to compare against. With the
    set-difference gone that reason is gone with it, and keeping the
    carve-out would leave the UNBOUNDED shape advisory while the scoped
    `git add one.py && git commit` denies -- a guard that punishes the
    careful spelling. The unscoped shape is strictly the more dangerous
    one, so it denies too."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    repo_q = shlex.quote(str(repo))
    for add_flags in ("-A", "-u", "--all"):
        cmd = 'git -C %s add %s && git -C %s commit -m "x"' % (
            repo_q, add_flags, repo_q
        )
        assert _verdict(cmd) == "deny", add_flags


def test_pathspec_from_file_add_denies_too(tmp_path):
    """INVERTED 2026-08-30, same reasoning as the flag-only arm above:
    `--pathspec-from-file` named paths no predicate here reads, which made
    the set-difference incomputable and the shape fail open. The deny no
    longer rests on a set-difference, and an add whose scope the guard
    cannot see is not a reason to wave the unscoped commit through."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    repo_q = shlex.quote(str(repo))
    cmd = 'git -C %s add --pathspec-from-file list.txt && git -C %s commit -m "x"' % (
        repo_q, repo_q
    )
    assert _verdict(cmd) == "deny"


def test_denies_even_when_index_holds_only_the_commands_own_pathspec(tmp_path):
    """INVERTED 2026-08-30 -- this row IS the reversal of C7 PM Ruling 2.
    The "safe single-session case" (index holds exactly what THIS command's
    own `git add` staged, nothing else) was the carve-out that kept the deny
    off; the PM was told the new ruling reverses it and proceeded."""
    repo = _init_repo(tmp_path)
    _stage(repo, "own.txt")
    cmd = _compound_cmd(repo, ["own.txt"])
    assert _verdict(cmd) == "deny"


def test_denies_when_index_holds_exactly_the_union_of_two_own_adds(tmp_path):
    """INVERTED 2026-08-30. A compound command may run MORE THAN ONE
    `git add` before its commit; the union of those adds used to buy an
    advisory when it covered the whole index. It no longer does -- but the
    row stays, because the multi-add shape is the one whose segment walk
    `_bt_compound_add_bare_commit` still has to get right (it scans every
    prior segment, not just the immediately preceding one)."""
    repo = _init_repo(tmp_path)
    _stage(repo, "one.txt")
    _stage(repo, "two.txt")
    repo_q = shlex.quote(str(repo))
    cmd = (
        "git -C %s add -- one.txt && git -C %s add -- two.txt && "
        'git -C %s commit -m "x"' % (repo_q, repo_q, repo_q)
    )
    assert _verdict(cmd) == "deny"


def test_denies_even_when_nothing_is_staged_at_all(tmp_path):
    """INVERTED 2026-08-30. An empty index held no foreign paths by
    construction, so this was the clearest advisory row of the set. It is
    now the clearest statement of what changed: the deny reads the COMMAND,
    not the index, and an index that is empty when the guard looks says
    nothing about what it holds when the commit runs."""
    repo = _init_repo(tmp_path)
    cmd = _compound_cmd(repo, ["own.txt"])
    assert _verdict(cmd) == "deny"


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
        ["git", "commit", "-m", "base\n\nSession-Id: sess-c7"], cwd=repo, check=True,
        **no_console_passthrough_kwargs()
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
        **no_console_passthrough_kwargs()
    )


def test_amend_scoped_denies_when_head_is_not_this_session(tmp_path):
    """The silence bug Finding 2 reports: a SCOPED amend (`--only --
    <paths>`) used to exit via `_bt_commit_has_explicit_pathspec` before
    ownership was ever considered. HEAD here carries no `Session-Id:`
    trailer at all (a peer's plain `git commit`, SC-DR-008 baseline) --
    the amend must now deny instead of falling through silently."""
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    subprocess.run(["git", "commit", "-m", "peer base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
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
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
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
    subprocess.run(["git", "commit", "-m", "peer base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
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
    subprocess.run(["git", "commit", "-m", "peer subject line"], cwd=repo, check=True, **no_console_passthrough_kwargs())
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
    form -- the whole remedy since DR-344 retired scoped-git-commit --
    matching the register the
    compound-shape deny already uses (guard-messaging.md § Register)."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -m "the subject"' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-c7")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "the subject" in reason
    assert "git commit -m" in reason and " -- <paths>" in reason
    # No `scoped-git-commit` fallback any more -- deleted under DR-344
    # (2026-08-23) along with `ceremony.scoped_git_commit`. See the sibling
    # note in test_check_blanket_git_add.py; the runnable trailing-pathspec
    # form above is now the whole remedy.
    assert "scoped-git-commit" not in reason


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


def test_compound_deny_survives_a_dead_git_because_it_never_probes(
    monkeypatch, tmp_path
):
    """INVERTED 2026-08-30, and the sharpest pin on the new predicate. This
    row used to assert the fail-OPEN posture: a forced `_run_git` failure
    degraded the compound deny to advisory, because the deny rested on a
    probe. It rests on the command tokens now, so a totally dead `git` must
    change NOTHING -- if this row ever goes advisory again, a probe has
    crept back onto the commit hot path."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"])

    def _boom(*args, **kwargs):
        return (-1, "")  # rc == -1: `_run_git`'s own timeout convention

    monkeypatch.setattr(dispatch_checks, "_run_git", _boom)
    assert _verdict(cmd) == "deny"


def test_compound_deny_spends_no_index_probe(tmp_path, monkeypatch):
    """The brightline leg of the 2026-08-30 change: the compound deny no
    longer reads the index, so it spends NO `git diff --cached` spawn where
    the deleted probe spent two per commit attempt -- on the commit hot
    path, under a 500ms budget with 50-70 concurrent sessions.

    Asserted as "no index probe", not "no subprocess at all", because that
    would be false and the difference matters: `_bt_git_sequencer_in_
    progress` still spends THREE `git rev-parse --git-path` spawns ahead of
    this deny, on every bare-commit evaluation. Those are pre-existing and
    out of this change's scope, but they are the remaining per-commit spawn
    cost on this path and the row names them so the next reader measures
    against the real number rather than this test's title."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"])
    spawned = []
    real_run = dispatch_checks.subprocess.run

    def _counting_run(args, *a, **kw):
        spawned.append(list(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(dispatch_checks.subprocess, "run", _counting_run)
    assert _verdict(cmd) == "deny"
    assert not [a for a in spawned if "diff" in a], spawned
    # Pin the pre-existing cost too, so a REGRESSION that adds a fourth
    # spawn to this path is caught by the row that measures the path.
    assert len(spawned) == 3, spawned


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


def test_add_positional_and_separator_paths_deny_like_any_other_add(tmp_path):
    """RETIRED DISTINCTION, kept as a regression row. `git add <pos> -- <pos>`
    used to need its two halves unioned into "the command's own pathspec",
    because miscounting them dropped `one.txt` into the foreign set and
    escalated a safe compound to deny. With the set-difference deleted there
    is no own-pathspec to compute and no way to get this spelling wrong --
    it denies because it is a compound bare commit, like every other add
    spelling. The row survives to pin that the exotic spelling still
    TOKENIZES as a `git add` segment, which is the one thing
    `_bt_compound_add_bare_commit` still has to see."""
    repo = _init_repo(tmp_path)
    _stage(repo, "one.txt")
    _stage(repo, "two.txt")
    repo_q = shlex.quote(str(repo))
    cmd = (
        'git -C %s add one.txt -- two.txt && git -C %s commit -m "x"'
        % (repo_q, repo_q)
    )
    assert _verdict(cmd) == "deny"


def test_deny_reason_names_the_shape_and_offers_a_runnable_scoped_form(tmp_path):
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"], commit_flags='-m "the subject"')
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-c7")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "the subject" in reason
    assert "git add -- <paths> && git commit -m" in reason
    assert " -- <paths>" in reason


# ---------------------------------------------------------------------------
# C1 (docs/plans/2026-08-15-blanket-gits-proffer-the-scoped-commit-helper.md)
# -- the worktree-union escalation predicate that closes the `-a` hole the
# two index-based predicates above deliberately exclude (PM Ruling 2,
# finding 6). NARROWED per PM ruling: gated behind `_is_hazard_repo`, so
# every row below monkeypatches that discriminator explicitly rather than
# relying on a real fleet-registry match against a `tmp_path` repo, which
# can never itself be a registered hazard repo.
# ---------------------------------------------------------------------------


def _force_hazard(monkeypatch, is_hazard: bool) -> None:
    monkeypatch.setattr(dispatch_checks, "_is_hazard_repo", lambda git_root: is_hazard)


def _touch_worktree(repo, name, content="worktree-edit"):
    """Modify a TRACKED file in the worktree without staging it -- `-a`'s
    own sweep source, invisible to any `git diff --cached` probe."""
    path = repo / name
    path.write_text(content)


def test_dash_am_denies_in_a_hazard_repo_when_worktree_holds_modified_paths(
    tmp_path, monkeypatch
):
    """AC1/AC2: `-am` escalates to DENY in a hazard repo when the
    UNION of staged + worktree-modified paths is non-empty -- the shape
    the two index-based predicates unconditionally exclude."""
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    _force_hazard(monkeypatch, True)
    cmd = 'git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_dash_a_dash_m_separate_tokens_denies_in_a_hazard_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    _force_hazard(monkeypatch, True)
    cmd = 'git -C %s commit -a -m "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_bundled_dash_sam_denies_in_a_hazard_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    _force_hazard(monkeypatch, True)
    cmd = 'git -C %s commit -sam "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_dash_dash_all_denies_in_a_hazard_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    _force_hazard(monkeypatch, True)
    cmd = 'git -C %s commit --all -m "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_dash_am_with_trailing_pathspec_still_participates_in_escalation(
    tmp_path, monkeypatch
):
    """AC5: `-am ... -- <paths>` is not a valid git invocation (git itself
    rejects paths with `-a`), so this row pins the early-exit ordering
    rather than sanctioning the shape -- `_bt_commit_has_explicit_
    pathspec` treats `-a` as unconditionally unscoped (same sweep-all
    check the new predicate itself inverts), so a trailing `-- <paths>`
    here does NOT short-circuit the check to silence; the shape reaches
    the new predicate exactly like plain `-am` and escalates identically
    in a hazard repo, staying advisory in a non-hazard one."""
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    cmd = 'git -C %s commit -am "x" -- base.txt' % shlex.quote(str(repo))
    _force_hazard(monkeypatch, True)
    assert _verdict(cmd) == "deny"
    _force_hazard(monkeypatch, False)
    assert _verdict(cmd) == "advisory"


def test_dash_c_prefixed_dash_am_denies_in_a_hazard_repo(tmp_path, monkeypatch):
    """AC4: honours `-C <dir>` -- reuses `_bt_git_dash_c_value`."""
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    _force_hazard(monkeypatch, True)
    cmd = 'git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_git_index_file_prefixed_dash_am_denies_in_a_hazard_repo(tmp_path, monkeypatch):
    """AC4: honours a leading `GIT_INDEX_FILE=` assignment -- reuses
    `_bt_git_index_file_env` rather than re-deriving it."""
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    index_file = repo / ".git" / "index"
    _force_hazard(monkeypatch, True)
    cmd = 'GIT_INDEX_FILE=%s git -C %s commit -am "x"' % (
        shlex.quote(str(index_file)), shlex.quote(str(repo))
    )
    assert _verdict(cmd) == "deny"


def test_piped_dash_am_segment_denies_in_a_hazard_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    _force_hazard(monkeypatch, True)
    cmd = 'echo y | git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_compound_add_then_dash_am_still_denies_in_a_hazard_repo(tmp_path, monkeypatch):
    """AC9 compound-shape row: a preceding scoped `git add -- mine.py` does
    NOT make the `-a` swept set provably own -- `-a` reaches worktree paths
    no `add` pathspec bounds. Must still escalate, unlike C7's own
    compound-shape predicate."""
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    other = repo / "mine.py"
    other.write_text("mine")
    _force_hazard(monkeypatch, True)
    repo_q = shlex.quote(str(repo))
    cmd = 'git -C %s add -- mine.py && git -C %s commit -am "x"' % (repo_q, repo_q)
    assert _verdict(cmd) == "deny"


def test_dash_am_stays_advisory_in_a_hazard_repo_when_tree_is_clean(tmp_path, monkeypatch):
    """Negative spec: hazard repo, but the union is empty -- stays
    advisory, never denies on nothing to sweep."""
    repo = _init_repo(tmp_path)
    _force_hazard(monkeypatch, True)
    cmd = 'git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def test_dash_am_stays_advisory_in_a_non_hazard_repo_even_when_dirty(tmp_path, monkeypatch):
    """The narrowing itself: a NON-hazard repo never escalates, however
    dirty the tree -- `check_blanket_git_add` un-widened the identical
    all-repo hazard 2026-07-31, and this predicate must not reintroduce
    it via a different guard."""
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    _force_hazard(monkeypatch, False)
    cmd = 'git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def test_dash_am_budget_spent_dispatch_stays_advisory_never_deny(tmp_path, monkeypatch):
    """AC3/AC9: a budget-spent dispatch (`_run_git` returning
    `_GIT_PROBE_BUDGET_SPENT_RC`) on `git commit -am` asserts ADVISORY,
    never DENY."""
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    _force_hazard(monkeypatch, True)

    def _spent(*args, **kwargs):
        return (dispatch_checks._GIT_PROBE_BUDGET_SPENT_RC, "")

    monkeypatch.setattr(dispatch_checks, "_run_git", _spent)
    cmd = 'git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def test_dash_am_probe_failure_fails_open_never_denies(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _stage(repo, "base.txt")
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _touch_worktree(repo, "base.txt")
    _force_hazard(monkeypatch, True)

    def _boom(*args, **kwargs):
        return (-1, "")

    monkeypatch.setattr(dispatch_checks, "_run_git", _boom)
    cmd = 'git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def test_bare_commit_predicates_still_return_false_on_dash_a(tmp_path):
    """Negative spec, called directly: the bare-commit-half predicates keep
    short-circuiting on `-a` -- the worktree-union predicate below is the
    only one that inverts that gate. `_bt_compound_add_bare_commit` inherits
    the exclusion from the probe it replaced, unchanged."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    tokens = dispatch_checks._bt_tokenize_full_command(
        'git -C %s commit -am "x"' % shlex.quote(str(repo))
    )
    segments = dispatch_checks._bt_segments_from_tokens_with_pipe_flag(tokens)
    seg_tokens, _pipe = segments[0]
    assert dispatch_checks._bt_compound_add_bare_commit(seg_tokens, segments, 0) is False
    assert dispatch_checks._bt_solo_bare_commit_index_nonempty(seg_tokens, segments, 0) is False


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


# ---------------------------------------------------------------------------
# Sequencer states (merge/cherry-pick/revert) -- the one shape where every
# deny branch's remediation is rejected by git itself, so a deny is a dead end.
# state/bug-backlog/2026-08-26-the-bare-commit-guard-has-no-merge-head-carve-out.yaml
# ---------------------------------------------------------------------------


def _commit_on(repo, branch, name, content):
    subprocess.run(["git", "checkout", "-q", "-B", branch], cwd=repo, check=True, **no_console_passthrough_kwargs())
    (repo / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", f"{branch}:{name}"], cwd=repo, check=True, **no_console_passthrough_kwargs())


def _repo_mid_merge(tmp_path):
    """A repo stopped inside a conflicted merge: MERGE_HEAD present, index
    non-empty, and no `git add` anywhere in the command under test."""
    repo = _init_repo(tmp_path)
    (repo / "base.txt").write_text("base")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    _commit_on(repo, "main", "conflict.txt", "ours")
    subprocess.run(["git", "checkout", "-q", "-b", "side", "HEAD~1"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    (repo / "conflict.txt").write_text("theirs")
    subprocess.run(["git", "add", "conflict.txt"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "side"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    merge = subprocess.run(["git", "merge", "main"], cwd=repo, capture_output=True, text=True, **no_console_creationflags())
    assert merge.returncode != 0, "fixture must stop inside a conflicted merge"
    subprocess.run(["git", "checkout", "--theirs", "--", "conflict.txt"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "add", "conflict.txt"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    assert (repo / ".git" / "MERGE_HEAD").exists()
    return repo


def test_bare_commit_mid_merge_is_advisory_not_deny(tmp_path):
    """The deny's own remediation (`git commit -m x -- <paths>`) exits 128
    during a merge, so denying leaves no runnable route. Downgraded to an
    advisory that names the verb which actually finishes the operation."""
    repo = _repo_mid_merge(tmp_path)
    cmd = 'git -C %s commit -m "x"' % (shlex.quote(str(repo)),)
    assert _verdict(cmd) == "advisory"


def test_bare_commit_mid_merge_advisory_names_the_continue_verb(tmp_path):
    """The whole point of the downgrade: the operator learns the route."""
    repo = _repo_mid_merge(tmp_path)
    cmd = 'git -C %s commit -m "x"' % (shlex.quote(str(repo)),)
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-c7")
    context = json.dumps(out["hookSpecificOutput"])
    assert "git merge --continue" in context
    assert "MERGE_HEAD" in context


def test_the_denys_own_remediation_really_is_unrunnable_mid_merge(tmp_path):
    """Pins the premise this carve-out rests on, rather than asserting it in
    prose: if git ever starts accepting a scoped commit mid-merge, the deny
    stops being a dead end and this whole branch should be reconsidered."""
    repo = _repo_mid_merge(tmp_path)
    scoped = subprocess.run(
        ["git", "commit", "-m", "x", "--", "conflict.txt"],
        cwd=repo, capture_output=True, text=True,
        **no_console_creationflags()
    )
    assert scoped.returncode != 0
    assert "partial commit" in (scoped.stderr + scoped.stdout).lower()


def test_bare_commit_outside_any_sequencer_still_denies(tmp_path):
    """The carve-out must not retire the guard for ordinary bare commits --
    same command shape, same non-empty index, no MERGE_HEAD."""
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    cmd = 'git -C %s commit -m "x"' % (shlex.quote(str(repo)),)
    assert _verdict(cmd) == "deny"
