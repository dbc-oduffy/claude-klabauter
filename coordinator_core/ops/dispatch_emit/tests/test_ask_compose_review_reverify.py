"""Ask route: Review re-verify phase on, returned, and targeted_tests carried into both commit params blocks."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"

_TARGETED_TESTS = ask_compose._TARGETED_TESTS_JS


def _compose(**over):
    kw = dict(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _titles(script):
    import re

    block = script.split("phases: [", 1)[1].split("]", 1)[0]
    return re.findall(r"'([^']*)'", block)


def test_reverify_phase_sits_after_the_review_wave_and_before_verify():
    titles = _titles(_compose())
    assert "Review re-verify" in titles
    assert titles.index("Review wave") < titles.index("Review re-verify") < titles.index("Verify")
    if "Review integration" in titles:
        assert titles.index("Review integration") < titles.index("Review re-verify")


def test_review_recover_and_blocker_js_exclude_renamed_nodes():
    assert "?.renamed_node ?? []" in ask_compose._test_blockers_js("_r", "_t")
    assert "rv?.renamed_node ?? []" in ask_compose._REVIEW_RECOVER_JS


def test_reverify_phase_names_the_op_and_the_test_runner():
    script = _compose()
    assert "review.reverify" in script
    assert "coordinator:test-runner" in script
    assert "const _reviewReverify" not in script  # pre-declared, const stripped
    assert "let _reviewReverify = null;" in script


def test_composed_reverify_prompt_selects_node_tests_and_never_passes_on_nothing_run():
    script = _compose()
    assert "node --test <that file>" in script
    assert 'status_before \\"not_run\\"' in script or 'status_before "not_run"' in script
    assert "Never report pass when no test ran" in script
    assert "status_before pass" not in script


def test_return_carries_reverify_under_review():
    assert "reverify: _reviewReverify, receipt:" in _compose()


def test_staged_review_reverify_body_leaves_the_script_and_lands_in_the_sink():
    sink: dict[str, str] = {}
    script = _compose(staged_out=sink)
    assert "prompts/review-reverify.md in full" in script
    assert "review.reverify" in sink["review-reverify"]
    assert "Re-verify the edits this run" not in script
    assert "Re-verify the edits this run" in _compose()


def test_every_commit_params_block_carries_targeted_tests_verbatim():
    script = _compose(in_run_commit=True, script_path="scratch/warp/a.workflow.mjs")
    # commit stage, its post-retest re-commit, and the halt record
    assert script.count(_TARGETED_TESTS) == 3


def test_halt_block_carries_targeted_tests_outside_the_terminal_commit_conditional():
    script = _compose(in_run_commit=True, script_path="scratch/warp/a.workflow.mjs")
    halt = script.split("async function _commitHaltRecord()", 1)[1].split("_terminalCommitParamsBlock(", 1)[1]
    assert halt.index("targeted_tests:") < halt.index("_nextAction.kind === 'terminal_commit'")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_composed_script_parses_as_an_async_function_body(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    # `node --check` rejects the body's top-level return; the AsyncFunction constructor parses it
    # the way the Workflow runner does.
    path = tmp_path / "s.js"
    path.write_text(_compose(), encoding="utf-8")
    program = (
        "const src = require('fs').readFileSync(process.argv[2], 'utf8')"
        ".replace(/^export const meta/m, 'const meta');"
        "new (Object.getPrototypeOf(async function () {}).constructor)("
        "'args', 'agent', 'parallel', 'pipeline', 'phase', src);"
    )
    out = subprocess.run([node, "-", str(path)], input=program, capture_output=True, text=True, **no_console_creationflags())
    assert out.returncode == 0, out.stderr


def test_reverify_joins_before_self_correction_reads_it():
    # Halted-run self-correction reads the re-verify result, so its join precedes the review-blocked
    # halt; the verify phase's join then resolves the same settled promise.
    script = _compose()
    assert "_reviewReverifyP = " in script and "const _reviewReverifyP" not in script
    join = script.index("[_verify, _reviewReverify] = await Promise.all(")
    assert script.index("const _runVerify = ") < join
    early = script.index("if (!_reverifyJoined)")
    assert early < join
    assert script.index("_halted.halted === \"review_blocked\")") > join


_SIZING = "state/sizings/x.yaml"
_OWN = "coordinator_core/tests/test_own.py"
_OWN_NODE = f"{_OWN}::test_mine"
_FOREIGN_NODE = "coordinator_core/tests/test_sibling.py::test_theirs"


def _run_halt(reverify, declared) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    manifest = {"ledger_item_ids": ["R1"]}
    if declared is not None:
        manifest["review_declared_paths"] = declared
    source = (
        f"let _halted = null; const _runId = 'r1'; const _sizingRel = {_SIZING!r}; const _planRel = 'docs/plans/p.md';\n"
        f"const _manifest = {json.dumps(manifest)}; const _reviewWave = [];\n"
        f"const _reviewReverify = {json.dumps(reverify)};\n"
        + ask_compose._refire_next_action_helper_js(_SIZING)
        + "\n"
        + ask_compose._review_blocked_halt_js(_SIZING)
        + "\nconsole.log(JSON.stringify({ halted: _halted, reverify: _reviewReverify }));\n"
    )
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_foreign_only_red_does_not_halt_and_lands_out_of_ask():
    out = _run_halt({"failing": [_FOREIGN_NODE]}, [_OWN])

    assert out["halted"] is None
    assert out["reverify"]["out_of_ask"] == [_FOREIGN_NODE]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_own_red_still_halts_and_foreign_red_rides_out_of_ask():
    out = _run_halt({"failing": [_OWN_NODE, _FOREIGN_NODE]}, [_OWN])

    assert out["halted"]["halted"] == ask_compose.HALT_REVIEW_BLOCKED
    assert [f["node_id"] for f in out["halted"]["failing"]] == [_OWN_NODE]
    assert out["reverify"]["out_of_ask"] == [_FOREIGN_NODE]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_base_green_red_outside_the_footprint_halts_as_owned_red():
    out = _run_halt(
        {"failing": [_FOREIGN_NODE, _OWN_NODE], "passed_at_base": [_FOREIGN_NODE], "pre_existing": []}, [_OWN]
    )

    assert out["halted"]["halted"] == ask_compose.HALT_REVIEW_BLOCKED
    assert [f["node_id"] for f in out["halted"]["failing"]] == sorted([_FOREIGN_NODE, _OWN_NODE])
    assert "out_of_ask" not in out["reverify"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_base_green_red_that_is_pre_existing_stays_foreign():
    out = _run_halt(
        {"failing": [_FOREIGN_NODE], "passed_at_base": [_FOREIGN_NODE], "pre_existing": [_FOREIGN_NODE]}, [_OWN]
    )

    assert out["halted"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_declared_footprint_keeps_every_red_a_blocker():
    out = _run_halt({"failing": [_FOREIGN_NODE]}, None)

    assert out["halted"]["halted"] == ask_compose.HALT_REVIEW_BLOCKED
    assert "out_of_ask" not in out["reverify"]


def _reconcile(wave, reverify, reused=False) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    source = (
        f"let _reviewReused = {json.dumps(reused)}; let _reviewWave = {json.dumps(wave)}; "
        f"let _reviewReverify = {json.dumps(reverify)};\n"
        + ask_compose._WAVE_DIFF_RECONCILE_JS
        + "\nconsole.log(JSON.stringify(_reviewWave));\n"
    )
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_CLAIMED = {"slice_id": "s1", "verdict": "BLOCKED", "findings": 2, "applied": 2, "sidecar_path": "rev/s1.md"}


def test_reconcile_runs_between_the_reverify_join_and_self_correction():
    script = _compose()
    join = script.index("if (!_reverifyJoined) { _reviewReverify = await")
    assert join < script.index(ask_compose._WAVE_DIFF_RECONCILE_JS) < script.index("const _reviewRecover = async")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_claimed_applied_with_an_empty_run_diff_reports_applied_below_findings():
    wave = _reconcile([_CLAIMED], {"edited": [], "created": []})

    assert wave[0]["applied"] == 0 and wave[0]["applied_claimed"] == 2
    assert wave[0]["unapplied_reason"] == "not in run diff"
    assert wave[0]["findings"] > wave[0]["applied"]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    "reverify, reused",
    [
        ({"edited": ["a.py"]}, False),
        ({"edited": [], "created": ["n.py"]}, False),
        ({"failing": []}, False),
        (None, False),
        ({"edited": [], "created": []}, True),
    ],
)
def test_recorded_edits_an_absent_diff_or_a_reused_wave_leave_applied_standing(reverify, reused):
    assert _reconcile([_CLAIMED], reverify, reused)[0]["applied"] == 2


def test_unapplied_reverify_reruns_verify_once():
    script = _compose()
    assert "_reviewReverify.unapplied.length) || (Array.isArray(_reviewReverify.failing) && _reviewReverify.failing.length))) { _verify = await _runVerify(); }" in script


_R1_IDS = [
    "coordinator_core/tests/test_pool_worker_console_suppression.py::test_worker_init_calls_the_binder_before_anything_else",
    "coordinator_core/tests/test_pool_worker_low_priority.py::test_worker_init_lowers_priority_before_registry_preload",
]


def _recover_drive(rv: dict) -> dict:
    from .test_ask_compose_halt_self_correction import _drive

    return _drive(reverify=rv, replies={}, own=("pkg/mod.py",))


def _r1_red(**kw) -> dict:
    rec = {"status_after": "fail", "failing": list(_R1_IDS), "pre_existing": [], "passed_at_base": []}
    rec.update(kw)
    return rec


def test_halt_reverify_prompt_asks_for_diff_named():
    js = ask_compose._review_recover_js(
        head="H", anchor="A", session_tail="", record_dir="state/warp/r1", run_id="r1"
    )
    assert "passed_at_base, diff_named)" in js and "`diff_named` each failing id" in js


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_diff_named_reds_are_owned_and_not_foreign_unverified():
    out = _recover_drive(_r1_red(diff_named=list(_R1_IDS)))

    assert "foreign_unverified" not in out["reverify"]
    assert out["foreign"] == []
    fix = [c for c in out["calls"] if c["label"] == ask_compose.HALT_FIX_LABEL]
    assert fix and all(i in fix[0]["prompt"] for i in _R1_IDS)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_without_diff_named_the_same_reds_stay_foreign_unverified():
    out = _recover_drive(_r1_red())

    assert sorted(out["reverify"]["foreign_unverified"]) == sorted(_R1_IDS)
    assert not [c for c in out["calls"] if c["label"] == ask_compose.HALT_FIX_LABEL]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_diff_named_id_that_is_pre_existing_stays_foreign():
    out = _recover_drive(_r1_red(diff_named=list(_R1_IDS), pre_existing=list(_R1_IDS)))

    assert "foreign_unverified" not in out["reverify"]
    assert not [c for c in out["calls"] if c["label"] == ask_compose.HALT_FIX_LABEL]
