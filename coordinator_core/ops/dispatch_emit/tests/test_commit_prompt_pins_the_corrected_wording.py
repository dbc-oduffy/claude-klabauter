
from coordinator_core.ops.dispatch_emit.emit import _commit_agent_call


def test_commit_prompt_never_claims_the_deleted_subject_join():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], deliverable_id="dlv-a-plan-99b845"
    )
    assert "joins on the subject" not in call


def test_commit_prompt_names_its_own_composing_op():
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "coordinator_core/ops/dispatch_emit/emit.py" in call


def test_commit_prompt_pins_the_probe_commit_prohibition_wording():
    """F1's corrected wording (measured: `2c0684479`, `63a3bf5fd`) -- the
    orphan/holder reconciliation clause now forbids probe commits and
    requires resolving every path in a truncated denial's refused set."""
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], results_var="wave1Results"
    )
    assert (
        "NEVER commit a "
        "path, or a subset of your pathspec, on its own to test whether the guard "
        "accepts it -- it lands on this shared branch and cannot be rewritten "
        "(measured: "
        in call
    )
    assert (
        "A denial arrives truncated, naming "
        "only its FIRST refused path -- run "
        in call
    )
    assert "on EVERY refused path before choosing a case, not only the one " "printed:" in call
