"""The composed ask script adopts the stage manifest from the disk read leg, not the trimmed relay.

The fold segment of the composed script runs under node with the relay adopted through the real
`_adoptManifest` and the `manifest-read` leg stubbed by label.
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
_DISK = {"run_dir": "d", "plan_id": "p", "rows": [_ROW], "review_declared_paths": ["a.py"],
         "test_targets": ["t.py"], "marker_path": "m"}
#: What the plan-blitz ready relay hands back: rows without brief_path, no review_declared_paths.
_TRIMMED_RELAY = {"rows": [{"id": "C1", "wave": 0, "deps": []}]}
_MANIFEST_REL = "state/warp/run-1/manifest.json"


def _fold(relay, disk) -> dict:
    """Run the fold segment: adopt `relay`, answer the manifest-read leg with `disk`, return its outcome."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose()
    start = script.index("  let _stageHalt")
    end = script.index("\n", script.index("'stage staged no rows'"))
    program = (
        "const _seen = [];\n"
        "const _DISK = JSON.parse(process.env.DISK);\n"
        "const agent = async (_p, opts) => { _seen.push(opts.label); if (opts.label === 'manifest-read') return _DISK;"
        " throw new Error('unstubbed ' + opts.label); };\n"
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
        env={**os.environ, "RELAY": json.dumps(relay), "DISK": json.dumps(disk)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_trimmed_relay_with_a_whole_disk_manifest_proceeds_on_the_disk_rows():
    out = _fold(_TRIMMED_RELAY, _DISK)
    assert out["halted"] is None
    assert out["manifest"] == _DISK
    assert out["seen"] == ["manifest-read"]


def test_an_absent_disk_manifest_halts_naming_the_path():
    out = _fold(_TRIMMED_RELAY, {"error": "no such file"})
    assert out["halted"]["kind"] == "refusal"
    assert out["halted"]["reason"] == f"no stage manifest on disk: {_MANIFEST_REL}"


def test_a_disk_manifest_missing_review_declared_paths_halts_naming_the_field():
    disk = {k: v for k, v in _DISK.items() if k != "review_declared_paths"}
    out = _fold(_TRIMMED_RELAY, disk)
    assert out["halted"]["kind"] == "refusal"
    assert out["halted"]["reason"] == "stage manifest on disk is not whole: review_declared_paths"


def test_a_relayed_stage_error_halts_without_a_disk_read():
    out = _fold({"error": "boom"}, _DISK)
    assert out["seen"] == []
    assert out["halted"]["reason"] == "stage refused: boom"


def test_no_relay_at_all_keeps_the_no_folded_manifest_halt():
    out = _fold(None, _DISK)
    assert out["seen"] == []
    assert out["halted"]["reason"].startswith("no folded stage manifest")
