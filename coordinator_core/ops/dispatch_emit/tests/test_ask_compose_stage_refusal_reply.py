"""A ready entry whose readyOpReply carries `error` and no manifest_handle halts as a stage refusal with that
text and its owed ids; error-with-handle and error-less handle-less replies still halt as not whole."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_plan_blitz_ready_restage import (  # noqa: F401
    _PLAN,
    _DISK,
    _labels,
    _run,
    _script,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    reader_file,
    readers_cfg,
    stage_reply,
    stage_root,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ERR = "design-review: route canvas, board docs/design/people-design/project/; the PM has not approved the canvas board"


def test_an_error_only_readyop_reply_halts_as_stage_refused_with_the_error(monkeypatch):
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpReply": {"error": _ERR}}))
    halt = out["result"]["halted_by"]
    assert halt["reason"] == "stage refused: " + _ERR
    assert "blitz-stage-read" not in _labels(out)


def test_an_error_only_readyop_reply_keeps_its_owed_ids(monkeypatch):
    reply = {"error": "boom", "owed_em_ids": ["C9"], "owed_plan": _PLAN}
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpReply": reply}))
    halt = out["result"]["halted_by"]
    assert halt["kind"] == "refusal"
    assert halt["reason"].startswith("stage refused: boom")
    assert "C9" in str(halt)


def test_an_error_with_a_handle_still_halts_as_not_whole(monkeypatch):
    reply = {**stage_reply(_DISK), "error": _ERR}
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpReply": reply}))
    assert out["result"]["halted_by"]["reason"].startswith("stage manifest relay is not whole")


def test_a_handleless_errorless_reply_still_halts_as_not_whole(monkeypatch):
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpReply": {"rows": []}}))
    assert out["result"]["halted_by"]["reason"] == "stage manifest relay is not whole: manifest_handle"


_H = {"path": "state/warp/r/manifest.json", "sha256": "0a" * 32, "bytes": 11}
_TWIN = "scratch/warp/r.workflow.mjs"


def _adopt(reply, recovered, *, wired=True) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    wire = (
        f"  let _stageRecover = async () => {{ _stageRecoverCalls++; return {{ stage_reply: {json.dumps(recovered)} }}; }};\n"
        if wired else "  let _stageRecover = null;\n"
    )
    code = (
        "(async () => {\n"
        "let _stagedManifest = null, _stageRefusal = null, _relayGap = null, _parked = null, _diskHalt = null;\n"
        "let _stageRecoverCalls = 0;\n"
        "const _adoptStagedInline = () => null; const _wholeManifestGap = () => null;\n"
        + ask_compose._ADOPT_STAGE_REPLY_HEAD_JS.replace("  let _stageRecover = null;\n", wire, 1)
        + ask_compose._ADOPT_STAGE_REPLY_LAUNCHER_JS
        + ask_compose._ADOPT_STAGE_REPLY_TAIL_JS
        + f"\n  await _adoptStageReply({json.dumps(reply)});\n"
        "  console.log(JSON.stringify({ pending: _stagePending, gap: _relayGap, refusal: _stageRefusal, "
        "calls: _stageRecoverCalls }));\n"
        "})();\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_handleless_errorless_reply_is_reread_once_through_stage_recover():
    got = _adopt({"rows": []}, {"manifest_handle": _H, "twin_path": _TWIN})
    assert got["calls"] == 1
    assert got["gap"] is None
    assert got["pending"] == {"handle": _H, "twin_path": _TWIN}


def test_a_handleless_errorless_reply_with_no_recover_leg_still_halts_not_whole():
    got = _adopt({"rows": []}, None, wired=False)
    assert got["calls"] == 0
    assert got["gap"] == "manifest_handle"


def test_a_reply_carrying_an_error_is_never_reread():
    got = _adopt({"error": "boom"}, {"manifest_handle": _H, "twin_path": _TWIN})
    assert got["calls"] == 0
    assert got["refusal"] == "boom"
