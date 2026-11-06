"""A refire that restages a run under a new manifest sha starts the twin the stage op wrote, not a replayed one.

The replayed stage reply names the pre-restage twin (pruned on disk); the stage op's fresh reply names the
twin ensure_twin wrote, and the chain stage loop must start that file.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

_RUN = "ask-r1"
_OLD = "4f" * 32
_NEW = "0a" * 32
_H_OLD = {"path": "state/warp/r/manifest.json", "sha256": _OLD, "bytes": 10}
_H_NEW = {"path": "state/warp/r/manifest.json", "sha256": _NEW, "bytes": 11}


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    return node


def _node_json(code: str) -> dict:
    out = subprocess.run([_node(), "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _adopt(reply: dict, recovered: dict) -> dict:
    code = (
        "(async () => {\n"
        "let _stagedManifest = null, _stageRefusal = null, _relayGap = null, _parked = null, _diskHalt = null;\n"
        "let _stageRecoverCalls = 0;\n"
        "const _adoptStagedInline = () => null; const _wholeManifestGap = () => null;\n"
        + ask_compose._ADOPT_STAGE_REPLY_HEAD_JS
        + f"  _stageRecover = async () => {{ _stageRecoverCalls++; return {{ stage_reply: {json.dumps(recovered)} }}; }};\n"
        + ask_compose._ADOPT_STAGE_REPLY_LAUNCHER_JS
        + ask_compose._ADOPT_STAGE_REPLY_TAIL_JS
        + f"\n  await _adoptStageReply({json.dumps(reply)});\n"
        "  console.log(JSON.stringify({ pending: _stagePending, calls: _stageRecoverCalls }));\n"
        "})();\n"
    )
    return _node_json(code)


def _loop(pending: dict) -> dict:
    code = (
        "const REPO_ROOT = '/r';\nconst calls = [];\n"
        f"const PENDING = {json.dumps({'stage_pending': pending})};\n"
        "const workflow = async (o) => { calls.push(o.scriptPath); return { done: true }; };\n"
        f"(async () => {{\n  let _why = null; let _chained = PENDING; const _chainPath = '/r/scratch/warp/{_RUN}.workflow.mjs';\n"
        + ask_compose._chain_stage_loop_js()
        + "\n  console.log(JSON.stringify({ why: _why, calls }));\n})();\n"
    )
    return _node_json(code)


def test_a_replayed_reply_naming_a_pruned_twin_is_replaced_by_the_fresh_stage_op_twin():
    written = staged_twin_rel(_RUN, _NEW)
    got = _adopt({"manifest_handle": _H_OLD, "twin_path": staged_twin_rel(_RUN, _OLD)},
                 {"manifest_handle": _H_NEW, "twin_path": written})

    assert got["calls"] == 1
    assert got["pending"] == {"handle": _H_NEW, "twin_path": written}


def test_the_chain_loop_starts_the_twin_the_restage_wrote_end_to_end():
    written = staged_twin_rel(_RUN, _NEW)
    adopted = _adopt({"manifest_handle": _H_OLD, "twin_path": staged_twin_rel(_RUN, _OLD)},
                     {"manifest_handle": _H_NEW, "twin_path": written})

    got = _loop(adopted["pending"])

    assert got["why"] is None
    assert got["calls"] == ["/r/" + written]


def test_a_recover_reply_naming_no_twin_leaves_the_replayed_reply():
    replayed = staged_twin_rel(_RUN, _OLD)
    got = _adopt({"manifest_handle": _H_OLD, "twin_path": replayed}, {"manifest_handle": _H_NEW})

    assert got["calls"] == 1
    assert got["pending"] == {"handle": _H_OLD, "twin_path": replayed}
