"""The dirty-scan bytes benchmark measures `_dirty_among` independently of any ledger counter.

Behaviour pinned: the fixture is stat-dirty for the touched files and stat-clean in the other variant,
`changed` is exactly the content-rewritten files in both, and the byte figure is the sum of the sizes
of the files `_dirty_among` actually read (so a reader that skips stat-clean paths shows fewer bytes).
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.benchmarks import land_run_dirty_scan_bytes as bench

pytestmark = pytest.mark.cadence

FILES = 25
CHANGED = 3


@pytest.fixture(scope="module")
def reports(tmp_path_factory):
    base = tmp_path_factory.mktemp("dirty-scan-bytes")
    return {v: bench.run_variant(v, base, FILES, CHANGED) for v in bench.VARIANTS}


def test_changed_is_exactly_the_rewritten_files_in_both_variants(reports):
    for variant, res in reports.items():
        assert res["changed_ok"] is True, (variant, res)
        assert res["changed"] == CHANGED, (variant, res)


def test_fixture_touch_makes_stat_dirty_without_changing_content(tmp_path):
    fx = bench.build_fixture(tmp_path / "r", FILES, CHANGED, touch=True)
    clean = bench.build_fixture(tmp_path / "c", FILES, CHANGED, touch=False)
    assert len(fx["touched"]) == FILES - CHANGED and clean["touched"] == []
    touched = fx["touched"][0]
    assert (tmp_path / "r" / touched).stat().st_mtime > (tmp_path / "c" / touched).stat().st_mtime
    assert os.path.getsize(tmp_path / "r" / touched) == os.path.getsize(tmp_path / "c" / touched)


def test_report_shape_and_the_changed_files_are_always_read(reports):
    """Holds at base and after the scan skips stat-clean paths: the rewritten files must be read either way."""
    for variant, res in reports.items():
        assert res["bytes_read"] > 0 and res["files_read"] >= CHANGED, (variant, res)
        assert res["process_ms"] >= 0.0 and isinstance(res["spawns"], int), (variant, res)


def test_stat_clean_never_reads_more_than_stat_dirty(reports):
    """A stat-clean tree is no costlier than a stat-dirty one; at base the two are equal (every file read)."""
    assert reports["stat_clean"]["bytes_read"] <= reports["stat_dirty"]["bytes_read"], reports
