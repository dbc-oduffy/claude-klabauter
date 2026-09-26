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
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"])
    assert _verdict(cmd) == "deny"


def test_deny_when_own_add_names_paths_positionally_without_separator(tmp_path):
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    repo_q = shlex.quote(str(repo))
    cmd = 'git -C %s add own.txt && git -C %s commit -m "x"' % (repo_q, repo_q)
    assert _verdict(cmd) == "deny"


def test_separatorless_add_denies_even_when_it_covers_the_whole_index(tmp_path):
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
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -m "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_solo_bare_commit_stays_advisory_when_index_is_empty(tmp_path):
    repo = _init_repo(tmp_path)
    cmd = 'git -C %s commit -m "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def test_solo_bare_commit_amend_denies_when_index_holds_foreign_paths(tmp_path):
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit --amend -m "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "deny"


def test_solo_bare_commit_amend_only_with_pathspec_never_fires(tmp_path):
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    subprocess.run(
        ["git", "commit", "-m", "base\n\nSession-Id: sess-c7"], cwd=repo, check=True,
        **no_console_passthrough_kwargs()
    )
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    assert _verdict(cmd) == "none"


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
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    _commit_with_trailer(repo, "peer base", "sess-peer")
    _stage(repo, "extra.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-mine")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_amend_scoped_stays_silent_when_head_is_this_session(tmp_path):
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    _commit_with_trailer(repo, "my base", "sess-mine")
    _stage(repo, "extra.txt")
    cmd = 'git -C %s commit --amend --only -m "x" -- mine.txt' % shlex.quote(str(repo))
    assert _verdict_with_sid(cmd, "sess-mine") == "none"


def test_amend_bare_own_head_still_reaches_bare_commit_chain(tmp_path):
    repo = _init_repo(tmp_path)
    _stage(repo, "mine.txt")
    _commit_with_trailer(repo, "my base", "sess-mine")
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit --amend -m "x"' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-mine")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_amend_missing_session_id_fails_closed(tmp_path):
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
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def test_solo_bare_commit_deny_reason_offers_scoped_forms(tmp_path):
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = 'git -C %s commit -m "the subject"' % shlex.quote(str(repo))
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-c7")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "the subject" in reason
    assert "git commit -m" in reason and " -- <paths>" in reason
    assert "scoped-git-commit" not in reason


def test_solo_bare_commit_probe_failure_fails_open_never_denies(monkeypatch, tmp_path):
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
        return (-1, "")

    monkeypatch.setattr(dispatch_checks, "_run_git", _boom)
    assert _verdict(cmd) == "deny"


def test_compound_deny_spends_no_index_probe(tmp_path, monkeypatch):
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
    assert len(spawned) == 3, spawned


def test_explicit_pathspec_on_commit_still_short_circuits_before_any_probe(tmp_path):
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


# finding 6). NARROWED per PM ruling: gated behind `_is_hazard_repo`, so


def _force_hazard(monkeypatch, is_hazard: bool) -> None:
    monkeypatch.setattr(dispatch_checks, "_is_hazard_repo", lambda git_root: is_hazard)


def _touch_worktree(repo, name, content="worktree-edit"):
    path = repo / name
    path.write_text(content)


def test_dash_am_denies_in_a_hazard_repo_when_worktree_holds_modified_paths(
    tmp_path, monkeypatch
):
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
    repo = _init_repo(tmp_path)
    _force_hazard(monkeypatch, True)
    cmd = 'git -C %s commit -am "x"' % shlex.quote(str(repo))
    assert _verdict(cmd) == "advisory"


def test_dash_am_stays_advisory_in_a_non_hazard_repo_even_when_dirty(tmp_path, monkeypatch):
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
    repo = _repo_mid_merge(tmp_path)
    cmd = 'git -C %s commit -m "x"' % (shlex.quote(str(repo)),)
    assert _verdict(cmd) == "advisory"


def test_bare_commit_mid_merge_advisory_names_the_continue_verb(tmp_path):
    repo = _repo_mid_merge(tmp_path)
    cmd = 'git -C %s commit -m "x"' % (shlex.quote(str(repo)),)
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-c7")
    context = json.dumps(out["hookSpecificOutput"])
    assert "git merge --continue" in context
    assert "MERGE_HEAD" in context


def test_the_denys_own_remediation_really_is_unrunnable_mid_merge(tmp_path):
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


@pytest.mark.parametrize(
    "commit_flags",
    ['-m "x"', "-q -F - <<'EOF'\nsubject\n\nbody\nEOF"],
    ids=["-m", "-F"],
)
def test_compound_deny_escalation_matches_across_m_and_f_heredoc_shapes(
    tmp_path, commit_flags
):
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"], commit_flags)
    assert _verdict(cmd) == "deny"


@pytest.mark.parametrize(
    "commit_flags",
    ['-m "x" -- own.txt', "-q -F - -- own.txt <<'EOF'\nsubject\n\nbody\nEOF"],
    ids=["-m", "-F"],
)
def test_compound_scoped_trailing_pathspec_matches_across_m_and_f_heredoc_shapes(
    tmp_path, commit_flags
):
    repo = _init_repo(tmp_path)
    _stage(repo, "foreign.txt")
    cmd = _compound_cmd(repo, ["own.txt"], commit_flags)
    assert _verdict(cmd) == "none"
