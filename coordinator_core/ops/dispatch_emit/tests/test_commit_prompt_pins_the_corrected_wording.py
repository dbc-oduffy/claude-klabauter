"""Pins the corrected commit-phase brief shape after the false-claims fix.

Spec backlink: this dispatch's brief (2026-08-31) -- three false claims in
`subject_rule`/`deliverable_rule` cost a halted wave and three misrouted
cross-repo memos: (1) close-out does not join on commit subject or the
Deliverable-Id trailer at all (`close_out_and_stamp.py`'s own docstrings);
(2) `scoped-git-commit` has no `--deliverable-id` flag
(`coordinator_core/git/commit.py :: commit_paths`'s signature); (3) a
mismatched trailer is not unrecoverable.

Negative-spec: the emitted commit prompt must never again claim a
subject/trailer join ("joins on the subject") or instruct a nonexistent
`--deliverable-id` flag, and must name its own composing op so a reader
knows where to correct the wording.
"""

from coordinator_core.ops.dispatch_emit.emit import _commit_agent_call


def test_commit_prompt_never_claims_the_deleted_subject_join():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "joins on the subject" not in call


def test_commit_prompt_never_instructs_the_nonexistent_flag():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "--deliverable-id" not in call


def test_commit_prompt_names_its_own_composing_op():
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "coordinator_core/ops/dispatch_emit/emit.py" in call
