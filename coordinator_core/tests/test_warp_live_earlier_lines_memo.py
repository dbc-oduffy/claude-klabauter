"""warp_live._earlier_lines: a repeat scan reads only bytes appended since the last one."""

from __future__ import annotations

import os
import random

import pytest

from coordinator_core import warp_live

TAIL = 200
HEAD = 2000
CHUNK = 128


@pytest.fixture(autouse=True)
def small_windows(monkeypatch):
    monkeypatch.setattr(warp_live, "_TRANSCRIPT_TAIL_BYTES", TAIL)
    monkeypatch.setattr(warp_live, "_TRANSCRIPT_HEAD_BYTES", HEAD)
    monkeypatch.setattr(warp_live, "_SCAN_CHUNK_BYTES", CHUNK)
    warp_live._EARLIER_MEMO.clear()
    yield
    warp_live._EARLIER_MEMO.clear()


def _reference(path: str, needle: str) -> str:
    """An unmemoised full scan of the same window."""
    key = needle.encode()
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        stop = max(0, size - TAIL)
        start = max(0, stop - HEAD)
        handle.seek(start)
        window = handle.read(stop - start)
    parts = window.split(b"\n")
    lines = [p.rstrip(b"\r") for p in parts if key in p]
    return b"\n".join(lines).decode("utf-8", errors="replace")


class _CountingFile:
    def __init__(self, inner, counter):
        self._inner = inner
        self._counter = counter

    def read(self, *args):
        data = self._inner.read(*args)
        self._counter[0] += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


@pytest.fixture
def bytes_read(monkeypatch):
    counter = [0]
    real_open = open

    def counting_open(path, mode="r", *args, **kwargs):
        return _CountingFile(real_open(path, mode, *args, **kwargs), counter)

    monkeypatch.setattr(warp_live, "open", counting_open, raising=False)
    return counter


def _line(i: int, needle: str = "", width: int = 40) -> bytes:
    body = f"row-{i:05d}-" + "x" * width
    if needle:
        body += f" {needle}"
    return (body + "\n").encode()


def _write(path, lines):
    path.write_bytes(b"".join(lines))


def _append(path, lines):
    with open(path, "ab") as handle:
        handle.write(b"".join(lines))


def test_repeat_call_on_an_unchanged_transcript_reads_no_head_bytes(tmp_path, bytes_read):
    path = tmp_path / "t.jsonl"
    _write(path, [_line(i, "task-a" if i % 7 == 0 else "") for i in range(80)])
    first = warp_live._earlier_lines(str(path), "task-a")
    assert first == _reference(str(path), "task-a")
    assert first
    bytes_read[0] = 0
    assert warp_live._earlier_lines(str(path), "task-a") == first
    assert bytes_read[0] <= warp_live._EARLIER_PROBE_BYTES


def test_append_reads_only_the_newly_exposed_bytes_and_matches_a_full_scan(tmp_path, bytes_read):
    path = tmp_path / "t.jsonl"
    _write(path, [_line(i, "task-a" if i % 5 == 0 else "") for i in range(60)])
    warp_live._earlier_lines(str(path), "task-a")
    added = [_line(100 + i, "task-a" if i % 3 == 0 else "") for i in range(10)]
    _append(path, added)
    bytes_read[0] = 0
    result = warp_live._earlier_lines(str(path), "task-a")
    assert result == _reference(str(path), "task-a")
    appended = sum(len(b) for b in added)
    assert bytes_read[0] <= appended + warp_live._EARLIER_PROBE_BYTES + 128


def test_lines_sliding_from_the_tail_into_the_head_window_are_found(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [_line(i) for i in range(10)] + [_line(99, "task-a")])
    assert warp_live._earlier_lines(str(path), "task-a") == ""  # still inside the tail
    for step in range(12):
        _append(path, [_line(200 + step)])
        assert warp_live._earlier_lines(str(path), "task-a") == _reference(str(path), "task-a")
    assert "task-a" in warp_live._earlier_lines(str(path), "task-a")


