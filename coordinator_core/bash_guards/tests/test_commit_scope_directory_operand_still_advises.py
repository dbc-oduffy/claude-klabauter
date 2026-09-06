"""A scope FORM is not a scope BOUND: `-o state/` must not buy silence.

Purpose: on 2026-08-03 `git commit -o <two files> state/subagent-share/
state/lessons/` swept 71 files across 10 concurrent sessions on a shared tree
and produced NO guard output at all. Two independent silences stacked, which is
why the incident left an empty `scope-warnings.log`:

  A. `_bt_commit_has_explicit_pathspec` returned True on any explicit-scope
     FORM without ever inspecting what the operands were, so a whole-subtree
     operand suppressed `check_git_commit_safe_commit_advise` outright.
  B. `_extract_commit_trailing_pathspecs` bailed unless the segment contained a
     literal `--`, so Check 13 never evaluated an `-o` commit at all.

Fixing A alone leaves the incident command silent through B's path, so both are
pinned here in one file, and the incident command itself is asserted literally
rather than in a reduced form.

NEGATIVE SPEC -- what this file must never be relaxed into asserting:
  - Recognition of `-o`/`--only` as scope is NOT reverted. `-o <file>` genuinely
    selects git's index-bypassing self-scoped mode; the spinoff's anti-scope
    says so in writing. Only SUPPRESSION narrows.
  - The advisory is NOT escalated to a deny. That is direction-class and sits
    with DoE's own strict-mode question.
  - A scoped commit naming a single FILE stays silent. If that regresses, every
    correct scoped commit starts nagging and the advisory gets tuned out --
    which costs more than the sweep it was meant to catch.

Spinoff: `state/handoffs/2026-08-03-commit-scope-guard-predicates.md`.
Non-spawning by construction: no git process is created, so this costs the
shared box nothing and stays off the spawning-tests ratchet.
"""

from __future__ import annotations

import os
import shlex

from coordinator_core.bash_guards import commit_tripwires as ct
from coordinator_core.bash_guards import dispatch_checks as dc

#: The command from the 2026-08-03 incident, in its original shape: two file
#: operands followed by two directory operands, under `-o`, with a quoted
#: message. Reduced forms elsewhere in this file are conveniences; this one is
#: the thing that actually happened.
INCIDENT_CMD = (
    'git commit -o coordinator_core/ipc.py coordinator_core/op_scopes.py '
    'state/subagent-share/ state/lessons/ -m "x"'
)


def _payload():
    return {"cwd": os.getcwd()}


def _advises(cmd: str) -> bool:
    return dc.check_git_commit_safe_commit_advise(cmd, "", _payload()) is not None


# ---------------------------------------------------------------------------
# A -- a directory operand must not suppress the advisory
# ---------------------------------------------------------------------------


def test_a_directory_operand_under_dash_o_still_advises():
    assert _advises("git commit -o state/ -m x")


def test_a_directory_operand_after_the_separator_still_advises():
    """The defect is NOT `-o`-specific. `-- <paths>` is the doctrinally
    RECOMMENDED spelling (SC-DR-008), so a fix scoped to `-o` would leave the
    form we actively tell people to use exposed."""
    assert _advises("git commit -m x -- state/")


def test_several_directory_operands_still_advise():
    assert _advises("git commit -m x -- state/lessons/ state/audits/")


def test_a_directory_named_without_a_trailing_slash_still_advises():
    """`state/lessons` and `state/lessons/` are the same sweep. The trailing
    separator is a spelling, so the check must also resolve against the
    filesystem rather than trusting punctuation."""
    assert _advises("git commit -o coordinator_core state/lessons -m x")


def test_a_glob_operand_still_advises():
    """A glob's membership resolves at commit time, not when the operator read
    the command back to themselves."""
    assert _advises('git commit -m x -- "*.py"')


# ---------------------------------------------------------------------------
# A -- the false-positive floor. These are correct commits and must stay quiet.
# ---------------------------------------------------------------------------


def test_a_single_file_under_dash_o_stays_silent():
    assert not _advises(
        "git commit -o coordinator_core/bash_guards/dispatch_checks.py -m x"
    )


def test_a_single_file_after_the_separator_stays_silent():
    assert not _advises(
        "git commit -m x -- coordinator_core/bash_guards/dispatch_checks.py"
    )


