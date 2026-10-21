"""Tests for the reply sidecar handle contract."""

from __future__ import annotations

import base64
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


def _joined(text: str) -> str:
    _, pages = rs.page(text, 0)
    return "".join(rs.page(text, i)[0] for i in range(pages))


def _decoded(handle: dict, text: str) -> list[str]:
    _, pages = rs.page(text, 0)
    return [base64.b64decode(rs.page_reply(handle, text, i)["chunk"]).decode("ascii") for i in range(pages)]


def test_pages_join_to_text_and_fit_the_quoted_bound(sidecar):
    root, _, handle = sidecar
    text = rs.read_text(root, handle)
    _, pages = rs.page(text, 0)
    assert pages > 1
    assert _joined(text) == text
    assert "".join(_decoded(handle, text)) == text
    assert all(len(json.dumps(rs.page_reply(handle, text, i)["chunk"])) <= rs.PAGE_CHARS for i in range(pages))
    assert all(len(rs.page(text, i)[0]) == rs.PAGE_RAW_CHARS for i in range(pages - 1))


def test_a_full_quote_page_stays_within_the_bound():
    text = '"' * (rs.PAGE_RAW_CHARS * 3)
    handle = {"sha256": "0" * 64, "bytes": len(text)}
    assert len(json.dumps(rs.page_reply(handle, text, 0)["chunk"])) <= rs.PAGE_CHARS
    assert rs.PAGE_RAW_CHARS == 4497


def test_empty_text_is_one_page_and_past_the_end_raises():
    assert rs.page("", 0) == ("", 1)
    with pytest.raises(IndexError):
        rs.page("", 1)
    with pytest.raises(IndexError):
        rs.page("abc", -1)


def test_pathological_quotes_page_under_eight_thousand():
    text = '"' * 20000
    handle = {"sha256": "0" * 64, "bytes": len(text)}
    _, pages = rs.page(text, 0)
    assert _joined(text) == text
    for i in range(pages):
        assert len(json.dumps(rs.page_reply(handle, text, i))) < 8000


def test_page_reply_keys_and_schema(sidecar):
    root, _, handle = sidecar
    text = rs.read_text(root, handle)
    reply = rs.page_reply(handle, text, 1)
    assert set(reply) == {"sha256", "bytes", "page", "pages", "encoding", "chunk", "chunk_sha256"}
    assert reply["encoding"] == "base64"
    assert reply["chunk_sha256"] == hashlib.sha256(rs.page(text, 1)[0].encode("ascii")).hexdigest()
    jsonschema.validate(reply, rs.PAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**reply, "extra": 1}, rs.PAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**reply, "chunk": 'ab"cd'}, rs.PAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**reply, "encoding": "raw"}, rs.PAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({k: v for k, v in reply.items() if k != "chunk_sha256"}, rs.PAGE_REPLY_SCHEMA)


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


def _node_eval(tmp_path, expr: str):
    script = tmp_path / "eval.js"
    script.write_text(rs.VERIFY_REPLY_JS + f"\nconsole.log(JSON.stringify({expr}));\n", encoding="ascii")
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_js_b64_decoder_and_page_verify(sidecar, tmp_path):
    root, _, handle = sidecar
    text = rs.read_text(root, handle)
    reply = rs.page_reply(handle, text, 1)
    raw = rs.page(text, 1)[0]
    assert _node_eval(tmp_path, f"_b64DecodeAscii({json.dumps(reply['chunk'])})") == raw
    assert _node_eval(tmp_path, "_b64DecodeAscii('ab\"d')") is None
    assert _node_eval(tmp_path, f"_b64DecodeAscii({json.dumps(base64.b64encode('café'.encode()).decode())})") is None
    assert _node_eval(tmp_path, "_b64DecodeAscii('abc')") is None
    assert _node_eval(tmp_path, f"_verifyPageChunk({json.dumps(reply)})") == {"text": raw}
    bad = reply["chunk"][:5] + ("A" if reply["chunk"][5] != "A" else "B") + reply["chunk"][6:]
    assert "fail" in _node_eval(tmp_path, f"_verifyPageChunk({json.dumps({**reply, 'chunk': bad})})")
    assert "fail" in _node_eval(tmp_path, f"_verifyPageChunk({json.dumps({**reply, 'chunk_sha256': '0' * 64})})")


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


def test_check_relay_default_cause_unchanged():
    msg = gcc.check_relay("record-urlenc", "abc", "9", "0" * 64)
    assert msg.endswith("the agent's copy of the pieces is damaged")
