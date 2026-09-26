
from coordinator_core.ops.dispatch_emit.emit import _commit_agent_call


def test_commit_prompt_never_claims_the_deleted_subject_join():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "joins on the subject" not in call


def test_commit_prompt_names_its_own_composing_op():
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "coordinator_core/ops/dispatch_emit/emit.py" in call