def test_a_partial_line_that_completes_later_is_matched_once_whole(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"".join(_line(i) for i in range(30)) + b"half task-")
    _append(path, [_line(500) for _ in range(8)])
    warp_live._earlier_lines(str(path), "task-a")
    with open(path, "ab") as handle:
        handle.write(b"a rest\n")
    _append(path, [_line(600 + i) for i in range(8)])
    assert warp_live._earlier_lines(str(path), "task-a") == _reference(str(path), "task-a")


def test_truncation_invalidates_the_memo(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [_line(i, "task-a" if i % 4 == 0 else "") for i in range(80)])
    warp_live._earlier_lines(str(path), "task-a")
    _write(path, [_line(i, "task-b") for i in range(70)])
    assert warp_live._earlier_lines(str(path), "task-a") == ""
    assert warp_live._earlier_lines(str(path), "task-b") == _reference(str(path), "task-b")


def test_a_replaced_file_of_greater_size_is_rescanned(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [_line(i, "task-a") for i in range(40)])
    stale = warp_live._earlier_lines(str(path), "task-a")
    assert stale
    _write(path, [_line(1000 + i, "task-a" if i % 2 else "") for i in range(90)])
    assert warp_live._earlier_lines(str(path), "task-a") == _reference(str(path), "task-a")


def test_a_missing_transcript_answers_empty_and_stores_nothing(tmp_path):
    assert warp_live._earlier_lines(str(tmp_path / "absent.jsonl"), "task-a") == ""
    assert warp_live._EARLIER_MEMO == {}


def test_the_memo_is_keyed_on_the_needle(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [_line(i, "task-a" if i % 2 else "task-b") for i in range(60)])
    a = warp_live._earlier_lines(str(path), "task-a")
    b = warp_live._earlier_lines(str(path), "task-b")
    assert a != b
    assert a == _reference(str(path), "task-a")
    assert b == _reference(str(path), "task-b")


def test_memo_key_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(warp_live, "_EARLIER_MEMO_MAX_KEYS", 3)
    path = tmp_path / "t.jsonl"
    _write(path, [_line(i) for i in range(40)])
    for n in range(8):
        warp_live._earlier_lines(str(path), f"needle-{n}")
    assert len(warp_live._EARLIER_MEMO) == 3
    assert (str(path), "needle-7") in warp_live._EARLIER_MEMO
    assert (str(path), "needle-0") not in warp_live._EARLIER_MEMO


def test_matched_bytes_over_the_cap_are_answered_but_not_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(warp_live, "_EARLIER_MEMO_MAX_BYTES", 10)
    path = tmp_path / "t.jsonl"
    _write(path, [_line(i, "task-a") for i in range(40)])
    assert warp_live._earlier_lines(str(path), "task-a") == _reference(str(path), "task-a")
    assert warp_live._EARLIER_MEMO == {}


def test_crlf_lines_match_a_full_scan(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [_line(i, "task-a" if i % 3 == 0 else "").replace(b"\n", b"\r\n") for i in range(60)])
    assert warp_live._earlier_lines(str(path), "task-a") == _reference(str(path), "task-a")
    _append(path, [_line(900 + i, "task-a").replace(b"\n", b"\r\n") for i in range(5)])
    assert warp_live._earlier_lines(str(path), "task-a") == _reference(str(path), "task-a")


def test_random_growth_always_equals_an_unmemoised_scan(tmp_path):
    rng = random.Random(7)
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"")
    for step in range(60):
        chunk = b"".join(
            _line(step * 10 + i, "task-a" if rng.random() < 0.2 else "", width=rng.randint(1, 90))
            for i in range(rng.randint(1, 6))
        )
        if rng.random() < 0.3:
            chunk = chunk.rstrip(b"\n")
        _append(path, [chunk])
        assert warp_live._earlier_lines(str(path), "task-a") == _reference(str(path), "task-a"), step
