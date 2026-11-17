"""stall_writer.submit never waits on the disk; one writer thread makes the directory once."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import stall_writer


@pytest.fixture(autouse=True)
def _fresh_dirs():
    stall_writer._made_dirs.clear()
    yield
    stall_writer.flush(5.0)
    stall_writer._made_dirs.clear()


def test_submit_returns_fast_while_writer_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    entered = threading.Event()

    def slow_mkdir(directory):
        entered.set()
        release.wait(10)
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(stall_writer, "_mkdir", slow_mkdir)
    assert stall_writer.submit(tmp_path / "d" / "a.txt", "one") is True
    assert entered.wait(5)
    started = time.perf_counter()
    assert stall_writer.submit(tmp_path / "d" / "b.txt", "two") is True
    assert (time.perf_counter() - started) < 0.05
    release.set()
    assert stall_writer.flush(5.0)
    assert (tmp_path / "d" / "a.txt").read_text(encoding="utf-8") == "one"
    assert (tmp_path / "d" / "b.txt").read_text(encoding="utf-8") == "two"


def test_full_queue_drops_and_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    entered = threading.Event()

    def slow_write(path, text):
        entered.set()
        release.wait(10)

    monkeypatch.setattr(stall_writer, "_write", slow_write)
    before = stall_writer.dropped_count()
    assert stall_writer.submit(tmp_path / "x.txt", "0")
    assert entered.wait(5)
    accepted = [stall_writer.submit(tmp_path / f"{i}.txt", "t") for i in range(stall_writer.QUEUE_MAX + 5)]
    assert accepted.count(False) == 5
    assert stall_writer.dropped_count() - before == 5
    release.set()
    assert stall_writer.flush(10.0)


def test_directory_made_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real = stall_writer._mkdir
    monkeypatch.setattr(stall_writer, "_mkdir", lambda d: (calls.append(d), real(d)))
    for i in range(4):
        stall_writer.submit(tmp_path / "dd" / f"{i}.txt", "t")
    assert stall_writer.flush(5.0)
    assert len(calls) == 1
    assert len(list((tmp_path / "dd").glob("*.txt"))) == 4


def test_claim_runs_on_writer_thread_and_loser_writes_stub(tmp_path: Path) -> None:
    threads = []

    def claim_won():
        threads.append(threading.current_thread())
        return True

    def claim_lost():
        threads.append(threading.current_thread())
        return False

    stall_writer.submit(tmp_path / "full.txt", "FULL", claim=claim_won, stub_text="stub:")
    stall_writer.submit(tmp_path / "stub.txt", "FULL", claim=claim_lost, stub_text="stub:")
    stall_writer.submit(tmp_path / "none.txt", "FULL", claim=claim_lost)
    assert stall_writer.flush(5.0)
    assert (tmp_path / "full.txt").read_text(encoding="utf-8") == "FULL"
    assert (tmp_path / "stub.txt").read_text(encoding="utf-8") == "stub:"
    assert not (tmp_path / "none.txt").exists()
    assert all(t.name == "stall-writer" and t is not threading.current_thread() for t in threads)


def test_dir_removed_after_first_write_is_recreated(tmp_path: Path) -> None:
    stall_writer.submit(tmp_path / "r" / "a.txt", "a")
    assert stall_writer.flush(5.0)
    (tmp_path / "r" / "a.txt").unlink()
    (tmp_path / "r").rmdir()
    stall_writer.submit(tmp_path / "r" / "b.txt", "b")
    assert stall_writer.flush(5.0)
    assert (tmp_path / "r" / "b.txt").read_text(encoding="utf-8") == "b"
