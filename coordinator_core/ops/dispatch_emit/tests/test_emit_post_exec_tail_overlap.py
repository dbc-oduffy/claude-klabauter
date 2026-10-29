"""The plan-path script overlaps review and verify, pipelines review per executor group and bounds verify.

AC1-AC3, AC6 and AC7 read the re-captured byte-parity golden (the door's output) and a fresh compose;
the budget race runs under node with a never-resolving agent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.pathspec import terminal_test_scope
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.review_mint.post_exec_tail import VERIFY_WALL_BUDGET_S, group_slice_prefix

_ROOT = Path(__file__).resolve().parents[4]
_GOLDEN = _ROOT / "coordinator_core" / "benchmarks" / "fixtures" / "emit_full_path_plan.expected.script.mjs"
_BARRIER = "await Promise.all(Object.values(_rows));"
_WAKE_DIGEST_SCHEMA = _ROOT / "coordinator_core" / "contract" / "wake-digest.schema.json"

_SERIAL_TAIL = (
    "  const _rows = {};\n  _rows['C1'] = _runRow('C1');\n  " + _BARRIER + "\n"
    "  phase('Review prep');\n  phase('Review wave');\n  phase('Review re-verify');\n  phase('Verify');\n"
    "  if (!_halted) {\n  phase('Verify');\n  const _verifyReply = await agent('verify');\n  }\n"
)


def _golden() -> str:
    return _GOLDEN.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _waits(text: str) -> list[list[str]]:
    return [
        re.findall(r"_rows\['([^']+)'\]", m.group(1))
        for m in re.finditer(r"_reviewGroups\[\d+\] = \(async \(\) => \{ await Promise\.all\(\[(.*?)\]\)\.catch", text)
    ]


def _ac1(text: str) -> None:
    barrier = text.index(_BARRIER)
    launches = [m.start() for m in re.finditer(r"_reviewGroups\[\d+\] = \(async", text)]
    assert launches and all(at < barrier for at in launches), "a review group launches after the all-rows barrier"
    every_row = set(re.findall(r"_rows\['([^']+)'\] = _runRow", text))
    waits = _waits(text)
    assert len(waits) == len(launches)
    assert all(w and set(w) <= every_row for w in waits), "a group awaits a row no launch declares"
    assert text.index("phase('Review prep')") > barrier


def _ac2(text: str) -> None:
    launch = text.index("let _verifyTestRun = null;")
    assert launch < text.index("phase('Review prep')") < text.index("phase('Review wave')")
    judge = text.index("phase('Verify')")
    assert text.index("phase('Review re-verify')") < judge
    block = text[judge:]
    assert "JSON.stringify(_testResult)" in block and "JSON.stringify(_reviewReverify)" in block


def _ac3(text: str) -> None:
    ms = str(VERIFY_WALL_BUDGET_S * 1000)
    assert text.count("Promise.race([") >= 2 and text.count(f"setTimeout(() => _res({{status: 'indeterminate'") == 2
    assert text.count(f"}}), {ms}); }})]).finally(() => clearTimeout(_t)))()") == 2
    assert "verify budget exceeded (300s): scoped tests" in text
    assert "verify budget exceeded (300s): criterion judge" in text
    assert "agent still running (orphaned), not stopped: " in text
    assert "await _testCall" not in text and "await _judgeCall" not in text
    guard = text.index("  if (!_halted) {\n  phase('Verify');")
    assert text.index("_testResult = await _verifyTestRun;") > guard
    assert "_testResult = await _verifyTestRun;" not in text[:guard]
    assert text.index("    const _testCall = ") > text.index("let _verifyTestRun = null;\n  if (!_halted) {")


def test_the_golden_pipelines_review_overlaps_verify_and_bounds_it():
    text = _golden()
    _ac1(text)
    _ac2(text)
    _ac3(text)


@pytest.mark.parametrize("check", [_ac1, _ac2, _ac3])
def test_each_assertion_reads_red_on_the_serial_tail(check):
    with pytest.raises((AssertionError, ValueError)):
        check(_SERIAL_TAIL)


def test_the_orphan_record_adds_no_wake_digest_key():
    schema = json.loads(_WAKE_DIGEST_SCHEMA.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False and schema["properties"]["tests"]["additionalProperties"] is False
    assert "orphan" not in _WAKE_DIGEST_SCHEMA.read_text(encoding="utf-8").lower()


def _row(row_id: str, writes: list[str], depends_on: list[str] | None = None) -> WaveRow:
    return WaveRow(
        id=row_id, title=f"title-{row_id}", surface="dispatch_emit", writes=writes, reads=[],
        depends_on=depends_on or [], agent_type=None, agent_model=None,
    )


_SHARED = "coordinator_core/ops/dispatch_emit/wave_map.py"
_OTHER = "coordinator_core/ops/dispatch_emit/pathspec.py"
_WAVES = [
    [_row("C1", [_SHARED])],
    [_row("C2", [_OTHER], ["C1"])],
    [_row("C3", [_SHARED], ["C2"])],
]


def _compose(waves=_WAVES, plan_path="docs/plans/p.md", **kwargs) -> str:
    from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

    fragment = _v5_fragment()
    fragment["execute_review"]["stages"].append(
        {
            "kind": "judge",
            "agents": [{"agentType": "coordinator:exit-criterion-judge", "model": "sonnet", "effort": "low", "schema": "judge-result"}],
        }
    )
    return emit.compose_script(
        waves, name="wf", description="d", review_roster_fragment=fragment,
        review_stage_schemas={**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        plan_path=plan_path, run_base_sha="a" * 40, repo_root=_ROOT, session_id="9d860631-c114-462a-a6ee-f685cc671ced",
        **kwargs,
    )


def test_a_group_whose_file_a_later_wave_rewrites_waits_for_that_row():
    text = _compose()
    _ac1(text)
    first, second, third = _waits(text)
    assert first == ["C1", "C3"]
    assert second == ["C2"] and "C3" in third


def test_the_surface_no_row_declares_goes_to_the_last_group_which_waits_for_every_row():
    text = _compose()
    first, second, third = _waits(text)
    assert sorted(third) == ["C1", "C2", "C3"]
    found = [m.group(1) for m in re.finditer(r'"slice_prefix":"[^"]+","paths":(\[[^\]]*\])', text)]
    paths = [json.loads(p) for p in found if "<" not in p]
    assert _SHARED in paths[0] and not any(p.endswith("pathspec.py") for p in paths[0])
    assert len(paths[2]) > 1


def test_the_groups_block_follows_the_row_launches_and_precedes_the_barrier():
    text = _compose()
    assert text.rindex("_rows['C3'] = _runRow(") < text.index("phase('Review groups')") < text.index(_BARRIER)
    assert "Review groups" in text.split("phases: [", 1)[1].split("]", 1)[0]


def test_reverify_names_a_call_per_group_prefix_and_the_main_prefix_and_reverts_each_with_one_failing_set():
    text = _compose()
    block = text[text.index("phase('Review re-verify')") : text.index("phase('Verify')")]
    block = block[block.index("Re-verify the edits") :]
    key = "9d860631-p"
    for i in range(3):
        assert f'"slice_prefix":"{group_slice_prefix(key, i, "a" * 40)}"' in block
    assert "for EVERY prefix" in block and "<all failing node ids>" in block
    assert "owns" not in block and "unattributed only when every prefix reports it so" in block
    assert block.count("review.reverify") >= 4


def test_two_plans_emitted_in_one_session_mint_disjoint_group_slice_ids_carrying_their_stems():
    def prefixes(plan_path: str) -> set[str]:
        text = _compose(plan_path=plan_path)
        return set(re.findall(r'"slice_prefix":"([^"]+)"', text))

    first = {p for p in prefixes("docs/plans/alpha plan.md") if ".g" in p}
    second = {p for p in prefixes("docs/plans/beta.md") if ".g" in p}
    assert first and second and first.isdisjoint(second)
    assert all("alpha-plan" in p for p in first) and all("beta" in p for p in second)
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in first | second)


def test_the_rerun_set_is_widened_and_the_verify_scope_is_unchanged():
    text = _compose()
    block = text[text.index("phase('Review re-verify')") : text.index("phase('Verify')")]
    mapped = terminal_test_scope([[_row("C1", [_SHARED])]], repo_root=_ROOT)
    assert mapped and f'"{_SHARED}": {json.dumps(mapped)}' in block
    scope = terminal_test_scope(_WAVES, repo_root=_ROOT)
    leg = text[text.index("Run the scoped test targets: [") :].split("]", 1)[0]
    assert all(target in leg for target in emit.collapse_test_scope(scope, _ROOT))


def test_a_run_with_writes_under_or_chatty_keeps_the_serial_review():
    chatty = _compose(chatty=True)
    assert "phase('Review groups')" not in chatty and "Review groups" not in chatty.split("phases: [", 1)[1].split("]", 1)[0]
    assert "phase('Review prep')" in chatty


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    return node


def _run_tail(monkeypatch, *, halted: str, test_agent: str, judge_agent: str) -> dict:
    monkeypatch.setattr(emit, "VERIFY_WALL_BUDGET_S", 1)
    script = (
        "(async () => {\n"
        f"let _halted = {halted}; let _reviewReverify = null; let _testResult = null; let _falsifierResult = null;\n"
        "const calls = []; const log = () => {}; const phase = () => {};\n"
        "const agent = (p, o) => { calls.push(o.label); return o.label === 'test:terminal' ? "
        f"{test_agent} : {judge_agent}; }};\n"
        + emit._verify_test_launch_js("agent('t', { label: 'test:terminal' })")
        + "\n  if (!_halted) {\n"
        + emit._verify_judge_block_js("agent('j', { label: 'judge' })")
        + "\n  }\n"
        "console.log(JSON.stringify({ calls, test: _testResult, judge: _falsifierResult }));\n"
        "})();"
    )
    out = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, check=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return json.loads(out)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_overrunning_test_leg_resolves_to_a_recorded_orphan_and_nothing_awaits_it(monkeypatch):
    got = _run_tail(
        monkeypatch, halted="null", test_agent="new Promise(() => {})",
        judge_agent="Promise.resolve({ status: 'met', differs_from_baseline: true })",
    )
    assert got["test"]["status"] == "error"
    assert "verify budget exceeded (1s): scoped tests" in got["test"]["summary"]
    assert "agent still running (orphaned), not stopped: scoped tests" in got["test"]["summary"]
    assert got["judge"]["status"] == "met"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_overrunning_judge_stays_indeterminate_and_names_the_orphan(monkeypatch):
    got = _run_tail(
        monkeypatch, halted="null", test_agent="Promise.resolve({ status: 'pass', tests_run: 3, tests_failed: 0 })",
        judge_agent="new Promise(() => {})",
    )
    assert got["test"]["status"] == "pass"
    assert got["judge"]["status"] == "indeterminate" and got["judge"]["differs_from_baseline"] is None
    assert "agent still running (orphaned), not stopped: criterion judge" in got["judge"]["reason"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_stop_rule_halt_before_launch_starts_no_verify_agent_and_binds_nothing(monkeypatch):
    got = _run_tail(monkeypatch, halted="'C2'", test_agent="Promise.resolve({})", judge_agent="Promise.resolve({})")
    assert got["calls"] == [] and got["test"] is None and got["judge"] is None
