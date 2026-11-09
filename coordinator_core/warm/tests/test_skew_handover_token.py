"""`skew.HANDOVER_TOKEN_ENV`: a handover successor serves its own generation while the live stamp is
still the superseded one, and the override expires on any other stamp."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.warm import skew

OLD = bytes([115, 104, 97, 58, 111, 108, 100, 10])  # b"sha:old\n"
NEW = bytes([115, 104, 97, 58, 110, 101, 119, 10])  # b"sha:new\n"
OTHER = bytes([115, 104, 97, 58, 111, 116, 104, 10])  # b"sha:oth\n"


def _root(tmp_path: Path, stamp: bytes) -> Path:
    (tmp_path / "coordinator_core").mkdir()
    skew.replace_engine_stamp_bytes(tmp_path, stamp) if (tmp_path / "coordinator_core" / "_engine_stamp").exists() else (
        tmp_path / "coordinator_core" / "_engine_stamp"
    ).write_bytes(stamp)
    return tmp_path


def test_override_serves_the_successor_token_only_while_the_superseded_stamp_is_live(tmp_path, monkeypatch):
    root = _root(tmp_path, OLD)
    monkeypatch.setenv(skew.HANDOVER_TOKEN_ENV, f"{skew.stamp_token(NEW)}:{skew.stamp_token(OLD)}")

    assert skew.compute_client_token(root) == skew.stamp_token(NEW)

    skew.replace_engine_stamp_bytes(root, NEW)
    assert skew.compute_client_token(root) == skew.stamp_token(NEW)

    skew.replace_engine_stamp_bytes(root, OTHER)
    assert skew.compute_client_token(root) == skew.stamp_token(OTHER)


def test_without_the_env_the_token_is_the_live_stamp(tmp_path, monkeypatch):
    root = _root(tmp_path, OLD)
    monkeypatch.delenv(skew.HANDOVER_TOKEN_ENV, raising=False)

    assert skew.compute_client_token(root) == skew.stamp_token(OLD)
