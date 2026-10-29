"""Ask route: Review re-verify phase on, returned, and targeted_tests carried into both commit params blocks."""

from __future__ import annotations

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


def test_reverify_phase_names_the_op_and_the_test_runner():
    script = _compose()
    assert "review.reverify" in script
    assert "coordinator:test-runner" in script
    assert "const _reviewReverify" not in script  # pre-declared, const stripped
    assert "let _reviewReverify = null;" in script


def test_return_carries_reverify_under_review():
    assert "reverify: _reviewReverify, receipt:" in _compose()


def test_staged_review_reverify_body_leaves_the_script_and_lands_in_the_sink():
    sink: dict[str, str] = {}
    script = _compose(staged_out=sink)
    assert "prompts/review-reverify.md in full" in script
    assert "review.reverify" in sink["review-reverify"]
    assert "Re-verify the edits this run" not in script
    assert "Re-verify the edits this run" in _compose()


def test_both_commit_params_blocks_carry_targeted_tests_verbatim():
    script = _compose(in_run_commit=True, script_path="scratch/warp/a.workflow.mjs")
    assert script.count(_TARGETED_TESTS) == 2


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


def test_unapplied_reverify_reruns_verify_once():
    script = _compose()
    assert "_reviewReverify.unapplied.length) || (Array.isArray(_reviewReverify.failing) && _reviewReverify.failing.length))) { _verify = await _runVerify(); }" in script
