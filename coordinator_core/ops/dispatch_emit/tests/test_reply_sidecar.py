"""Tests for the reply sidecar handle contract."""

from __future__ import annotations

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


def test_check_relay_default_cause_unchanged():
    msg = gcc.check_relay("record-urlenc", "abc", "9", "0" * 64)
    assert msg.endswith("the agent's copy of the pieces is damaged")
