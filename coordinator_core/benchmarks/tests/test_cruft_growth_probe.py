"""The cruft growth probe reports residue and growth truthfully on both sides of its verdict."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from coordinator_core.benchmarks import cruft_growth_probe as probe

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUNS = 3
KILL_EVERY = 2


def _noop_sweep(layout, session_id):
    return {"process_time_ms": 0.0, "spawns": 0, "listings": 0}


def _owner_keyed_sweep(layout, session_id):
    """An ideal owner-keyed pruner: removes every owned path of every run but the current session's."""
    for manifest_file in layout.manifests.glob("run-*.json"):
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest["session_id"] == session_id:
            continue
        for path in manifest["owned"].values():
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.lexists(path):
                os.unlink(path)
    shutil.rmtree(layout.repo / "scratch" / ".deleted", ignore_errors=True)
    return _noop_sweep(layout, session_id)


def test_no_sweep_leaves_owned_paths_and_reports_red(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(probe, "sweep_once", _noop_sweep)
    result = probe.run_probe(tmp_path, RUNS, KILL_EVERY)
    normal, killed = result["per_run"][0], result["per_run"][1]
    assert killed["killed"] and not normal["killed"]
    for kind in ("scratch_copy", "warp_dir", "share_dir", "session_dir", "temp_index", "stall_dump"):
        assert kind in killed["residual_kinds"]
    # A normal run cleans its managed temp dir and the run-end reap takes its ledgered scratch.
    assert "managed_temp" not in normal["residual_kinds"]
    assert "scratch_copy" not in normal["residual_kinds"] and "warp_dir" not in normal["residual_kinds"]
    # No writer cleans its share, session, temp-index or stall record: owned paths accrue per run.
    for kind in ("share_dir", "session_dir", "temp_index", "stall_dump"):
        assert result["residual_by_kind"][kind] == RUNS
    assert result["residual_owned_paths"] == sum(r["residual_owned_paths"] for r in result["per_run"]) > 0
    assert result["green"] is False
    after = result["bytes_after_run"]
    assert len(after) == RUNS
    assert result["bytes_growth_run2_to_runN"] == after[-1] - after[1]
    assert result["bytes_growth_per_run"] == round((after[-1] - after[1]) / (RUNS - 2), 1)


def test_owner_keyed_sweep_leaves_nothing_and_reports_green(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(probe, "sweep_once", _owner_keyed_sweep)
    result = probe.run_probe(tmp_path, RUNS, KILL_EVERY)
    assert result["residual_owned_paths"] == 0
    assert all(r["residual_owned_paths"] == 0 for r in result["per_run"])
    assert result["bounded"] is True and result["green"] is True
    assert result["peak_run_footprint_bytes"] > 0


def test_fixture_under_the_managed_run_temp_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run-temp"):
        probe.Layout(tmp_path / "coordinator" / "_fleet" / "run-temp" / "x").build()
