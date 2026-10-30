"""The ask script's terminal return surfaces a research halt's fire, and the halt-record commit stays composed."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_RESEARCH
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="survey four repos and rank them",
        sizing_rel=None,
        run_id="run-research-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_terminal_return_reads_the_research_halts_fire_between_split_and_released():
    script = _script()
    ret = script[script.rindex("  return { arm:") :]
    split = ret.index("_splitExit ? _splitExit.fire")
    research = ret.index(f"_halted.halted === '{HALT_RESEARCH}' && Array.isArray(_halted.fire) ? _halted.fire")
    released = ret.index("_commit?.close?.siblings?.released ?? []")
    assert split < research < released


def test_a_research_halt_with_a_fire_takes_the_handoff_next_action_not_revise_and_refire():
    script = _script()
    na = script[script.index("  _nextAction = _closeable"):]
    na = na[: na.index(";\n")]
    assert na.index("kind: 'fire_research'") < na.index("kind: 'revise_and_refire'")


def test_halt_record_commit_is_composed_and_guarded_only_by_halted_and_token():
    script = _script()
    guard = "if (_halted && _RUN_TOKEN && !(_commit && _commit.committed === true)) {"
    assert script.count(guard) == 1
    assert "async function _commitHaltRecord()" in script
    assert "_haltCommit = await _commitHaltRecord()" in script
    assert "research" not in guard
