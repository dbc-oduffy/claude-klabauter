"""The client-minted -32004 names the engine's stall dump, or why none exists."""

from __future__ import annotations

from coordinator_core.warm import breadcrumb, client


def _point_stall_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda engine_root=None: tmp_path)
    monkeypatch.setattr(
        "coordinator_core.warm.engine_root.current_engine_clone", lambda: tmp_path
    )
    dump_dir = tmp_path / "stall-dumps"
    dump_dir.mkdir()
    return dump_dir


def _envelope(key):
    return client._indeterminate_envelope(
        {"id": 1, "method": "memo.send"}, "no response within 30s", key, waited_secs=30.0
    )


def test_matching_dump_is_named_in_text_and_data(tmp_path, monkeypatch):
    dump_dir = _point_stall_dir(monkeypatch, tmp_path)
    older = dump_dir / "20261102T192628Z-1-overdue-key-abc.txt"
    newer = dump_dir / "20261102T192725Z-1-overdue-key-abc.txt"
    other = dump_dir / "20261102T192900Z-1-overdue-key-zzz.txt"
    for f in (older, newer, other):
        f.write_text("x", encoding="utf-8")
    error = _envelope("key-abc")["error"]
    assert f"Stall dump: {newer}." in error["message"]
    assert error["data"]["stall_dump"] == str(newer)
    assert "stall_dump_absent_reason" not in error["data"]


def test_no_matching_dump_says_why_and_names_the_directory(tmp_path, monkeypatch):
    dump_dir = _point_stall_dir(monkeypatch, tmp_path)
    (dump_dir / "20261102T192900Z-1-overdue-key-zzz.txt").write_text("x", encoding="utf-8")
    error = _envelope("key-abc")["error"]
    assert "No stall dump named: no stall dump matches this request" in error["message"]
    assert str(dump_dir) in error["message"]
    assert "stall_dump" not in error["data"]
    assert error["data"]["stall_dump_absent_reason"]


def test_unsafe_key_never_globs(tmp_path, monkeypatch):
    dump_dir = _point_stall_dir(monkeypatch, tmp_path)
    (dump_dir / "20261102T192900Z-1-overdue-a.txt").write_text("x", encoding="utf-8")
    error = _envelope("*")["error"]
    assert "stall_dump" not in error["data"]
    assert "no dispatch key" in error["data"]["stall_dump_absent_reason"]
