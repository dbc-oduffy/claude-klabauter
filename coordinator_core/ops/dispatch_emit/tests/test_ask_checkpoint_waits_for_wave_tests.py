"""An ask checkpoint waits on its wave's `verify:wave-<n>` verdict: red or timeout is recorded and never blocks the commit; the runner is deadline-bounded."""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.win_portability import no_console_creationflags

from .test_ask_compose_wave_barrier import _script

_KW = dict(repo_root=None, session_id=None, plan_path="docs/plans/p.md", run_base_sha="a" * 40)
_GATE = ask_compose._WAVE_TESTS_GATE_FN


def _checkpoint_js(**over) -> str:
    return emit._checkpoint_commit_js(None, **{**_KW, **over})


def test_no_gate_emits_the_machinery_byte_identically():
    assert _checkpoint_js() == _checkpoint_js(pre_commit_gate_js=None)
    assert _GATE not in _checkpoint_js()


def test_emit_route_passes_only_the_owed_gates_gate():
    source = inspect.getsource(emit.compose_script)
    assert "pre_commit_gate_js=_OWED_GATES_GATE_FN" in source
    assert "_WAVE_TESTS_GATE_FN" not in source
    assert emit._OWED_GATES_GATE_FN != _GATE


def test_gate_sits_after_paths_and_before_the_commit_agent():
    js = _checkpoint_js(pre_commit_gate_js=_GATE)
    assert js.index("const paths = ") < js.index(f"await {_GATE}(n, done)") < js.index("label: 'commit:wave-'")
    assert js.count(_GATE) == 1


def test_ask_script_wires_the_gate_and_keeps_the_pinned_call_line():
    script = _script()
    assert f"await {_GATE}(n, done)" in script
    assert "label: 'verify:wave-' + n" in script
    assert "if (w !== _waves[_waves.length - 1]) await _commitWave(w, _waveIds);" in script


def test_since_halt_credit_reads_only_checkpoint_shas():
    script = _script()
    assert "..._checkpointShas];" in script
    assert script.count("_checkpointWithheld") == 2


def test_manifest_schema_admits_wave_test_targets():
    prop = ask_compose._MANIFEST_SCHEMA["properties"]["wave_test_targets"]
    assert prop == {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}}


def _run(
    waves: list[dict], verdicts: dict, targets: dict, extra: str = "", terminal: bool = False,
    landed: dict | None = None,
) -> dict:
    """Run each wave through the composed `_commitWave` with stubbed legs; `verdicts` maps wave -> verify reply."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    machinery = _checkpoint_js(pre_commit_gate_js=_GATE, runtime_plan_js="'p.md'", reds_var=ask_compose.WAVE_REDS_VAR)
    gate = ask_compose._wave_tests_green_js(None, repo_root=None, run_id="r1")
    source = f"""
