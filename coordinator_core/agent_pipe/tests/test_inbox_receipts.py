"""Tests for the agent_pipe receipt store contract."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core.agent_pipe import inbox_receipts as ir
from coordinator_core.agent_pipe.contract import mailbox_path


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))


def test_write_then_read_round_trips_keyed_by_msg_id(tmp_path: Path) -> None:
    path = ir.write("conv-a", "m1", "recv-1", "2026-11-28T00:00:00Z")
    ir.write("conv-a", "m2", "recv-2", "2026-11-28T00:00:01Z")
    assert path.is_relative_to(tmp_path / "agent-pipe")
    assert path.parent == mailbox_path("x").parent.parent / "receipts"
    got = ir.read("conv-a")
    assert set(got) == {"m1", "m2"}
    assert got["m1"] == {"msg_id": "m1", "receiver": "recv-1", "ts": "2026-11-28T00:00:00Z"}


def test_read_is_per_sender_and_empty_when_none() -> None:
    ir.write("conv-a", "m1", "r", "t")
    assert ir.read("conv-b") == {}
    assert set(ir.read("conv-a")) == {"m1"}


def test_first_receipt_wins_and_torn_lines_are_skipped() -> None:
    path = ir.write("conv-a", "m1", "first", "t1")
    ir.write("conv-a", "m1", "second", "t2")
    with open(path, "ab") as f:
        f.write(b'{"msg_id": "m3", "rece\n[1]\n')
    got = ir.read("conv-a")
    assert got["m1"]["receiver"] == "first"
    assert set(got) == {"m1"}


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "../x", "a\0b"])
def test_ids_that_escape_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        ir.write(bad, "m1", "r", "t")
    with pytest.raises(ValueError):
        ir.read(bad)
    with pytest.raises(ValueError):
        ir.write("conv-a", bad, "r", "t")


def test_missing_settings_root_is_a_typed_refusal_naming_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "absent"))
    with pytest.raises(ir.ReceiptRootMissing, match="COORDINATOR_SETTINGS_HOME"):
        ir.write("conv-a", "m1", "r", "t")
    with pytest.raises(ir.ReceiptRootMissing, match="COORDINATOR_SETTINGS_HOME"):
        ir.read("conv-a")
    assert not (tmp_path / "absent").exists()


def test_concurrent_writes_keep_every_receipt() -> None:
    threads = [threading.Thread(target=ir.write, args=("conv-a", f"m{i}", "r", "t")) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(ir.read("conv-a")) == {f"m{i}" for i in range(24)}


def test_write_and_read_spawn_no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[object] = []
    real = subprocess.Popen.__init__

    def counting(self, *a, **kw):
        spawns.append(a)
        real(self, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    ir.write("conv-a", "m1", "r", "t")
    ir.read("conv-a")
    assert spawns == []
