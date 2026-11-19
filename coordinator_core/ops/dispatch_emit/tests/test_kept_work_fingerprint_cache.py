"""kept_work.fingerprint reads an unchanged file once and re-reads after a size or mtime change."""
import os
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import kept_work, run_dir_stat_cache


@pytest.fixture(autouse=True)
def _fresh_cache():
    run_dir_stat_cache.clear(kept_work.FINGERPRINT_CACHE_NAMESPACE)
    yield
    run_dir_stat_cache.clear(kept_work.FINGERPRINT_CACHE_NAMESPACE)


@pytest.fixture
def read_count(monkeypatch):
    calls = []
    real = Path.read_bytes

    def counting(self):
        calls.append(str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    return calls


def test_unchanged_file_is_read_once(tmp_path, read_count):
    (tmp_path / "a.txt").write_bytes(b"hello\r\n")
    first = [kept_work.fingerprint(tmp_path, "a.txt") for _ in range(5)]
    assert len(set(first)) == 1
    assert first[0] == kept_work.blob_sha(b"hello\r\n")  # raw bytes, CRLF not folded
    assert len(read_count) == 1


def test_size_change_rereads(tmp_path, read_count):
    f = tmp_path / "a.txt"
    f.write_bytes(b"one")
    before = kept_work.fingerprint(tmp_path, "a.txt")
    f.write_bytes(b"three")
    after = kept_work.fingerprint(tmp_path, "a.txt")
    assert before != after
    assert after == kept_work.blob_sha(b"three")
    assert len(read_count) == 2


def test_mtime_change_same_size_rereads(tmp_path, read_count):
    f = tmp_path / "a.txt"
    f.write_bytes(b"aaa")
    before = kept_work.fingerprint(tmp_path, "a.txt")
    f.write_bytes(b"bbb")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    after = kept_work.fingerprint(tmp_path, "a.txt")
    assert before != after
    assert after == kept_work.blob_sha(b"bbb")
    assert len(read_count) == 2


def test_absent_is_none_and_not_cached(tmp_path):
    assert kept_work.fingerprint(tmp_path, "missing.txt") is None
    (tmp_path / "missing.txt").write_bytes(b"x")
    assert kept_work.fingerprint(tmp_path, "missing.txt") == kept_work.blob_sha(b"x")


def test_unreadable_is_none_and_not_cached(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")

    def boom(self):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert kept_work.fingerprint(tmp_path, "a.txt") is None
    monkeypatch.undo()
    assert kept_work.fingerprint(tmp_path, "a.txt") == kept_work.blob_sha(b"x")


@pytest.fixture
def kept_parse_count(monkeypatch):
    run_dir_stat_cache.clear(kept_work.KEPT_PARSE_CACHE_NAMESPACE)
    calls = []
    real = kept_work.read

    def counting(record_dir):
        calls.append(str(record_dir))
        return real(record_dir)

    monkeypatch.setattr(kept_work, "read", counting)
    yield calls
    run_dir_stat_cache.clear(kept_work.KEPT_PARSE_CACHE_NAMESPACE)


def _write_kept(record: Path, blob: str) -> None:
    kept_work.write(record, {"C1": {"paths": {"a.txt": blob}}}, None)


def test_kept_fingerprints_parse_unchanged_file_once(tmp_path, kept_parse_count):
    _write_kept(tmp_path, "b" * 40)
    first = [kept_work.kept_fingerprints(tmp_path) for _ in range(4)]
    assert all(f == {"a.txt": frozenset({"b" * 40})} for f in first)
    assert len(kept_parse_count) == 1


def test_kept_fingerprints_reparse_after_rewrite(tmp_path, kept_parse_count):
    _write_kept(tmp_path, "b" * 40)
    kept_work.kept_fingerprints(tmp_path)
    _write_kept(tmp_path, "c" * 41)
    assert kept_work.kept_fingerprints(tmp_path) == {"a.txt": frozenset({"c" * 41})}
    assert len(kept_parse_count) == 2


def test_kept_fingerprints_absent_file_is_empty_and_not_cached(tmp_path, kept_parse_count):
    assert kept_work.kept_fingerprints(tmp_path) == {}
    _write_kept(tmp_path, "b" * 40)
    assert kept_work.kept_fingerprints(tmp_path) == {"a.txt": frozenset({"b" * 40})}


def test_kept_fingerprints_result_is_not_shared_between_callers(tmp_path, kept_parse_count):
    _write_kept(tmp_path, "b" * 40)
    kept_work.kept_fingerprints(tmp_path).clear()
    assert kept_work.kept_fingerprints(tmp_path) == {"a.txt": frozenset({"b" * 40})}