let _halted = null; let _terminalTestsWanted = {json.dumps(terminal)}; const _runId = 'r1'; const _repoRoot = '/r'; const _runBase = 'b';
const _gateOwed = {{}}; const _stoppedBy = []; const _rows = {{}}; const _ROW_VERIFY_SCHEMA = {{}};
const _seamRelease = {{}}; const _seamHeldPlans = {{}}; const _seamGates = {{}};
const _manifest = {{ wave_test_targets: {json.dumps(targets)} }};
const _landedPaths = {json.dumps(landed or {})};
const _verdicts = {json.dumps(verdicts)}; const spawned = []; const prompts = {{}};
const log = () => {{}};
async function agent(prompt, opts) {{
  spawned.push(opts.label); prompts[opts.label] = prompt;
  if (opts.label.startsWith('verify:wave-')) {{
    const v = _verdicts[opts.label.slice('verify:wave-'.length)];
    if (v === 'throw') throw new Error('boom');
    if (v === 'limit') throw new Error('usage limit reached');
    return v;
  }}
  const reds = prompt.includes('dispatch.checkpoint_reds');
  return {{ outcome: 'committed', sha: 'sha-' + opts.label, ...(reds ? {{ fix_ids: ['fx-' + opts.label] }} : {{}}) }};
}}
{ask_compose._usage_limit_helper_js()}
{machinery}
{gate}
(async () => {{
  const waves = {json.dumps(waves)};
  for (const w of waves) {{
    for (const id of w.ids) _landed[id] = {{ paths: _landedPaths[id] ?? [id + '.py'], declared: _landedPaths[id] ?? [id + '.py'] }};
    await _commitWave(w.n, w.ids);
  }}
  {extra}
  console.log(JSON.stringify({{ spawned, shas: _checkpointShas, carried: _carried, withheld: _checkpointWithheld,
    failures: _commitFailures, halted: _halted, prompts, fixIds: _checkpointFixIds }}));
}})();
"""
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _verdict(status: str, sidecar: str | None = "s.md") -> dict:
    out = {"chunk": "wave", "status": status, "tests_run": 3, "tests_failed": 0 if status != "fail" else 1,
           "typecheck_clean": None}
    if sidecar:
        out["sidecar_path"] = sidecar
    return out


_W0 = [{"n": 0, "ids": ["C0"]}]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("reply", [_verdict("fail"), _verdict("error"), _verdict("pass", None), None, "throw"])
def test_red_or_unproven_verdict_is_recorded_but_never_blocks_the_commit(reply):
    out = _run(_W0, {"0": reply}, {"0": ["t_a.py"]})
    assert out["spawned"] == ["verify:wave-0", "commit:wave-0"]
    assert out["shas"] == ["sha-commit:wave-0"]
    assert out["carried"] == []
    assert [w["wave"] for w in out["withheld"]] == [0]
    assert out["failures"] == [] and out["halted"] is None
    assert out["fixIds"] == ["fx-commit:wave-0"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_red_wave_commit_prompt_lists_each_failing_id_and_names_the_checkpoint_reds_op():
    reply = {**_verdict("fail"), "failing_ids": ["t_a.py::test_one", "t_b.py::test_two"]}
    prompt = _run(_W0, {"0": reply}, {"0": ["t_a.py"]})["prompts"]["commit:wave-0"]
    assert "dispatch.checkpoint_reds" in prompt
    assert '"failing":["t_a.py::test_one","t_b.py::test_two"]' in prompt
    assert '"base_red":[]' in prompt and '"run_id":"r1"' in prompt
    assert "fix_paths" in prompt and "commit_block" in prompt


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_red_verdict_without_failing_ids_lists_the_wave_as_one_unnamed_red():
    prompt = _run(_W0, {"0": _verdict("fail")}, {"0": ["t_a.py"]})["prompts"]["commit:wave-0"]
    assert '"failing":["<unnamed wave 0>"]' in prompt


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_green_wave_commit_prompt_never_names_the_checkpoint_reds_op():
    out = _run(_W0, {"0": _verdict("pass")}, {"0": ["t_a.py"]})
    assert "dispatch.checkpoint_reds" not in out["prompts"]["commit:wave-0"]
    assert out["fixIds"] == []


def test_default_composed_ask_script_wires_reds_and_surfaces_fix_ids_in_the_result():
    script = _script()
    assert "dispatch.checkpoint_reds" in script
    assert "checkpoint_fix_ids: _checkpointFixIds" in script
    assert _checkpoint_js().count("checkpoint_reds") == 0


def test_wave_verify_prompts_carry_the_deadline_and_the_no_investigate_hang_report():
    clause = emit._wave_verify_bound_clause()
    assert emit._verify_deadline_clause() in clause
    assert "timeout:" in clause and "node id" in clause and "never investigate" in clause
    ask = ask_compose._wave_tests_green_js(None, repo_root=None, run_id="r1")
    owed = emit._owed_gates_green_js(None, repo_root=None, owed_scope={"C0": ["t.py"]})
    for js in (ask, owed):
        assert emit._js_string_literal(emit._test_runner_clause(None) + clause) in js
    assert "return false" not in owed.split("const status")[1]
    assert ask.split("const status")[1].count("return false") == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("status", ["pass", "pass-with-skips"])
def test_green_verdict_spawns_exactly_one_commit_leg(status):
    out = _run(_W0, {"0": _verdict(status)}, {"0": ["t_a.py"]})
    assert out["spawned"] == ["verify:wave-0", "commit:wave-0"]
    assert out["shas"] == ["sha-commit:wave-0"]
    assert out["carried"] == [] and out["withheld"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_terminal_tests_running_skips_the_wave_verify_leg_for_a_wave_adding_no_test_file():
    out = _run(_W0, {"0": _verdict("fail")}, {"0": ["t_a.py"]}, terminal=True)
    assert out["spawned"] == ["commit:wave-0"]
    assert out["shas"] == ["sha-commit:wave-0"]
    assert out["withheld"] == [] and out["halted"] is None


_ADDS_TEST = {"C0": ["src/mod.py", "tests/test_mod.py"]}
_RED_OWN = {**_verdict("fail"), "failing_ids": ["tests/test_mod.py::test_one"]}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_terminal_tests_wave_adding_a_red_test_file_is_withheld_not_committed():
    out = _run(_W0, {"0": _RED_OWN}, {"0": ["tests/test_mod.py"]}, terminal=True, landed=_ADDS_TEST)
    assert out["spawned"] == ["verify:wave-0"]
    assert out["shas"] == [] and out["carried"] == ["C0"]
    assert [w["wave"] for w in out["withheld"]] == [0]
    assert out["failures"] == [] and out["halted"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_terminal_tests_wave_adding_a_green_test_file_commits_it():
    out = _run(_W0, {"0": _verdict("pass")}, {"0": ["tests/test_mod.py"]}, terminal=True, landed=_ADDS_TEST)
    assert out["spawned"] == ["verify:wave-0", "commit:wave-0"]
    assert out["shas"] == ["sha-commit:wave-0"] and out["carried"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_terminal_tests_red_elsewhere_does_not_withhold_a_wave_whose_added_test_is_not_failing():
    reply = {**_verdict("fail"), "failing_ids": ["tests/test_other.py::test_x"]}
    out = _run(_W0, {"0": reply}, {"0": ["tests/test_mod.py"]}, terminal=True, landed=_ADDS_TEST)
    assert out["spawned"] == ["verify:wave-0", "commit:wave-0"]
    assert out["carried"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_withheld_falsifier_lands_with_the_later_wave_that_turns_it_green():
    waves = [{"n": 0, "ids": ["C0"]}, {"n": 1, "ids": ["C1"]}]
    targets = {"0": ["tests/test_mod.py"], "1": ["tests/test_mod.py"]}
    out = _run(
        waves, {"0": _RED_OWN, "1": _verdict("pass")}, targets, terminal=True,
        landed={**_ADDS_TEST, "C1": ["src/fix.py"]},
    )
    assert out["spawned"] == ["verify:wave-0", "verify:wave-1", "commit:wave-1"]
    assert out["carried"] == [] and out["shas"] == ["sha-commit:wave-1"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_wave_without_declared_targets_commits_with_no_verify_leg():
    out = _run(_W0, {}, {})
    assert out["spawned"] == ["commit:wave-0"]
    assert out["shas"] == ["sha-commit:wave-0"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_later_wave_commits_both_waves_paths_only_after_its_cumulative_scope_passes():
    waves = [{"n": 0, "ids": ["C0"]}, {"n": 1, "ids": ["C1"]}]
    targets = {"0": ["t0.py"], "1": ["t0.py", "t1.py"]}
    out = _run(waves, {"0": _verdict("fail"), "1": _verdict("pass")}, targets)
    assert out["spawned"] == ["verify:wave-0", "commit:wave-0", "verify:wave-1", "commit:wave-1"]
    assert out["shas"] == ["sha-commit:wave-0", "sha-commit:wave-1"]
    assert out["carried"] == []
    assert [w["wave"] for w in out["withheld"]] == [0]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_usage_limit_throw_halts_resumably_instead_of_withholding_silently():
    out = _run(_W0, {"0": "limit"}, {"0": ["t.py"]})
    assert out["halted"]["halted"] == ask_compose.HALT_USAGE_LIMIT
    assert out["shas"] == [] and out["carried"] == ["C0"]
