"""probe_store._DirStats parity with os.stat and its listing economy."""
from __future__ import annotations

import os

from coordinator_core.engine_root import ENGINE_CHECKOUT_ROOT
from coordinator_core.roadmap import probe_store


def _sig(st):
    return (st.st_size, st.st_mtime_ns)


def test_parity_with_os_stat_over_closure():
    paths = probe_store._closure(os.path.normpath(str(ENGINE_CHECKOUT_ROOT)))
    assert paths
    ds = probe_store._DirStats()
    for p in paths:
        got = ds.stat(p)
        try:
            want = os.stat(p)
        except OSError:
            assert got is None, p
            continue
        assert _sig(got) == _sig(want), p


def test_absent_path_is_none(tmp_path):
    assert probe_store._DirStats().stat(str(tmp_path / "nope.py")) is None
    assert probe_store._DirStats().stat(str(tmp_path / "nodir" / "nope.py")) is None


def test_two_files_one_scandir(tmp_path, monkeypatch):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("a")
    b.write_text("bb")
    calls = []
    real = os.scandir

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(probe_store.os, "scandir", counting)
    ds = probe_store._DirStats()
    assert ds.stat(str(a)).st_size == 1
    assert ds.stat(str(b)).st_size == 2
    assert len(calls) == 1


def test_name_missing_from_listing_falls_through(tmp_path, monkeypatch):
    f = tmp_path / "f.py"
    f.write_text("x")
    real = os.scandir

    def dropping(path):
        return iter(()) if str(path) == str(tmp_path) else real(path)

    monkeypatch.setattr(probe_store.os, "scandir", dropping)
    assert probe_store._DirStats().stat(str(f)) is not None