def test_a_deleted_paths_scoped_commit_stays_silent():
    """A path being REMOVED is absent from disk by construction, and
    `git commit -m x -- gone.py` is the ratified way to land that deletion.
    Firing here would nag on a correct commit; the deliberate narrowing is
    recorded in `_bt_commit_scope_operand_is_sweeping`'s own docstring, which
    also names what evidence would reverse it."""
    assert not _advises("git commit -m x -- this-path-does-not-exist-anywhere.py")


def test_an_unscoped_commit_still_advises():
    """SC-DR-017. The narrowing must not accidentally teach the bare form to
    read as scoped."""
    assert _advises("git commit -m x")


def test_include_still_suppresses_nothing():
    """`--include` scopes nothing -- it ADDS to the index -- and the negative
    spec in `_bt_commit_has_explicit_pathspec`'s docstring is preserved in
    behaviour, not just in prose."""
    assert _advises("git commit -i -m x a.txt")


# ---------------------------------------------------------------------------
# B -- Check 13 must evaluate `-o` commits
# ---------------------------------------------------------------------------


def test_b_extractor_sees_dash_o_operands():
    assert ct._extract_commit_trailing_pathspecs(
        "git commit -o coordinator/docs/a.md state/lessons/ -m x"
    ) == ["coordinator/docs/a.md", "state/lessons/"]


def test_b_extractor_sees_long_only_operands():
    assert ct._extract_commit_trailing_pathspecs(
        "git commit --only state/ -m x"
    ) == ["state/"]


def test_b_separator_form_is_unchanged():
    assert ct._extract_commit_trailing_pathspecs(
        "git commit -m x -- state/lessons/"
    ) == ["state/lessons/"]


def test_b_bare_commit_is_still_not_applicable():
    assert ct._extract_commit_trailing_pathspecs("git commit -m x") is None


def test_b_message_that_looks_like_a_flag_is_not_read_as_scope():
    """`-m -o` puts the literal token `-o` in the MESSAGE position. A walk
    matching `-o` across bare tokens would manufacture scope out of a message
    body; the delegated scan's option-value pair-skipping is what prevents it,
    and this pins that the delegation actually happens."""
    assert ct._extract_commit_trailing_pathspecs("git commit -m -o") is None


def test_b_fails_closed_to_not_applicable_on_an_unparseable_segment():
    """An unterminated quote must yield None, never a fabricated path list --
    this check can only narrow itself out of firing, never widen into a false
    advisory on a segment it could not parse."""
    assert ct._extract_commit_trailing_pathspecs('git commit -o "unterminated') is None


# ---------------------------------------------------------------------------
# The incident, asserted literally, through both guards
# ---------------------------------------------------------------------------


def test_the_incident_command_now_fires_the_advisory():
    assert _advises(INCIDENT_CMD)


def test_the_incident_command_is_now_visible_to_check_13():
    """Landing A without B would leave this returning None -- the advisory
    restored but the incident's other silence intact."""
    assert ct._extract_commit_trailing_pathspecs(INCIDENT_CMD) == [
        "coordinator_core/ipc.py",
        "coordinator_core/op_scopes.py",
        "state/subagent-share/",
        "state/lessons/",
    ]


def test_the_incident_operands_classify_as_a_sweep():
    """The unit-level statement of the same fact: the two directory operands
    are what make this a sweep, and the two file operands do not rescue it."""
    assert dc._bt_commit_scope_is_sweeping(shlex.split(INCIDENT_CMD), os.getcwd())
# ---------------------------------------------------------------------------
# The sweep advisory's own override key
# ---------------------------------------------------------------------------


def test_the_sweep_override_silences_the_sweep(monkeypatch):
    monkeypatch.setenv("COORDINATOR_ALLOW_GIT_COMMIT_SCOPE_SWEEP", "1")
    assert not _advises("git commit -o state/ -m x")


def test_the_sweep_override_does_not_silence_a_bare_commit(monkeypatch):
    """The two keys make different claims. `..._SCOPE_SWEEP` means "I named a
    subtree and I meant that subtree"; `..._BARE` means "I meant the whole
    index". An operator who set one has not decided the other, so neither may
    stand in for it."""
    monkeypatch.setenv("COORDINATOR_ALLOW_GIT_COMMIT_SCOPE_SWEEP", "1")
    assert _advises("git commit -m x")


def test_the_bare_override_is_not_required_to_silence_a_sweep(monkeypatch):
    """Regression guard on the vocabulary: before this key existed, the only
    way to silence a widened sweep advisory was to claim you meant a bare
    commit, which is a different and larger claim than the operator was
    making."""
    monkeypatch.delenv("COORDINATOR_ALLOW_GIT_COMMIT_BARE", raising=False)
    monkeypatch.setenv("COORDINATOR_ALLOW_GIT_COMMIT_SCOPE_SWEEP", "1")
    assert not _advises("git commit -m x -- state/")


