"""Tests for the reply sidecar handle contract."""

from __future__ import annotations

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


def test_sidecar_is_ascii_json_with_trailing_newline(sidecar):
    _, path, handle = sidecar
    raw = path.read_bytes()
    assert raw.isascii() and raw.endswith(b"}\n")
    assert handle["bytes"] == len(raw)


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


def test_pages_join_to_text_and_fit_the_escaped_bound(sidecar):
    root, _, handle = sidecar
    text = rs.read_text(root, handle)
    _, pages = rs.page(text, 0)
    assert pages > 1
    assert _joined(text) == text
    assert all(len(json.dumps(rs.page(text, i)[0])) <= rs.PAGE_CHARS for i in range(pages))


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
    reply = rs.page_reply(handle, rs.read_text(root, handle), 1)
    assert set(reply) == {"sha256", "bytes", "page", "pages", "chunk"}
    jsonschema.validate(reply, rs.PAGE_REPLY_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**reply, "extra": 1}, rs.PAGE_REPLY_SCHEMA)


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
    assert "ASCII" in _verify_js(tmp_path, "café", {"sha256": "0" * 64, "bytes": 4})


def test_check_relay_default_cause_unchanged():
    msg = gcc.check_relay("record-urlenc", "abc", "9", "0" * 64)
    assert msg.endswith("the agent's copy of the pieces is damaged")
