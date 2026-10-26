"""Pure tests for ledger_recovery: attribution, footprint, landing-withheld and the JS rendering."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
from coordinator_core.win_portability import no_console_creationflags

TEST_FILE = "tests/test_tool_producer_failure_disclosure.py"


def _r43_rows():
    return [
        {"id": "C1", "writes": ["pkg/live.py"], "register_rows": ["R5"]},
        {"id": "C2", "writes": ["pkg/producer.py", TEST_FILE], "register_rows": ["R5"]},
        {"id": "C3", "writes": ["pkg/other.py"], "register_rows": ["R5"]},
    ]


def _r43_item():
    return {
        "id": "R5",
        "status": "not_met",
        "remaining": [f"Fix the 2 failing tests in {TEST_FILE}", "Prove bare-name run from a foreign cwd"],
        "evidence": "2 failed",
    }


def test_r43_shape_reopens_only_the_chunk_writing_the_failing_test():
    out = lr.reopen_plan([_r43_item()], _r43_rows(), [TEST_FILE + "::test_a"], [])
    assert out == {"reopen": {"C2": ["R5"]}, "held": {}, "unattributed": []}


def test_432570_shape_holds_absent_chunks_and_reopens_nothing():
    rows = [{"id": f"C{i}", "writes": [f"src/m{i}.py"], "register_rows": ["T5"]} for i in range(6)]
    item = {"id": "T5", "status": "not_met", "remaining": ["C7 not run", "C8/C9 wait on plan", "C10 pending"]}
    out = lr.reopen_plan([item], rows, [], ["C7", "C8", "C9", "C10"])
    assert out["reopen"] == {}
    assert out["held"] == {c: ["T5"] for c in ("C7", "C8", "C9", "C10")}
    assert out["unattributed"] == []


def test_rule_three_needs_exactly_one_claimant():
    item = {"id": "R9", "status": "not_met", "remaining": ["something vague"]}
    one = [{"id": "A", "writes": ["a.py"], "register_rows": ["R9"]}, {"id": "B", "writes": ["b.py"], "register_rows": ["R1"]}]
    two = [{"id": "A", "writes": ["a.py"], "register_rows": ["R9"]}, {"id": "B", "writes": ["b.py"], "register_rows": ["R9"]}]
    assert lr.reopen_plan([item], one, [], [])["reopen"] == {"A": ["R9"]}
    out = lr.reopen_plan([item], two, [], [])
    assert out["reopen"] == {} and out["unattributed"] == ["R9"]


def test_rule_three_is_skipped_when_a_named_chunk_matches():
    item = {"id": "R9", "status": "not_met", "remaining": ["finish B"]}
    rows = [{"id": "A", "writes": ["a.py"], "register_rows": ["R9"]}, {"id": "B", "writes": ["b.py"], "register_rows": ["R1"]}]
    assert lr.reopen_plan([item], rows, [], [])["reopen"] == {"B": ["R9"]}


@pytest.mark.parametrize(
    "over",
    [
        {"status": "drifted"},
        {"status": "post_commit"},
        {"status": "met"},
        {"owed_by": ["X1"]},
        {"withheld_by": "C3"},
        {"essential": False},
    ],
)
def test_non_blocking_items_are_never_reopened(over):
    item = {**_r43_item(), **over}
    assert lr.reopen_plan([item], _r43_rows(), [TEST_FILE], []) == {"reopen": {}, "held": {}, "unattributed": []}


def test_a_manifest_id_is_reopened_not_held_even_if_listed_held():
    item = {"id": "R5", "status": "not_met", "remaining": ["C1 incomplete"]}
    out = lr.reopen_plan([item], _r43_rows(), [], ["C1"])
    assert out["reopen"] == {"C1": ["R5"]} and out["held"] == {}


def test_a_path_inside_an_id_like_token_is_not_an_id():
    item = {"id": "R5", "status": "not_met", "remaining": ["edit src/C1.py"]}
    rows = [{"id": "C1", "writes": ["other.py"], "register_rows": ["R1"]}]
    assert lr.reopen_plan([item], rows, [], [])["reopen"] == {}


def test_footprint_is_posix_and_deduplicated():
    row = {"id": "C2", "writes": ["pkg\\producer.py", "./" + TEST_FILE]}
    fp = lr.footprint(row, [TEST_FILE + "::t1", "tests\\x.py", TEST_FILE])
    assert fp == ["pkg/producer.py", TEST_FILE]


def test_footprint_adds_failing_test_under_a_writes_under_dir():
    row = {"id": "C2", "writes": ["a.py"], "writes_under": ["tests/"]}
    assert lr.footprint(row, ["tests/test_x.py::t", "elsewhere/test_y.py"]) == ["a.py", "tests/test_x.py"]


def _land_rows():
    return [
        {"id": "C1", "writes": ["pkg/live.py"]},
        {"id": "C2", "writes": ["pkg/producer.py"]},
        {"id": "C3", "writes": ["pkg/unrelated.py"]},
        {"id": "C4", "writes": ["pkg/uses_live.py"]},
    ]


def test_landing_withheld_pulls_an_importer_of_an_exhausted_chunk():
    out = lr.landing_withheld({"C2": "exhausted"}, _land_rows(), [("pkg/live.py", "pkg/producer.py")])
    assert out == {"C2": lr.CAP_EXHAUSTED, "C1": lr.IMPORT_CLOSURE_OPEN}


def test_landing_withheld_follows_recorded_out_of_footprint_paths_and_is_transitive():
    status = {"C2": {"status": "blocked", "paths": ["pkg/_producer_substrate.py"]}}
    edges = {"pkg/live.py": ["pkg/_producer_substrate.py"], "pkg/uses_live.py": ["pkg/live.py"]}
    out = lr.landing_withheld(status, _land_rows(), edges)
    assert out == {"C2": lr.FIX_BLOCKED, "C1": lr.IMPORT_CLOSURE_OPEN, "C4": lr.IMPORT_CLOSURE_OPEN}
    assert "C3" not in out


def test_landing_withheld_leaves_unrelated_and_fixed_chunks_landing():
    out = lr.landing_withheld({"C2": "fixed", "C3": "held"}, _land_rows(), [("pkg/live.py", "pkg/producer.py")])
    assert out == {"C3": lr.HELD}


def test_landing_withheld_only_considers_py_importers():
    rows = [{"id": "C1", "writes": ["doc.md"]}, {"id": "C2", "writes": ["pkg/p.py"]}]
    assert lr.landing_withheld({"C2": "exhausted"}, rows, [("doc.md", "pkg/p.py")]) == {"C2": lr.CAP_EXHAUSTED}


def test_interface_constants_and_schema():
    assert lr.RECOVERY_ATTEMPT_CAP == 2
    assert lr.LEDGER_FIX_LABEL == "ledger-fix:" and lr.LEDGER_REVERIFY_LABEL == "ledger-reverify"
    assert lr.LEDGER_REVERIFY_LABEL != "verify"
    assert lr.LEDGER_FIX_SCHEMA["required"] == ["status", "paths", "reason"]
    assert lr.FIX_BLOCKED != lr.CAP_EXHAUSTED


def test_fix_prompt_carries_the_negative_specs():
    p = lr.ledger_fix_prompt("r", "e", "f", "fp")
    for needle in ("${fp}", "ONLY paths", "Reproduce first", "BLOCKED", "Commit nothing", "delete its assertion"):
        assert needle in p


def test_fix_prompt_names_the_notes_file_only_when_given_one():
    assert "state/warp/r/notes.jsonl" in lr.ledger_fix_prompt("r", "e", "f", "fp", "state/warp/r/notes.jsonl")
    assert "notes.jsonl" not in lr.ledger_fix_prompt("r", "e", "f", "fp")


def test_fix_prompt_frames_remaining_as_gaps_to_close():
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_GAP_RULE

    p = lr.ledger_fix_prompt("r", "e", "f", "fp")
    assert "Gaps to close" in p
    assert HALT_GAP_RULE in p
    assert "Remaining:" not in p
    for var in ("${r}", "${e}", "${f}", "${fp}"):
        assert var in p


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    return node


def _run_js(node, call):
    program = lr.REOPEN_PLAN_JS + "\nprocess.stdout.write(JSON.stringify(" + call + "));"
    out = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reopen_plan_js_parses_and_agrees_with_python():
    node = _node()
    cases = [
        ([_r43_item()], _r43_rows(), [TEST_FILE + "::t"], []),
        (
            [{"id": "T5", "status": "not_met", "remaining": ["C7", "C8/C9", "C10"]}],
            [{"id": "C0", "writes": ["a.py"], "register_rows": ["T5"]}],
            [],
            ["C7", "C8", "C9", "C10"],
        ),
        (
            [{"id": "R9", "status": "not_met", "remaining": ["vague"]}, {"id": "R8", "status": "drifted", "remaining": ["C0"]}],
            [{"id": "A", "writes": ["a.py"], "register_rows": ["R9"]}, {"id": "B", "writes": ["b.py"], "register_rows": ["R9"]}],
            [],
            [],
        ),
    ]
    for items, rows, failed, held in cases:
        js = _run_js(node, "_reopenPlan(%s, %s, %s, %s)" % tuple(json.dumps(x) for x in (items, rows, failed, held)))
        assert js == lr.reopen_plan(items, rows, failed, held)
