"""peak_rss measures a spawned pytest tree's peak and flags a file over an explicit bound.

The bound is a parameter, so the over-bound case allocates 64 MiB against a 32 MiB bound and
no test here commits hundreds of MiB on a shared box. Each measurement spawns one pytest child.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.testing import peak_rss, spawn_depth

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]
pytest.importorskip("psutil")

MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _no_nested_refusal():
    refusal = spawn_depth.refusal_for_spawn(os.environ)
    if refusal:
        pytest.skip(refusal)


def test_over_bound_filters_by_peak():
    results = [peak_rss.PeakResult("a", 10, 0), peak_rss.PeakResult("b", 50, 0)]
    assert [r.path for r in peak_rss.over_bound(results, 32)] == ["b"]
    assert peak_rss.over_bound(results, 50) == []


def test_tiny_file_measured_and_under_bound(tmp_path: Path):
    f = tmp_path / "test_tiny.py"
    f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = peak_rss.measure_file(str(f))
    assert result.returncode == 0, "child run failed; its figure is inadmissible"
    assert result.peak_bytes > 0
    assert not peak_rss.over_bound([result], peak_rss.BOUND_BYTES)


def test_allocating_file_is_over_an_explicit_bound(tmp_path: Path):
    f = tmp_path / "test_alloc.py"
    f.write_text(
        "import time\n"
        "def test_alloc():\n"
        "    buf = bytearray(64 * 1024 * 1024)\n"
        "    for i in range(0, len(buf), 4096):\n"
        "        buf[i] = 1\n"
        "    time.sleep(1.0)\n"
        "    assert len(buf)\n",
        encoding="utf-8",
    )
    result = peak_rss.measure_file(str(f))
    assert result.returncode == 0
    assert result.peak_bytes > 32 * MIB
    assert peak_rss.over_bound([result], 32 * MIB) == [result]


def test_discover_files_finds_test_modules_under_scope(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "test_a.py").write_text("", encoding="utf-8")
    (tmp_path / "helper.py").write_text("", encoding="utf-8")
    found = peak_rss.discover_files([str(tmp_path)])
    assert [Path(p).name for p in found] == ["test_a.py"]