def test_the_sweep_override_is_registered_in_the_operator_reference():
    """A key an operator cannot look up is not an override, it is folklore.
    `docs/reference/guard-override-keys.md` is the route from a guard name to
    its key, so absence there is the defect, not a docs nicety."""
    import pathlib as _p

    doc = _p.Path(__file__).resolve().parents[3] / "docs" / "reference" / "guard-override-keys.md"
    assert "COORDINATOR_ALLOW_GIT_COMMIT_SCOPE_SWEEP" in doc.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# C -- a sweeping scope must not inherit the no-scope-at-all vocabulary
#
# Two independent field reports of the same defect: example-retrieval-repo-em 2026-09-02
# ("the commit guard says 'names no scope' at a command that named one"), and
# doe-claude-em 2026-09-04, which reproduced it by calling the check function
# directly -- no dispatcher, no `cd` prefix -- and so retired the theory that
# `offer-git-c`'s rewrite short-circuit was the cause.
#
# Section A above pins that the sweeping form is not SILENT. That left the
# text unpinned, and control fell through into branches written for a command
# with no pathspec at all: the operator was told their command "names no
# scope" and handed back, verbatim, the command they had just typed. Both
# halves of that are asserted here.
#
# NEGATIVE SPEC: these rows assert TEXT, never severity. The deny/advisory
# split is untouched by them and stays where section A's own negative spec
# leaves it -- escalating the sweeping form is direction-class and sits with
# DoE's strict-mode question.
# ---------------------------------------------------------------------------


def _reason(cmd: str) -> str:
    out = dc.check_git_commit_safe_commit_advise(cmd, "", _payload())
    assert out is not None, cmd
    return out["hookSpecificOutput"]["permissionDecisionReason"]


#: The compound form the earlier suite never exercised. Every row above is a
#: solo commit, so the interaction between a sweeping scope and the
#: 2026-08-30 compound-add deny was invisible to a green run.
COMPOUND_SWEEP_CMD = 'git add -- state/ && git commit -m "x" -- state/'


def test_a_sweeping_scope_is_not_told_it_named_no_scope():
    assert "names no scope" not in _reason("git commit -m x -- state/")


def test_the_compound_sweeping_form_is_not_told_it_named_no_scope():
    assert "names no scope" not in _reason(COMPOUND_SWEEP_CMD)


def test_a_sweeping_scope_is_told_which_operand_is_the_directory():
    assert "state/ names a directory" in _reason("git commit -m x -- state/")


def test_several_sweeping_operands_are_all_named():
    reason = _reason("git commit -m x -- state/lessons/ state/audits/")
    assert "state/lessons/, state/audits/ name directories" in reason


def test_the_remediation_is_not_the_command_it_just_refused():
    """The circularity is the operator-facing cost, not a wording nit: a
    remedy character-for-character identical to the refused command reads as
    a parser failure and sends the operator permuting spellings. Asserted on
    the compound form, where the old text offered back both halves."""
    assert "git add -- <paths> && git commit" not in _reason(COMPOUND_SWEEP_CMD)


def test_the_remediation_asks_for_files():
    assert "-- <file> <file>" in _reason(COMPOUND_SWEEP_CMD)


def test_a_bare_commit_keeps_the_no_scope_vocabulary():
    """The false-positive floor for section C. A command that really names
    nothing must still be told so -- the fix narrows which commands get that
    sentence, it does not retire the sentence."""
    assert "names no scope" in _reason("git commit -m x")


def test_only_naming_nothing_is_told_the_flag_is_the_problem():
    """`--only` with no operand this walk can see sweeps too, and has no
    operand to quote, so the flag itself becomes the subject rather than the
    message falling back to a sentence about directories that names none."""
    reason = _reason("git commit --only -m x")
    assert "'--only' selects git's self-scoped mode but names no path" in reason


def test_an_amend_outranks_a_sweeping_scope_in_the_message(monkeypatch):
    """A scoped `--amend` still overwrites a commit that may be a peer's, and
    no amount of pathspec wording addresses that. The amend body must win."""
    monkeypatch.setenv("COORDINATOR_ALLOW_GIT_COMMIT_AMEND", "1")
    reason = _reason("git commit --amend -m x -- state/")
    assert "rewrites whatever commit is at HEAD" in reason
    assert "names a directory" not in reason
