"""reply_sidecar JS contract run under a real `node` child."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import reply_sidecar as rs
from coordinator_core.ops.dispatch_emit.tests.test_reply_sidecar import _manifest, _reader_file, sidecar  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _verify_js(tmp_path, text: str, handle: dict):
    script = tmp_path / "verify.js"
    script.write_text(
        rs.VERIFY_REPLY_JS + f"\nconsole.log(JSON.stringify(_verifyReplyText({json.dumps(text)}, {json.dumps(handle)})));\n",
        encoding="ascii",
    )
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


_NODE_PRELUDE = """
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
let agentCalls = 0;
const agent = () => { agentCalls++; throw new Error('agent called'); };
"""


def _node_json(tmp_path, source: str):
    script = tmp_path / "run.js"
    script.write_text(source, encoding="ascii")
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def _read_with(tmp_path, handle: dict, workflow_js: str):
    src = (
        _NODE_PRELUDE
        + rs.VERIFY_REPLY_JS
        + rs.READ_STAGE_SIDECAR_JS
        + f"\nconst workflow = {workflow_js};\n"
        + f"_readStageSidecar({json.dumps(handle)}, 'ROOT').then(r => console.log(JSON.stringify({{r: r, agentCalls: agentCalls}})));\n"
    )
    return _node_json(tmp_path, src)


def test_reindented_manifest_verifies_and_changed_value_refuses(sidecar, tmp_path):
    root, path, handle = sidecar
    text = rs.read_text(root, handle)
    reindented = json.dumps(json.loads(text), indent=4)
    changed = json.loads(text)
    changed["head_sha"] = "b" * 40
    assert rs.canonical_json(json.loads(reindented)).encode("ascii") == path.read_bytes()
    assert rs.canonical_json(changed) != text
    if shutil.which("node") is None:
        pytest.skip("node absent")
    assert _verify_js(tmp_path, text, handle) is None
    assert isinstance(_verify_js(tmp_path, reindented, handle), str)
    assert isinstance(_verify_js(tmp_path, json.dumps(changed, indent=4), handle), str)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_verify_reply_js_accepts_sidecar_and_refuses_damage(sidecar, tmp_path):
    root, _, handle = sidecar
    text = rs.read_text(root, handle)
    assert _verify_js(tmp_path, text, handle) is None
    flipped = text[:10] + chr(ord(text[10]) ^ 1) + text[11:]
    assert isinstance(_verify_js(tmp_path, flipped, handle), str)
    assert isinstance(_verify_js(tmp_path, text[:-50], handle), str)
    assert "not ASCII" in _verify_js(tmp_path, "café", {"sha256": "0" * 64, "bytes": 4})
    notjson = "abcd"
    assert "not valid JSON" in _verify_js(
        tmp_path, notjson, {"sha256": hashlib.sha256(notjson.encode()).hexdigest(), "bytes": 4}
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_reader_script_returns_the_sidecar_text(sidecar, tmp_path):
    root = tmp_path / "big"
    path = root / "state" / "warp" / "run1" / "big.json"
    handle = rs.write_reply(root, path, _manifest(150))
    text = path.read_text("ascii")
    assert len(text) > 3 * 4497
    src = (
        _NODE_PRELUDE
        + f"const fs = require('fs');\nconst body = fs.readFileSync({json.dumps(str(_reader_file(root, handle)))}, 'ascii').slice({len(rs.READER_META)});\n"
        + "new AsyncFunction(body)().then(v => console.log(JSON.stringify(v)));\n"
    )
    assert _node_json(tmp_path, src) == text


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_read_stage_sidecar_js_adopts_and_refuses_damage(sidecar, tmp_path):
    root, path, handle = sidecar
    body = json.dumps(_reader_file(root, handle).read_text("ascii")[len(rs.READER_META):])
    runner = (
        "async ({ scriptPath }) => { if (scriptPath !== 'ROOT/scratch/stage-readers/" + handle["sha256"] + ".js') "
        "throw new Error('bad path ' + scriptPath); return new AsyncFunction(%s)(); }"
    )
    ok = _read_with(tmp_path, handle, runner % body)
    assert ok["agentCalls"] == 0
    assert ok["r"] == {"manifest": _manifest()}

    text = path.read_text("ascii")
    flipped = text[:10] + chr(ord(text[10]) ^ 1) + text[11:]
    for bad in (flipped, text[:-5]):
        res = _read_with(tmp_path, handle, runner % json.dumps(rs.reader_script(bad)))
        assert "fail" in res["r"] and res["agentCalls"] == 0

    thrown = _read_with(tmp_path, handle, "async () => { throw new Error('no such file'); }")
    assert "fail" in thrown["r"] and handle["sha256"] in thrown["r"]["fail"] and thrown["agentCalls"] == 0

    for bad_handle in ({**handle, "sha256": "zz"}, {**handle, "bytes": -1}, {**handle, "bytes": "3"}, {**handle, "path": ""}):
        assert "fail" in _read_with(tmp_path, bad_handle, "async () => ''")["r"]
