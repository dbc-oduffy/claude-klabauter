"""A peer landing this wave's paths first is a success, not a halt.

The commit phase's ALREADY-COMMITTED clause assumes whoever committed your paths
was this phase on an earlier pass, so it looks for ONE commit carrying every
chunk id. On a shared checkout that assumption breaks: a peer session or the
dispatching EM commits the same paths first, across commits carrying none of
those ids, and `commit_paths` raises `NothingToCommit`.

Measured 2026-09-10: the mise-prep run's commit agent found all ten declared
files correct and committed — across three commits — could not find one bearing
the wave's ids, withheld the success token, and halted a run whose work was
already safely on disk. /mise-en-place names that event (a peer-session commit
collision) and expects it; the emitted phase had no vocabulary for it.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import _commit_agent_call


def _prompt() -> str:
    return _commit_agent_call(
        ["docs/plans/p.md"], "Commit wave 1", 0, ["C1"], results_var="wave1Results"
    )


def test_the_peer_landed_state_is_named_as_a_success():
    prompt = _prompt()
    assert "LANDED BY SOMEONE ELSE is a THIRD state" in prompt
    assert "NothingToCommit" in prompt


def test_it_demands_both_checks_rather_than_a_clean_tree_alone():
    """The original clause's whole point is that tracked-and-clean is equally
    true of a path this run never touched. The new branch may not relax that."""
    prompt = _prompt()
    assert "git status --porcelain" in prompt
    assert "git diff HEAD" in prompt
    assert "executor reports for this wave" in prompt


def test_it_requires_the_real_carrying_commits_be_named():
    """The success token reports HEAD, so attribution would otherwise be lost."""
    assert "git log --oneline -1" in _prompt()


def test_the_original_resumed_run_clause_survives():
    """A third state is added; the two that existed are not loosened."""
    prompt = _prompt()
    assert "ALREADY COMMITTED" in prompt
    assert "do not report success on clean-tree" in prompt
