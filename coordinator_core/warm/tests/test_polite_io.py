"""Tests for coordinator_core.warm.polite_io."""
import os
import sys
from unittest import mock

from coordinator_core.warm import polite_io


def test_lower_own_priority_returns_status_never_raises():
    with mock.patch.object(polite_io, "_lower_windows", return_value={"memory": True, "io": True}), \
         mock.patch.object(polite_io, "_lower_posix", return_value={"memory": True, "io": True}):
        st = polite_io.lower_own_priority()
    assert isinstance(st, dict) and "memory" in st and "io" in st and st["platform"] == sys.platform


def test_lower_own_priority_swallows_platform_failure():
    with mock.patch.object(polite_io, "_lower_windows", side_effect=OSError("x")), \
         mock.patch.object(polite_io, "_lower_posix", side_effect=OSError("x")):
        st = polite_io.lower_own_priority()
    assert st["memory"] is False and st["io"] is False and "error" in st


def test_module_has_no_top_level_ctypes_import():
    import ast
    with open(polite_io.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
            [node.module] if isinstance(node, ast.ImportFrom) else []
        assert not any(n and n.startswith("ctypes") for n in names)


def test_open_bulk_reads_content(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc" * 1000)
    with polite_io.open_bulk(p) as f:
        assert f.read() == b"abc" * 1000
    assert f.closed


def test_open_bulk_uses_sequential_flag_and_fadvise(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"x")
    seen = {}
    real_open = os.open

    def spy(path, flags, *a, **k):
        seen["flags"] = flags
        return real_open(path, flags, *a, **k)

    with mock.patch.object(os, "open", spy):
        with polite_io.open_bulk(p):
            pass
    if hasattr(os, "O_SEQUENTIAL"):
        assert seen["flags"] & os.O_SEQUENTIAL
    if hasattr(os, "posix_fadvise"):
        with mock.patch.object(os, "posix_fadvise") as adv:
            with polite_io.open_bulk(p):
                pass
        advices = [c.args[3] for c in adv.call_args_list]
        assert advices == [os.POSIX_FADV_SEQUENTIAL, os.POSIX_FADV_DONTNEED]


def test_open_bulk_missing_file_raises(tmp_path):
    try:
        with polite_io.open_bulk(tmp_path / "nope"):
            pass
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _limiter(rate, thr):
    c = _Clock()
    return polite_io.ReadRateLimiter(rate, thr, clock=c.now, sleep=c.sleep), c


def test_below_threshold_never_waits():
    lim, c = _limiter(1000, 500)
    for _ in range(100):
        assert lim.acquire(499) == 0.0
    assert c.slept == []


def test_at_or_above_threshold_waits_once_burst_spent():
    lim, c = _limiter(1000, 500)
    assert lim.acquire(1000) == 0.0  # burst of one second
    w = lim.acquire(500)
    assert abs(w - 0.5) < 1e-9
    assert c.slept == [w]


def test_tokens_refill_with_time():
    lim, c = _limiter(1000, 100)
    lim.acquire(1000)
    c.t += 1.0
    assert lim.acquire(1000) == 0.0


def test_hook_path_read_never_waits(tmp_path, monkeypatch):
    from coordinator_core.search import engine

    slept = []
    monkeypatch.setattr(
        engine, "_READ_LIMITER",
        polite_io.ReadRateLimiter(1, 1, sleep=slept.append),
    )
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * (3 * 1024 * 1024))
    counted = [0]
    assert engine._read_text(str(big), counted) is not None
    assert slept == []
    assert counted[0] == 3 * 1024 * 1024


def test_run_throttle_reaches_limiter_only_when_opted_in(tmp_path, monkeypatch):
    from coordinator_core.search import engine

    slept = []
    monkeypatch.setattr(
        engine, "_READ_LIMITER",
        polite_io.ReadRateLimiter(1, 1, sleep=slept.append),
    )
    (tmp_path / "a.txt").write_bytes(b"needle\n" * 1000)
    (tmp_path / "b.txt").write_bytes(b"needle\n" * 1000)
    spec = engine.SearchSpec(pattern="needle", targets=[str(tmp_path)], recursive=True)
    engine.GrepSource(spec).execute(str(tmp_path), None)
    assert slept == []
    engine.GrepSource(spec, throttle=True).execute(str(tmp_path), None)
    assert slept


def test_run_truncates_at_byte_cap(tmp_path, monkeypatch):
    from coordinator_core.search import engine

    monkeypatch.setattr(engine, "MAX_BYTES_SCANNED", 1000)
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_bytes(b"needle\n" * 400)
    spec = engine.SearchSpec(pattern="needle", targets=[str(tmp_path)], recursive=True)
    result = engine.run(spec, cwd=str(tmp_path))
    assert result.truncated and result.cap_hit == "byte-cap"
    assert result.files_scanned < 3


def test_bulk_throttle_opt_in_waits(tmp_path, monkeypatch):
    from coordinator_core.search import engine

    slept = []
    monkeypatch.setattr(
        engine, "_READ_LIMITER",
        polite_io.ReadRateLimiter(1, 1, sleep=slept.append),
    )
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * 4096)
    engine._read_text(str(big), None, throttle=True)
    engine._read_text(str(big), None, throttle=True)
    assert slept
