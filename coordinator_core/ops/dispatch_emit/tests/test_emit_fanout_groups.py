"""The plan-route script shares one agent between small rows with identical predecessors and
reports the agent cost in its meta `fanout` object."""

from __future__ import annotations

import re

from coordinator_core.ops._workflow_contract import Severity, extract_meta_block, run_checks
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.fanout_cost import read_fanout
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW, execute_section

_ROW_AGENT_RE = re.compile(r"label: 'work:")


def _row(id_: str, path: str, **kw) -> WaveRow:
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface="dispatch_emit",
        writes=[path],
        reads=[],
        depends_on=[{"chunk": c} for c in kw.pop("depends_on", [])],
        **kw,
    )


def _four_rows_after_one() -> list[list[WaveRow]]:
    base = "coordinator_core/ops/dispatch_emit"
    return [
        [_row("C1", f"{base}/spine_read.py")],
        [
            _row("C2", f"{base}/wave_map.py", depends_on=["C1"]),
            _row("C3", f"{base}/pathspec.py", depends_on=["C1"]),
            _row("C4", f"{base}/work_label.py", depends_on=["C1"]),
            _row("C5", f"{base}/chatty.py", depends_on=["C1"]),
        ],
    ]


def test_four_small_rows_sharing_predecessors_run_in_two_agents():
    script = compose_script(_four_rows_after_one(), name="wf", description="d", **REVIEW_KW)
    section = execute_section(script)
    assert len(_ROW_AGENT_RE.findall(section)) == 3  # C1 solo + two groups
    assert "_groupCalls['C2']" in section and "_groupCalls['C4']" in section
    for rid in ("C2", "C3", "C4", "C5"):
        assert f"_rows['{rid}'] = _runRow('{rid}'" in section
    assert "You own 2 assignments, in this order: C2, C3" in section


def test_meta_fanout_is_present_and_pure_literal():
    script = compose_script(_four_rows_after_one(), name="wf", description="d", **REVIEW_KW)
    fanout = read_fanout(script)
    assert fanout is not None
    assert fanout["units"] == 5
    assert fanout["expectedAgents"] >= 3
    assert fanout["basis"] in ("exact", "lower-bound")
    assert fanout["groups"] == [["C1"], ["C2", "C3"], ["C4", "C5"]]
    assert extract_meta_block(script) is not None
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def test_rows_with_different_predecessors_stay_solo():
    base = "coordinator_core/ops/dispatch_emit"
    waves = [
        [_row("C1", f"{base}/spine_read.py"), _row("C2", f"{base}/wave_map.py")],
        [
            _row("C3", f"{base}/pathspec.py", depends_on=["C1"]),
            _row("C4", f"{base}/work_label.py", depends_on=["C2"]),
            _row("C5", f"{base}/chatty.py", depends_on=["C1", "C2"]),
        ],
    ]
    script = compose_script(waves, name="wf", description="d", **REVIEW_KW)
    assert read_fanout(script)["groups"] == [["C1", "C2"], ["C3"], ["C4"], ["C5"]]


def test_reviewer_slot_and_differing_type_rows_stay_solo():
    base = "coordinator_core/ops/dispatch_emit"
    waves = [
        [
            _row("C1", f"{base}/spine_read.py", agent_type="coordinator:test-runner"),
            _row("C2", f"{base}/wave_map.py"),
            _row("C3", f"{base}/pathspec.py"),
            _row("C4", f"{base}/work_label.py", agent_type="coordinator:code-reviewer"),
            _row("C5", f"{base}/chatty.py"),
        ],
    ]
    script = compose_script(waves, name="wf", description="d", slot_rows=["C5"], **REVIEW_KW)
    assert read_fanout(script)["groups"] == [["C1"], ["C2", "C3"], ["C4"], ["C5"]]


def test_expected_agents_counts_each_review_wave_lane():
    import copy

    from .conftest import _V5_ROSTER_FRAGMENT

    base = "coordinator_core/ops/dispatch_emit"
    waves = [[_row("C1", f"{base}/spine_read.py")]]
    baseline = read_fanout(compose_script(waves, name="wf", description="d", **REVIEW_KW))["expectedAgents"]

    fragment = copy.deepcopy(_V5_ROSTER_FRAGMENT)
    wave_stage = next(s for s in fragment["execute_review"]["stages"] if s["kind"] == "review-wave")
    for i in (2, 3):
        lane = dict(wave_stage["agents"][0])
        lane["agentType"] = f"coordinator:code-reviewer-{i}"
        wave_stage["agents"].append(lane)
    kw = {**REVIEW_KW, "review_roster_fragment": fragment}
    widened = read_fanout(compose_script(waves, name="wf", description="d", **kw))["expectedAgents"]
    assert widened - baseline == 2


def test_member_missing_from_group_reply_reads_incomplete():
    script = compose_script(_four_rows_after_one(), name="wf", description="d", **REVIEW_KW)
    assert "function _memberStatus(text, id)" in script
    assert "function _memberResult(text, id)" in script
