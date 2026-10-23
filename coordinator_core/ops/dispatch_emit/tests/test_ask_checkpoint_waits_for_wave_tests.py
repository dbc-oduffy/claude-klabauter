"""An ask checkpoint waits on its wave's `verify:wave-<n>` verdict: red withholds and carries; the emit route is unchanged."""

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


def test_emit_route_passes_no_gate():
    assert "pre_commit_gate_js" not in inspect.getsource(emit.compose_script)


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


def _run(waves: list[dict], verdicts: dict, targets: dict, extra: str = "") -> dict:
    """Run each wave through the composed `_commitWave` with stubbed legs; `verdicts` maps wave -> verify reply."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    machinery = _checkpoint_js(pre_commit_gate_js=_GATE, runtime_plan_js="'p.md'")
    gate = ask_compose._wave_tests_green_js(None, repo_root=None, run_id="r1")
    source = f"""
let _halted = null; const _runId = 'r1'; const _repoRoot = '/r'; const _runBase = 'b';
const _gateOwed = {{}}; const _stoppedBy = []; const _rows = {{}}; const _ROW_VERIFY_SCHEMA = {{}};
const _seamRelease = {{}}; const _seamHeldPlans = {{}}; const _seamGates = {{}};
const _manifest = {{ wave_test_targets: {json.dumps(targets)} }};
const _verdicts = {json.dumps(verdicts)}; const spawned = [];
const log = () => {{}};
async function agent(prompt, opts) {{
  spawned.push(opts.label);
  if (opts.label.startsWith('verify:wave-')) {{
    const v = _verdicts[opts.label.slice('verify:wave-'.length)];
    if (v === 'throw') throw new Error('boom');
    if (v === 'limit') throw new Error('usage limit reached');
    return v;
  }}
  return {{ outcome: 'committed', sha: 'sha-' + opts.label }};
}}
{ask_compose._usage_limit_helper_js()}
{machinery}
{gate}
(async () => {{
  const waves = {json.dumps(waves)};
  for (const w of waves) {{
    for (const id of w.ids) _landed[id] = {{ paths: [id + '.py'], declared: [id + '.py'] }};
    await _commitWave(w.n, w.ids);
  }}
  {extra}
  console.log(JSON.stringify({{ spawned, shas: _checkpointShas, carried: _carried, withheld: _checkpointWithheld,
    failures: _commitFailures, halted: _halted }}));
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
def test_red_or_unproven_verdict_withholds_carries_and_records_no_sha(reply):
    out = _run(_W0, {"0": reply}, {"0": ["t_a.py"]})
    assert out["spawned"] == ["verify:wave-0"]
    assert out["shas"] == []
    assert out["carried"] == ["C0"]
    assert [w["wave"] for w in out["withheld"]] == [0]
    assert out["failures"] == [] and out["halted"] is None


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
    assert out["spawned"] == ["verify:wave-0", "verify:wave-1", "commit:wave-1"]
    assert out["shas"] == ["sha-commit:wave-1"]
    assert out["carried"] == []
    assert [w["wave"] for w in out["withheld"]] == [0]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_usage_limit_throw_halts_resumably_instead_of_withholding_silently():
    out = _run(_W0, {"0": "limit"}, {"0": ["t.py"]})
    assert out["halted"]["halted"] == ask_compose.HALT_USAGE_LIMIT
    assert out["shas"] == [] and out["carried"] == ["C0"]
