"""Tests for the reply sidecar handle contract."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import jsonschema
import pytest

from coordinator_core.ops.dispatch_emit import grind_commit_contract as gcc
from coordinator_core.ops.dispatch_emit import reply_sidecar as rs


def _manifest(rows: int = 40) -> dict:
    return {
        "rows": [{"id": f"C{i}", "title": f"row {i} caf\u00e9 " + "x" * 80, "writes": [f"src/f{i}.py"]} for i in range(rows)],
        "review_declared_paths": ["a.py", "b.py"],
        "marker_path": "state/warp/run1/marker",
        "test_targets": ["tests/test_a.py"],
        "head_sha": "a" * 40,
    }


@pytest.fixture
def sidecar(tmp_path):
    path = tmp_path / "state" / "warp" / "run1" / "manifest.json"
    handle = rs.write_reply(tmp_path, path, _manifest())
    return tmp_path, path, handle


def test_forty_row_payload_round_trips_with_tail_fields(sidecar):
    root, _, handle = sidecar
    assert set(handle) == set(rs.HANDLE_KEYS)
    assert handle["path"] == "state/warp/run1/manifest.json"
    assert rs.read_reply(root, handle) == _manifest()
    assert len(rs.read_reply(root, handle)["rows"]) == 40


def test_sidecar_is_canonical_ascii_json(sidecar):
    _, path, handle = sidecar
    raw = path.read_bytes()
    assert raw.isascii() and raw.endswith(b"}") and b"\n" not in raw
    assert raw.decode("ascii") == rs.canonical_json(_manifest())
    assert handle["bytes"] == len(raw)


def test_reindented_content_canonicalizes_to_the_written_text(sidecar):
    _, path, _ = sidecar
    m = {"z": {"b": 1, "a": ["café", " ", "q\"\\"]}, "a": None, "n": "中"}
    reindented = json.dumps(m, indent=4)
    assert rs.canonical_json(json.loads(reindented)) == rs.canonical_json(m)
    assert rs.canonical_json(json.loads(json.dumps(_manifest(), indent=4))) == path.read_text("ascii")


@pytest.mark.spawns_process
@pytest.mark.cadence
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


def test_handle_for_matches_write_handle(sidecar):
    root, path, handle = sidecar
    assert rs.handle_for(root, path) == handle


def test_no_tmp_file_left_behind(sidecar):
    _, path, _ = sidecar
    assert [p.name for p in path.parent.iterdir()] == ["manifest.json"]


def test_one_flipped_byte_is_refused(sidecar):
    root, path, handle = sidecar
    raw = bytearray(path.read_bytes())
    raw[10] ^= 0x01
    path.write_bytes(bytes(raw))
    with pytest.raises(rs.ReplySidecarError) as exc:
        rs.read_reply(root, handle)
    assert str(exc.value).startswith(gcc.RELAY_CORRUPT_HEAD)
    assert rs.SIDECAR_CAUSE in str(exc.value)


def test_truncated_sidecar_is_refused(sidecar):
    root, path, handle = sidecar
    path.write_bytes(path.read_bytes()[:-50])
    with pytest.raises(rs.ReplySidecarError, match=gcc.RELAY_CORRUPT_HEAD):
        rs.read_text(root, handle)


def test_wrong_bytes_value_is_refused(sidecar):
    root, _, handle = sidecar
    with pytest.raises(rs.ReplySidecarError, match=gcc.RELAY_CORRUPT_HEAD):
        rs.read_text(root, {**handle, "bytes": handle["bytes"] + 1})


def test_non_ascii_sidecar_is_refused(sidecar):
    root, path, handle = sidecar
    path.write_bytes("caf\u00e9".encode("utf-8"))
    with pytest.raises(rs.ReplySidecarError, match="not ASCII"):
        rs.read_text(root, rs.handle_for(root, path))


@pytest.mark.parametrize(
    "bad",
    ["../outside.json", "state/warp/../../etc/x", "/abs/x.json", "C" + ":/x.json", "state\\warp\\x", "docs/plans/x.json", "stateX/x"],
)
def test_path_outside_roots_refused_before_any_read(tmp_path, bad):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "x.json").write_text("{}\n")
    handle = {"path": bad, "sha256": "0" * 64, "bytes": 3}
    with pytest.raises(rs.ReplySidecarError) as exc:
        rs.read_text(tmp_path, handle)
    assert str(exc.value).startswith(gcc.RELAY_CORRUPT_HEAD)
    assert "arrived as" not in str(exc.value)


def test_write_outside_roots_refused(tmp_path):
    with pytest.raises(rs.ReplySidecarError):
        rs.write_reply(tmp_path, tmp_path / "docs" / "x.json", {})
    assert not (tmp_path / "docs").exists()


def test_malformed_handle_refused(tmp_path):
    for bad in ({}, {"path": "scratch/x", "sha256": "0" * 64}, {"path": "scratch/x", "sha256": "0" * 64, "bytes": True}):
        with pytest.raises(rs.ReplySidecarError):
            rs.read_text(tmp_path, bad)


def test_missing_sidecar_refused(tmp_path):
    with pytest.raises(rs.ReplySidecarError, match=gcc.RELAY_CORRUPT_HEAD):
        rs.read_text(tmp_path, {"path": "scratch/none.json", "sha256": "0" * 64, "bytes": 1})


def _payload_with_reader_bytes(target: int) -> dict:
    base = len(rs.reader_script(rs.canonical_json({"k": ""})))
    return {"k": "x" * (target - base)}


def test_reader_over_cap_is_refused_before_any_file_is_written(tmp_path):
    payload = _payload_with_reader_bytes(rs.READER_MAX_BYTES + 1)
    assert len(rs.reader_script(rs.canonical_json(payload))) == rs.READER_MAX_BYTES + 1
    path = tmp_path / "state" / "warp" / "run1" / "big.json"
    with pytest.raises(rs.ReplySidecarError, match=gcc.RELAY_CORRUPT_HEAD):
        rs.write_reply(tmp_path, path, payload)
    assert list(tmp_path.rglob("*")) == []


def test_reader_exactly_at_cap_is_written(tmp_path):
    assert rs.READER_MAX_BYTES == 524288
    payload = _payload_with_reader_bytes(rs.READER_MAX_BYTES)
    path = tmp_path / "state" / "warp" / "run1" / "big.json"
    handle = rs.write_reply(tmp_path, path, payload)
    assert (tmp_path / rs.reader_rel(handle["sha256"])).stat().st_size == rs.READER_MAX_BYTES
    assert rs.read_reply(tmp_path, handle) == payload


def test_handle_for_refuses_an_over_cap_reader_and_writes_no_reader(tmp_path):
    path = tmp_path / "state" / "warp" / "run1" / "big.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(rs.canonical_json(_payload_with_reader_bytes(rs.READER_MAX_BYTES + 1)).encode("ascii"))
    with pytest.raises(rs.ReplySidecarError, match=gcc.RELAY_CORRUPT_HEAD):
        rs.handle_for(tmp_path, path)
    assert not (tmp_path / rs.READER_ROOT).exists()


def test_stage_reply_schema_carries_the_parked_reply():
    from coordinator_core.ops.dispatch_emit import ask_contract as ac

    assert rs.STAGE_REPLY_SCHEMA["properties"]["parked"]["const"] == ac.STOP_AFTER_PLAN
    assert ac.PARKED_REPLY_KEY in rs.STAGE_REPLY_SCHEMA["properties"]
    for path in ("docs/plans/x.md", None):
        jsonschema.validate({ac.PARKED_REPLY_KEY: ac.STOP_AFTER_PLAN, "plan_path": path}, rs.STAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"parked": "size", "plan_path": None}, rs.STAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"parked": "plan", "plan_path": 3}, rs.STAGE_REPLY_SCHEMA)


def test_stage_reply_schema_accepts_already_staged_and_in_flight_replies(sidecar):
    from coordinator_core.ops.dispatch_emit import ask_stage_receipt as asr

    _, _, handle = sidecar
    assert asr.ALREADY_STAGED_KEY in rs.STAGE_REPLY_SCHEMA["properties"]
    assert asr.STAGE_IN_FLIGHT_KEY in rs.STAGE_REPLY_SCHEMA["properties"]
    jsonschema.validate({"manifest_handle": handle, asr.ALREADY_STAGED_KEY: True}, rs.STAGE_REPLY_SCHEMA)
    jsonschema.validate({asr.STAGE_IN_FLIGHT_KEY: {}}, rs.STAGE_REPLY_SCHEMA)
    jsonschema.validate({asr.STAGE_IN_FLIGHT_KEY: {"digest": "d", "pid": 1}}, rs.STAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"manifest_handle": handle, asr.ALREADY_STAGED_KEY: False}, rs.STAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"manifest_handle": handle, "already_stagedd": True}, rs.STAGE_REPLY_SCHEMA)


def test_stage_reply_schema_accepts_both_shapes_and_refuses_manifest_key(sidecar):
    _, _, handle = sidecar
    jsonschema.validate({"manifest_handle": handle}, rs.STAGE_REPLY_SCHEMA)
    jsonschema.validate({"error": "x", "owed_em_ids": ["a"], "owed_plan": None}, rs.STAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"manifest_handle": handle, "manifest": {}}, rs.STAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"manifest_handle": {**handle, "sha256": "zz"}}, rs.STAGE_REPLY_SCHEMA)


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


@pytest.mark.spawns_process
@pytest.mark.cadence
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


def test_js_names_no_runtime_decoder():
    for banned in ("atob", "Buffer", "TextDecoder", "_canonJson"):
        assert banned not in rs.VERIFY_REPLY_JS


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


def _reader_file(root, handle):
    return root / rs.reader_rel(handle["sha256"])


def test_reader_rel_refuses_bad_sha_before_any_filesystem_access():
    for bad in ("", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65, None, 5):
        with pytest.raises(rs.ReplySidecarError):
            rs.reader_rel(bad)
    assert rs.reader_rel("a" * 64) == f"scratch/stage-readers/{'a' * 64}.js"


def test_reader_script_is_pure_ascii_single_return():
    text = 'q"\\   café\n'
    body = rs.reader_script(text)
    assert body.startswith(rs.READER_META)
    body = body[len(rs.READER_META):]
    assert body.isascii() and body.startswith("return ") and body.endswith(";\n")
    assert json.loads(body[len("return "):-2]) == text


def test_write_reply_leaves_reader_and_handle_for_remints_it(sidecar):
    root, path, handle = sidecar
    reader = _reader_file(root, handle)
    assert reader.read_bytes() == rs.reader_script(path.read_text("ascii")).encode("ascii")
    reader.unlink()
    assert rs.handle_for(root, path) == handle
    assert reader.read_bytes() == rs.reader_script(path.read_text("ascii")).encode("ascii")
    assert not list(reader.parent.glob("*.tmp"))


def test_second_write_of_same_payload_leaves_reader_mtime_unchanged(sidecar):
    root, path, handle = sidecar
    reader = _reader_file(root, handle)
    before = reader.stat().st_mtime_ns
    assert rs.write_reply(root, path, _manifest()) == handle
    assert rs.handle_for(root, path) == handle
    assert reader.stat().st_mtime_ns == before


@pytest.mark.spawns_process
@pytest.mark.cadence
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


@pytest.mark.spawns_process
@pytest.mark.cadence
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


def test_read_stage_sidecar_js_names_no_runtime_decoder():
    for banned in ("atob", "Buffer", "TextDecoder", "agent("):
        assert banned not in rs.READ_STAGE_SIDECAR_JS


def _slotted_script(root, run_id="run1"):
    from coordinator_core.ops.dispatch_emit import nested_run_contract as nrc

    script = root / "scratch" / "warp" / f"{run_id}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"export const meta = {{ name: 'x' }}\n{nrc.STAGED_INLINE_SLOT}\n", encoding="utf-8")
    return nrc.staged_twin_rel


def test_write_reply_and_handle_for_leave_the_staged_twin_beside_a_slotted_script(tmp_path):
    twin_rel = _slotted_script(tmp_path)
    path = tmp_path / "state" / "warp" / "run1" / "manifest.json"
    handle = rs.write_reply(tmp_path, path, _manifest())
    twin = tmp_path / twin_rel("run1", handle["sha256"])
    assert str(handle["sha256"]) in twin.name
    assert handle["sha256"] in twin.read_text(encoding="utf-8")
    twin.unlink()
    assert rs.handle_for(tmp_path, path) == handle
    assert twin.is_file()


def test_write_reply_without_a_script_leaves_no_twin(sidecar):
    root, _, handle = sidecar
    assert not list(root.glob("scratch/warp/*staged*"))


def test_check_relay_default_cause_unchanged():
    msg = gcc.check_relay("record-urlenc", "abc", "9", "0" * 64)
    assert msg.endswith("the agent's copy of the pieces is damaged")
