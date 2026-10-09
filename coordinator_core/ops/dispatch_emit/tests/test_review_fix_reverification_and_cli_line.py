"""Review fixes are re-verified by the terminal phase; the digest hands the EM a runnable terminal_commit line."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import wake_digest as wd
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW
from .test_ask_compose import _compose as _compose_ask
from .test_emit import _wave_row
from .test_wake_digest_contract import _kwargs

_BASE = "a" * 40


def _phases(script: str) -> list:
    block = script.split("phases: [", 1)[1].split("]", 1)[0]
    return re.findall(r"'([^']*)'", block)


def _test_prompt(script: str) -> str:
    start = script.index("phase('Scoped test run')")
    return script[start : script.index("test:terminal", start)]


@pytest.fixture
def script(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    tsc = tmp_path / "node_modules" / ".bin" / "tsc"
    tsc.parent.mkdir(parents=True)
    tsc.write_text("", encoding="utf-8")
    waves = [[_wave_row("C1", ["src/a.ts"])]]
    return compose_script(
        waves, name="wf", description="ts", repo_root=tmp_path, run_base_sha=_BASE, **REVIEW_KW
    )


def test_terminal_test_phase_runs_after_every_review_stage(script):
    phases = _phases(script)
    phase_at = phases.index("Scoped test run")

    review_stages = [p for p in phases if p.startswith("Review")]
    assert review_stages, phases
    assert all(phases.index(p) < phase_at for p in review_stages)
    test_at = script.index("phase('Scoped test run')")
    assert script.index("phase('Review wave')") < script.index("phase('Review integration')") < test_at


def test_terminal_test_prompt_covers_review_edited_files_beside_the_row_writes(script):
    prompt = _test_prompt(script)

    assert f"git diff --name-only {_BASE}" in prompt
    assert "git ls-files --others --exclude-standard" in prompt
    assert "Review stages ran before this phase" in prompt
    assert "tsc --noEmit -p <that dir>" in prompt
    assert "node_modules/.bin/tsc --noEmit -p ." in prompt


def test_review_edit_scope_falls_back_to_head_without_a_run_base():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]]
    script = compose_script(waves, name="wf", description="py", **REVIEW_KW)

    assert "git diff --name-only HEAD" in _test_prompt(script)


def test_ask_route_terminal_test_covers_review_edits_too():
    ask = _compose_ask()

    assert ask.index("phase('review')") < ask.index("phase('Scoped test run')")
    assert "git diff --name-only" in ask and "Review stages ran before this phase" in ask


# --- terminal_commit_cli ----------------------------------------------------


def _digest(tmp_path, *, inline_note: str = "") -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable to execute the generated script")
    js = wd.completion_return_js(**_kwargs(script_path="wf.mjs", session_id="s" * 8, anchor_plan_path="p.md"))
    harness = tmp_path / "cli.js"
    harness.write_text(
        "let _halted = null, _incompleteChunks = [], _unansweredBriefs = [], _notStarted = [], "
        "_blockedChunks = [], _stoppedBy = [];\n"
        "let _testResult = {status:'pass', tests_run:5, tests_failed:0, build_clean:true, "
        "summary:'ok', sidecar_path:'s.md'};\n"
        "let _verifications = [{chunk:'C1', status:'pass'}];\n"
        "let _falsifier = {status:'met', differs_from_baseline:true, observation:'ok', sidecar_path:'f.md'};\n"
        "let _reviewPrep = {slices:[1,2], sidecar_path:" + json.dumps(inline_note + "prep.md") + "};\n"
        "let _reviewWave = [{applied: 1, sidecar_path: " + json.dumps(inline_note + "w.md") + "}];\n"
        "let _deliveryVerdict = {verdict:'PASS', product_files:1, claims_unbacked:[]};\n"
        "let _reviewIntegration = {fixes_applied:2, em_may_think_differently:[], unresolved:[], "
        "overflow:0, brief_conformance:{items:1,met:1,unmet:0}, rebuild_decision:null, sidecar_path:'i.md'};\n"
        "console.log(JSON.stringify((function(){\n" + js + "\n})()));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(harness)], capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_digest_carries_a_terminal_commit_line_whose_params_equal_next_action_params(tmp_path):
    digest = _digest(tmp_path)

    argv = shlex.split(digest["terminal_commit_cli"])
    assert argv[:2] == ["coordinator-invoke", "dispatch.terminal_commit"]
    assert len(argv) == 3
    params = json.loads(argv[2])
    assert params == digest["next_action"]["params"]
    assert params["inline_review"]["integration_stem"]
    assert wd.validate_digest(digest) == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_apostrophe_in_the_params_survives_the_shell_quoting(tmp_path):
    digest = _digest(tmp_path, inline_note="it's ")

    argv = shlex.split(digest["terminal_commit_cli"])

    assert json.loads(argv[2]) == digest["next_action"]["params"]
    assert "it's w.md" in argv[2]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_params_too_large_for_argv_go_through_a_stdin_heredoc(tmp_path):
    digest = _digest(tmp_path, inline_note="x" * (wd.TERMINAL_COMMIT_CLI_ARGV_MAX + 1))

    head, _, rest = digest["terminal_commit_cli"].partition("\n")
    body, _, tail = rest.rpartition("\n")

    assert head == "coordinator-invoke dispatch.terminal_commit --params-file - <<'JSON'"
    assert tail == "JSON"
    assert json.loads(body) == digest["next_action"]["params"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_digest_with_no_next_action_params_has_a_null_line(tmp_path):
    js = wd.completion_return_js(**_kwargs(review_vars=None, has_commit_request=False, skipped_rows=[]))
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable to execute the generated script")
    harness = tmp_path / "null.js"
    harness.write_text(
        "let _halted = null, _incompleteChunks = [], _unansweredBriefs = [], _notStarted = [], "
        "_blockedChunks = [], _stoppedBy = [], _verifications = [], _testResult = null, _falsifier = null;\n"
        "console.log(JSON.stringify((function(){\n" + js + "\n})()));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(harness)], capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    digest = json.loads(proc.stdout)

    assert digest["next_action"]["params"] is None and digest["terminal_commit_cli"] is None


def test_the_ask_route_return_carries_the_line_too():
    ask = _compose_ask()

    assert "get terminal_commit_cli()" in ask and "function _terminalCommitCli(" in ask

