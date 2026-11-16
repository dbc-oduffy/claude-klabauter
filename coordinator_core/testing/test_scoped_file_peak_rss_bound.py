"""Guard: no scoped coordinator_core test file's pytest run peaks above ``peak_rss.BOUND_BYTES``.

The guard verdict (``assert_within_bound``) is proven per commit against a synthetic file and an
explicit small bound, so nothing here commits hundreds of MiB on a shared box. The sweep over the
real scope runs one pytest child per file and is a cadence-gate leg only.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.testing import peak_rss, spawn_depth

pytest.importorskip("psutil")

MIB = 1024 * 1024
SCOPES = (
    "coordinator_core/ops/ceremony/tests",
    "coordinator_core/ops/dispatch_emit/tests",
)


def assert_within_bound(result: peak_rss.PeakResult, bound: int = peak_rss.BOUND_BYTES) -> None:
    """Fail naming the file when ``result`` peaks above ``bound``."""
    offenders = peak_rss.over_bound([result], bound)
    assert not offenders, (
        f"{result.path} peaked at {result.peak_bytes / MIB:.1f} MiB, over the "
        f"{bound / MIB:.0f} MiB bound (rc={result.returncode})"
    )


@pytest.fixture
def _spawn_allowed():
    refusal = spawn_depth.refusal_for_spawn(os.environ)
    if refusal:
        pytest.skip(refusal)


def test_verdict_names_an_over_bound_file_and_passes_a_small_one():
    big = peak_rss.PeakResult("tests/test_big.py", 64 * MIB, 0)
    small = peak_rss.PeakResult("tests/test_small.py", 1 * MIB, 0)
    with pytest.raises(AssertionError, match="tests/test_big.py"):
        assert_within_bound(big, 32 * MIB)
    assert_within_bound(small, 32 * MIB)
    assert_within_bound(big)  # 64 MiB is under the real 400 MiB bound


@pytest.mark.spawns_process
def test_measured_synthetic_file_is_flagged_over_an_explicit_bound(tmp_path: Path, _spawn_allowed):
    heavy = tmp_path / "test_heavy.py"
    heavy.write_text(
        "import time\n"
        "def test_heavy():\n"
        "    buf = bytearray(64 * 1024 * 1024)\n"
        "    for i in range(0, len(buf), 4096):\n"
        "        buf[i] = 1\n"
        "    time.sleep(1.0)\n"
        "    assert len(buf)\n",
        encoding="utf-8",
    )
    tiny = tmp_path / "test_tiny.py"
    tiny.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    heavy_result = peak_rss.measure_file(str(heavy))
    tiny_result = peak_rss.measure_file(str(tiny))

    assert heavy_result.returncode == 0 and tiny_result.returncode == 0, "child run failed; figures inadmissible"
    # A bare pytest child already sits near 50 MiB, so the explicit bound is the tiny file's own
    # peak plus 32 MiB: the 64 MiB allocation clears it, the tiny file cannot.
    bound = tiny_result.peak_bytes + 32 * MIB
    with pytest.raises(AssertionError, match="test_heavy.py"):
        assert_within_bound(heavy_result, bound)
    assert_within_bound(tiny_result, bound)


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.parametrize("rel_path", peak_rss.discover_files(SCOPES))
def test_scoped_file_peak_is_within_bound(rel_path: str, _spawn_allowed):
    assert_within_bound(peak_rss.measure_file(rel_path))
