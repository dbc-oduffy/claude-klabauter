"""The composed ask script adopts a stage relay only when it is whole, with no read leg behind it.

The fold segment of the composed script runs under node with the relay adopted through the real
`_adoptManifest`; any agent() the segment reaches is a failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ROW = {
    "id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
    "writes": ["a.py"], "wave": 0, "deps": [],
}
_SHA = "a" * 40
_WHOLE = {"run_dir": "d", "plan_id": "p", "rows": [_ROW], "review_declared_paths": ["a.py"],
          "test_targets": ["t.py"], "marker_path": "m", "head_sha": _SHA}
#: What a trimming relay hands back: rows without brief_path, no review_declared_paths.
_TRIMMED_RELAY = {"rows": [{"id": "C1", "wave": 0, "deps": []}]}


def _fold(relay) -> dict:
    """Run the fold segment: adopt `relay`, return its outcome and every agent label it reached."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose()
    start = script.index("  let _stageHalt")
    end = script.index("\n", script.index("'stage staged no rows'"))
    program = (
        "const _seen = [];\n"
        "const agent = async (_p, opts) => { _seen.push(opts.label); throw new Error('unstubbed ' + opts.label); };\n"
        "const _agentDefsClause = () => '';\n"
        "(async () => {\n"
        "let _manifest = null, _stagedManifest = null, _halted = null, _allLanded = false, _gate = { arm: 'm' };\n"
        "let _incompleteChunks = [], _crossRepoRows = [], _sizingRel = null, _planRel = null, _runId = 'run-1';\n"
        + ask_compose._ADMIT_MANIFEST_JS
        + f"\n_adoptManifest(JSON.parse(process.env.RELAY));\n{script[start:end]}\n"
        "console.log(JSON.stringify({ seen: _seen, manifest: _manifest, halted: _halted }));\n"
        "})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });\n"
    )
    proc = subprocess.run(
        [node, "-"], input=program, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "RELAY": json.dumps(relay)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_whole_relay_is_adopted_with_no_agent_behind_it():
    out = _fold(_WHOLE)
    assert out["halted"] is None
    assert out["manifest"] == _WHOLE
    assert out["seen"] == []


def test_a_trimmed_relay_halts_naming_the_missing_field():
    out = _fold(_TRIMMED_RELAY)
    assert out["seen"] == []
    assert out["halted"]["kind"] == "refusal"
    assert out["halted"]["reason"] == "stage manifest relay is not whole: review_declared_paths"


def test_a_relay_without_a_head_sha_halts_naming_it():
    out = _fold({k: v for k, v in _WHOLE.items() if k != "head_sha"})
    assert out["halted"]["reason"] == "stage manifest relay is not whole: head_sha"


def test_a_relay_with_a_malformed_head_sha_halts_naming_it():
    out = _fold({**_WHOLE, "head_sha": "HEAD"})
    assert out["halted"]["reason"] == "stage manifest relay is not whole: head_sha"


def test_a_relayed_stage_error_halts_without_a_read():
    out = _fold({"error": "boom"})
    assert out["seen"] == []
    assert out["halted"]["reason"] == "stage refused: boom"


def test_no_relay_at_all_keeps_the_no_folded_manifest_halt():
    out = _fold(None)
    assert out["seen"] == []
    assert out["halted"]["reason"].startswith("no folded stage manifest")
