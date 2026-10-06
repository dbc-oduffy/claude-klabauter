"""The emitted triage brief makes a row's pm_ruling a binding input."""
from coordinator_core.ops.dispatch_emit import grind_stages


def test_triage_prompt_carries_pm_ruling_clause():
    call = grind_stages.compose_triage_call(
        label="triage", phase_title="Grind", run_dir="runs/x", profile="p",
        verdicts=("fix", "park"),
    )
    assert "`pm_ruling`" in call
    assert "binding" in call
    assert "never issue a fresh park" in call
