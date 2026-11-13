"""Every stall dump, full record or stub, carries the running engine's stamp and source sha."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.warm import hang_restart, server, skew


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_full_and_stub_dumps_carry_stamp_and_sha(tmp_path, monkeypatch):
    stall_dir = tmp_path / "svc" / "stall-dumps"
    monkeypatch.setattr(hang_restart, "maybe_restart", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ENGINE_STAMP_LINES", "engine-stamp: sha:abc123\nsource-sha: abc123\n")
    full = server._write_stall_record(stall_dir, None, key="a", leg="{}")
    stub = server._write_stall_record(stall_dir, None, key="b", leg="{}")
    assert full != stub
    assert _lines(full)[:2] == ["engine-stamp: sha:abc123", "source-sha: abc123"]
    assert "--- threads ---" in full.read_text(encoding="utf-8")
    stub_lines = _lines(stub)
    assert stub_lines[0].startswith("stub:")
    assert stub_lines[1:3] == ["engine-stamp: sha:abc123", "source-sha: abc123"]


def test_stall_class_still_leads_the_header(tmp_path, monkeypatch):
    stall_dir = tmp_path / "svc" / "stall-dumps"
    monkeypatch.setattr(hang_restart, "maybe_restart", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ENGINE_STAMP_LINES", "engine-stamp: s\nsource-sha: x\n")
    full = server._write_stall_record(stall_dir, None, key="a", stall_class="io-wait")
    assert _lines(full)[:3] == ["stall-class: io-wait", "engine-stamp: s", "source-sha: x"]


def test_reads_stamp_and_strips_dirty_suffix_from_sha(tmp_path, monkeypatch):
    skew.write_engine_stamp(tmp_path, "sha:deadbeef+dirty-1234")
    monkeypatch.setattr(server, "_engine_clone_root", lambda: Path(tmp_path))
    assert server._read_engine_stamp_lines() == (
        "engine-stamp: sha:deadbeef+dirty-1234\nsource-sha: deadbeef\n"
    )


def test_unstamped_tree_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_engine_clone_root", lambda: Path(tmp_path))
    assert server._read_engine_stamp_lines() == "engine-stamp: unstamped\nsource-sha: unknown\n"
