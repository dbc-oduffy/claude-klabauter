"""The plan-path script reviews each executor group with Sonnet as it concludes, then one final Opus pass.

Composes a two-wave script with `emit.compose_script` and reads the emitted text.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.review_mint.execute_review import OperativeCriterion
from coordinator_core.ops.review_mint.final_review import final_review_mandate

_ROOT = Path(__file__).resolve().parents[4]
_CRITERION = "the emitted script ends review with one verified final Opus pass"
_FIRST = "coordinator_core/ops/dispatch_emit/wave_map.py"
_SECOND = "coordinator_core/ops/dispatch_emit/pathspec.py"

_STAGE_SCHEMAS = {
    "review-prep-result": {"type": "object"},
    "slice-review-result": {"type": "object"},
    "review-integration-result": {"type": "object"},
}


def _row(row_id: str, writes: list[str], depends_on: list[str] | None = None) -> WaveRow:
    return WaveRow(
        id=row_id, title=f"title-{row_id}", surface="dispatch_emit", writes=writes, reads=[],
        depends_on=depends_on or [], agent_type=None, agent_model=None,
    )


def _roster() -> dict:
    return {
        "schema": "review-roster-fragment",
        "schema_version": 5,
        "blocking_verdicts": {"coordinator:code-reviewer": "BLOCKED"},
        "execute_review": {
            "brightline": {"op": "review_brightline_gate", "partition_on": "PARTITION-MANDATORY"},
            "stages": [
                {
                    "kind": "prep",
                    "agents": [
                        {"agentType": "coordinator:test-runner", "model": "haiku", "effort": "low", "schema": "review-prep-result"}
                    ],
                },
                {
                    "kind": "review-wave",
                    "agents": [
                        {
                            "agentType": "coordinator:code-reviewer", "model": "sonnet", "effort": "low",
                            "per": "slice", "applies": "own-slice", "schema": "slice-review-result",
                        }
                    ],
                },
                {
                    "kind": "integration",
                    "agents": [
                        {
                            "agentType": "coordinator:overengineering-reviewer", "model": "opus", "effort": "low",
                            "applies": "residue", "schema": "review-integration-result",
                        }
                    ],
                },
            ],
        },
    }


def _compose() -> str:
    waves = [[_row("C1", [_FIRST])], [_row("C2", [_SECOND], ["C1"])]]
    context = emit.PlanContext(
        title="t", goal=None, problem_excerpt=None, operative_criterion=OperativeCriterion(statement=_CRITERION)
    )
    return emit.compose_script(
        waves, name="wf", description="d", review_roster_fragment=_roster(), review_stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/p.md", run_base_sha="a" * 40, repo_root=_ROOT,
        session_id="9d860631-c114-462a-a6ee-f685cc671ced", plan_context=context,
    )


def test_each_group_launch_awaits_only_its_quiescence_rows_and_runs_sonnet():
    text = _compose()
    launches = list(re.finditer(r"_reviewGroups\[(\d+)\] = \(async \(\) => \{ await Promise\.all\(\[(.*?)\]\)\.catch", text))
    assert len(launches) == 2
    waits = [re.findall(r"_rows\['([^']+)'\]", m.group(2)) for m in launches]
    assert waits[0] == ["C1"]
    assert set(waits[1]) == {"C1", "C2"}
    groups = text[text.index("phase('Review groups')") : text.index("phase('Review prep')")]
    assert groups.count("model: 'sonnet'") >= 2 and "model: 'opus'" not in groups


def _after_barrier(text: str) -> str:
    return text[text.index("phase('Review prep')") : text.index("phase('Review re-verify')")]


def test_exactly_one_opus_call_sits_after_every_group_and_whole_diff_await_and_carries_the_mandate():
    text = _compose()
    assert len(re.findall(r"model: ['\"]opus['\"]", text)) == 1
    opus = text.index("model: 'opus'")
    assert opus > text.index("await Promise.all(_reviewGroups)")
    assert opus > text.index("const _wholeWave = await parallel(")
    assert opus > text.index("phase('Review integration')")
    integration = _after_barrier(text)
    first, second = final_review_mandate(_CRITERION).split("\n")[:2]
    assert first in integration and second[:40] in integration
    assert integration.count(_CRITERION) == 1


def test_the_re_verify_phase_follows_the_final_opus_call():
    text = _compose()
    assert text.index("model: 'opus'") < text.index("phase('Review re-verify')")
