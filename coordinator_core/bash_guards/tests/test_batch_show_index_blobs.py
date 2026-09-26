
from __future__ import annotations

from coordinator_core.bash_guards import dispatch_checks


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


def _batch_stdout(sha_a: str, sha_c: str) -> bytes:
    header_a = f"{sha_a} blob 5\n".encode("utf-8")
    content_a = b"hello\n"
    missing_b = b":paths/b missing\n"
    header_c = f"{sha_c} blob 3\n".encode("utf-8")
    content_c = b"xyz\n"
    return header_a + content_a + missing_b + header_c + content_c


def test_multi_path_batch_resolves_present_and_missing_by_own_slot(monkeypatch):
    sha_a = "1" * 40
    sha_c = "2" * 40
    stdout = _batch_stdout(sha_a, sha_c)

    monkeypatch.setattr(
        dispatch_checks.subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, stdout),
    )

    results = dispatch_checks._batch_show_index_blobs(
        ["paths/a", "paths/b", "paths/c"], cwd=None,
    )

    assert results == {"paths/a": "hello", "paths/b": None, "paths/c": "xyz"}


def test_nonzero_returncode_fails_the_whole_batch_closed(monkeypatch):
    sha_a = "1" * 40
    sha_c = "2" * 40
    stdout = _batch_stdout(sha_a, sha_c)

    monkeypatch.setattr(
        dispatch_checks.subprocess, "run",
        lambda *a, **k: _FakeCompleted(1, stdout),
    )

    results = dispatch_checks._batch_show_index_blobs(
        ["paths/a", "paths/b", "paths/c"], cwd=None,
    )

    assert results == {"paths/a": None, "paths/b": None, "paths/c": None}


def test_empty_input_short_circuits_without_spawning(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("must not spawn for an empty path list")

    monkeypatch.setattr(dispatch_checks.subprocess, "run", _fail)
    assert dispatch_checks._batch_show_index_blobs([], cwd=None) == {}
