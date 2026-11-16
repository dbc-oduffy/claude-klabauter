"""Tests for the distill scriptPath launcher: packed payload round-trip, nested call, receipt, cap refusal."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.distill import launch_script as ls
from coordinator_core.ops.dispatch_emit import launcher_staged_twin as twin
from coordinator_core.ops.dispatch_emit.emit import _WORKFLOW_SCRIPT_BYTE_CAP
from coordinator_core.ops.dispatch_emit.nested_run_contract import STAGED_UNPACK_JS

_TEXT_OPEN = "const ARGS = JSON.parse(_unpackStaged({text: "
_PLAIN_OPEN = "const ARGS = JSON.parse("


def _args(rows: int = 40) -> dict:
    return {
        "runId": "run-1",
        "repoRoot": "/repo",
        "argsInlined": True,
        "batches": [
            {"batchId": f"b{i}", "files": [f"docs/wiki/section/page-{i}.md"], "description": "same text every row"}
            for i in range(rows)
        ],
        "unicode": "caf\u00e9 \u2028 \"quoted\" \\ back",
    }


def _decode(source: str) -> object:
    """Python mirror of what the Workflow VM does with the launcher's ARGS line."""
    dec = json.JSONDecoder()
    if _TEXT_OPEN in source:
        start = source.index(_TEXT_OPEN) + len(_TEXT_OPEN)
        text, end = dec.raw_decode(source, start)
        marker = ", pack: "
        assert source[end : end + len(marker)] == marker
        pack, _ = dec.raw_decode(source, end + len(marker))
        return json.loads(twin._expand(text, pack["m"], pack["t"]))
    start = source.index(_PLAIN_OPEN) + len(_PLAIN_OPEN)
    text, _ = dec.raw_decode(source, start)
    return json.loads(text)


def _write(tmp_path: Path, args: dict, **kw) -> dict:
    return ls.write_launcher(args, workflow_script="/abs/harvest.workflow.js", run_dir=tmp_path, **kw)


def test_round_trip_reproduces_args_exactly(tmp_path):
    args = _args()
    reply = _write(tmp_path, args)
    source = Path(reply["scriptPath"]).read_text(encoding="utf-8")
    assert _decode(source) == args


def test_packed_launcher_carries_unpacker_and_is_smaller_than_inline(tmp_path):
    args = _args(400)
    reply = _write(tmp_path, args)
    source = Path(reply["scriptPath"]).read_text(encoding="utf-8")
    assert STAGED_UNPACK_JS in source
    assert reply["launcher_bytes"] < len(json.dumps(args, separators=(",", ":")).encode("utf-8"))


def test_unpackable_payload_renders_plain_parse_without_unpacker(tmp_path):
    args = {"runId": "r", "repoRoot": "/r", "n": 1}
    assert twin._pack_manifest(json.dumps(args, separators=(",", ":"), ensure_ascii=False)) is None
    reply = _write(tmp_path, args)
    source = Path(reply["scriptPath"]).read_text(encoding="utf-8")
    assert STAGED_UNPACK_JS not in source
    assert _decode(source) == args


def test_launcher_shape_meta_first_and_one_nested_call(tmp_path):
    reply = _write(tmp_path, _args())
    source = Path(reply["scriptPath"]).read_text(encoding="utf-8")
    assert source.startswith("export const meta = {")
    assert source.count("workflow(") == 1
    assert 'return await workflow({scriptPath: "/abs/harvest.workflow.js"}, ARGS)' in source


def test_workflow_script_is_posix_even_from_a_windows_style_path(tmp_path):
    win = "C:" + "\\dev\\h.workflow.js"
    reply = ls.write_launcher(_args(), workflow_script=win, run_dir=tmp_path)
    source = Path(reply["scriptPath"]).read_text(encoding="utf-8")
    assert "\\\\" not in source.split("workflow(")[1]


def test_reply_shape_and_hash_match_file_bytes(tmp_path):
    reply = _write(tmp_path, _args(), file_name="launch-leg2.workflow.mjs")
    path = Path(reply["scriptPath"])
    data = path.read_bytes()
    assert set(reply) == {"scriptPath", "sha256", "launcher_bytes"}
    assert path.name == "launch-leg2.workflow.mjs"
    assert "\\" not in reply["scriptPath"]
    assert reply["launcher_bytes"] == len(data)
    assert reply["sha256"] == hashlib.sha256(data).hexdigest()
    assert b"\r" not in data


def test_receipt_written_beside_launcher(tmp_path):
    reply = _write(tmp_path, _args())
    assert Path(reply["scriptPath"] + ".emitted.json").is_file()


def test_over_cap_refuses_naming_bytes_and_cap_and_writes_nothing(tmp_path):
    big = {"runId": "r", "repoRoot": "/r", "blob": "x" * (_WORKFLOW_SCRIPT_BYTE_CAP + 10)}
    with pytest.raises(ValueError) as exc:
        _write(tmp_path, big)
    assert str(_WORKFLOW_SCRIPT_BYTE_CAP) in str(exc.value)
    assert "bytes" in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_just_under_cap_is_accepted(tmp_path):
    probe = _write(tmp_path, {"blob": ""}, file_name="probe.workflow.mjs")
    room = _WORKFLOW_SCRIPT_BYTE_CAP - probe["launcher_bytes"]
    reply = _write(tmp_path, {"blob": "y" * room})
    assert reply["launcher_bytes"] <= _WORKFLOW_SCRIPT_BYTE_CAP
    with pytest.raises(ValueError):
        _write(tmp_path, {"blob": "y" * (room + 1)}, file_name="over.workflow.mjs")


def test_missing_workflow_script_refused(tmp_path):
    with pytest.raises(ValueError, match="workflow_script"):
        ls.write_launcher(_args(), workflow_script="", run_dir=tmp_path)
