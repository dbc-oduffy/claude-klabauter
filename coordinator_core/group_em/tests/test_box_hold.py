"""Tests for coordinator_core.group_em.box_hold; every case writes under tmp_path only."""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.group_em import box_hold


def test_round_trip(tmp_path):
    h = box_hold.set_hold("s1", "swap", 60, directory=tmp_path, now=1000.0)
    got = box_hold.read_hold(now=1010.0, directory=tmp_path)
    assert got == h
    assert got.set_by_session == "s1" and got.reason == "swap"
    assert got.expires_at == 1060.0


def test_absent_reads_none(tmp_path):
    assert box_hold.read_hold(directory=tmp_path) is None


def test_expiry(tmp_path):
    box_hold.set_hold("s1", "r", 60, directory=tmp_path, now=1000.0)
    assert box_hold.read_hold(now=1059.0, directory=tmp_path) is not None
    assert box_hold.read_hold(now=1060.0, directory=tmp_path) is None


def test_ttl_clamped(tmp_path):
    h = box_hold.set_hold("s1", "r", 10**9, directory=tmp_path, now=0.0)
    assert h.expires_at == box_hold.MAX_HOLD_TTL_S


def test_malformed_reads_none(tmp_path):
    p = tmp_path / "box" / "hold.json"
    p.parent.mkdir(parents=True)
    for body in ("{not json", "[]", '{"set_by_session": "s"}',
                 '{"set_by_session": "s", "reason": "r", "set_at": "x", "expires_at": 1e18}'):
        p.write_text(body, encoding="utf-8")
        assert box_hold.read_hold(now=1.0, directory=tmp_path) is None


def test_hand_written_record_ttl_capped_on_read(tmp_path):
    p = tmp_path / "box" / "hold.json"
    p.parent.mkdir(parents=True)
    p.write_text('{"set_by_session": "s", "reason": "r", "set_at": 0, "expires_at": 1e18}',
                 encoding="utf-8")
    assert box_hold.read_hold(now=box_hold.MAX_HOLD_TTL_S + 1, directory=tmp_path) is None


def test_non_utf8_record_reads_none(tmp_path):
    p = tmp_path / "box" / "hold.json"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xfe\x00")
    assert box_hold.read_hold(now=1.0, directory=tmp_path) is None


def test_clear_other_sessions_live_hold_refused(tmp_path):
    box_hold.set_hold("s1", "r", 600, directory=tmp_path)
    assert box_hold.clear_hold("s2", directory=tmp_path) is False
    assert box_hold.read_hold(directory=tmp_path) is not None


def test_clear_by_holder_and_operator(tmp_path):
    box_hold.set_hold("s1", "r", 600, directory=tmp_path)
    assert box_hold.clear_hold("s1", directory=tmp_path) is True
    assert box_hold.read_hold(directory=tmp_path) is None
    box_hold.set_hold("s1", "r", 600, directory=tmp_path)
    assert box_hold.clear_hold(None, directory=tmp_path) is True
    assert box_hold.clear_hold(None, directory=tmp_path) is False


def test_expired_hold_clearable_by_anyone(tmp_path):
    box_hold.set_hold("s1", "r", 1, directory=tmp_path, now=0.0)
    assert box_hold.clear_hold("s2", directory=tmp_path) is True


@pytest.mark.spawns_process
def test_import_closure_is_leaf():
    code = (
        "import sys; import coordinator_core.group_em.box_hold; "
        "bad=[m for m in ('subprocess','coordinator_core.group_em.nomination',"
        "'coordinator_core.group_em.session_registry') if m in sys.modules]; "
        "print(bad)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.stdout.strip() == "[]", out.stdout + out.stderr
