"""The warm server's stall-dumps dir carries an owner mark, stamped once, through every writer entry."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core import owner_mark
from coordinator_core.warm import server


def _assert_pid_mark(stall_dir: Path) -> None:
    mark = owner_mark.read_mark(stall_dir)
    assert mark is not None
    assert (mark["owner_kind"], mark["owner_id"]) == ("pid", str(os.getpid()))
    assert mark["ttl_hours"] is None
    assert owner_mark.owner_alive(mark) is True


def test_ensure_stall_dir_creates_and_stamps(tmp_path: Path) -> None:
    stall = server._ensure_stall_dir(tmp_path / "svc" / "stall-dumps")
    assert stall.is_dir()
    _assert_pid_mark(stall)


def test_ensure_stall_dir_keeps_the_first_mark(tmp_path: Path) -> None:
    stall = tmp_path / "stall-dumps"
    owner_mark.write_mark(stall, owner_mark.OWNER_PID, 999999)
    server._ensure_stall_dir(stall)
    mark = owner_mark.read_mark(stall)
    assert mark is not None and mark["owner_id"] == "999999"


def test_unstampable_dir_still_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(owner_mark, "write_mark", boom)
    assert server._ensure_stall_dir(tmp_path / "stall-dumps").is_dir()


def test_claim_stall_episode_stamps_the_dir(tmp_path: Path) -> None:
    stall = tmp_path / "stall-dumps"
    assert server._claim_stall_episode(stall) is True
    _assert_pid_mark(stall)


def test_stall_stub_stamps_the_dir(tmp_path: Path) -> None:
    stall = tmp_path / "stall-dumps"
    stub = server._write_stall_stub(stall, None, key="k1", leg="leg")
    assert stub is not None and stub.is_file()
    _assert_pid_mark(stall)


def test_full_stall_record_stamps_the_dir_and_the_mark_is_not_pruned(tmp_path: Path) -> None:
    stall = tmp_path / "stall-dumps"
    record = server._write_stall_record(
        stall, None, key="k2", leg="leg", repo_root=tmp_path, sample_box=False
    )
    assert record is not None and record.is_file()
    _assert_pid_mark(stall)
    assert (stall / owner_mark.MARK_FILE).is_file()
    assert not list(stall.glob("*.tmp"))
