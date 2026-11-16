"""The checkpoint commit leg carries each withheld path's reason from the commit reply to the run log."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.complete

_KW = dict(repo_root=None, session_id=None, plan_path="docs/plans/p.md", run_base_sha="a" * 40)


def test_schema_and_prompt_carry_withheld_reasons():
    props = emit._CHECKPOINT_COMMIT_SCHEMA["properties"]
    assert props["withheld_reasons"] == {"type": "object", "additionalProperties": {"type": "string"}}
    assert "withheld_reasons" not in emit._CHECKPOINT_COMMIT_SCHEMA["required"]
    js = emit._checkpoint_commit_js(None, **_KW)
    assert "`checkpoint_withheld_reasons` verbatim into `withheld_reasons`" in js


def _run(reply: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    machinery = emit._checkpoint_commit_js(None, **_KW)
    source = f"""
const _runId = 'r1'; const _repoRoot = '/r'; const _runBase = 'b';
const _gateOwed = {{}}; const _stoppedBy = []; const _rows = {{}};
const logs = [];
const log = (m) => logs.push(m);
async function agent() {{ return {json.dumps(reply)}; }}
{machinery}
(async () => {{
  _landed.C1 = {{ paths: ['a/x.json', 'b/y.py'], declared: ['a/x.json', 'b/y.py'] }};
  await _commitWave(0, ['C1']);
  console.log(JSON.stringify({{ named: _heldPathsNamed, carried: _carried, logs }}));
}})();
"""
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_each_withheld_path_is_named_with_its_reason():
    out = _run({"outcome": "committed", "sha": "s1", "withheld_paths": ["a/x.json"],
                "withheld_reasons": {"a/x.json": "foreign hunk"}})
    assert out["named"] == [{"wave": 0, "path": "a/x.json", "reason": "foreign hunk"}]
    assert out["carried"] == ["C1"]
    assert any("a/x.json (foreign hunk)" in m for m in out["logs"])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_withheld_path_without_a_reason_is_still_named_and_nothing_withheld_logs_nothing():
    out = _run({"outcome": "committed", "sha": "s1", "withheld_paths": ["b/y.py"]})
    assert out["named"] == [{"wave": 0, "path": "b/y.py", "reason": "no reason returned"}]
    clean = _run({"outcome": "committed", "sha": "s2"})
    assert clean["named"] == [] and clean["logs"] == [] and clean["carried"] == []


def _fold(halted_js: str, named: list, declare: bool = True) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    source = f"""
let _halted = {halted_js};
{"const _heldPathsNamed = " + json.dumps(named) + ";" if declare else ""}
{emit._HALT_NAMES_WITHHELD_JS}
console.log(JSON.stringify(_halted));
"""
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_NAMED = [{"wave": 0, "path": "a/x.json", "reason": "foreign hunk"}]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_halt_lists_each_withheld_path_with_its_reason():
    assert _fold("'C2'", _NAMED) == "C2 [checkpoint withheld: a/x.json (foreign hunk)]"
    typed = _fold("{ halted: 'commit', reason: 'r' }", _NAMED)
    assert typed["checkpoint_withheld"] == {"a/x.json": "foreign hunk"}
    assert typed["reason"] == "r [checkpoint withheld: a/x.json (foreign hunk)]"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_halt_or_nothing_withheld_leaves_the_halt_untouched():
    assert _fold("null", _NAMED) is None
    assert _fold("'C2'", []) == "C2"
    assert _fold("'C2'", [], declare=False) == "C2"
