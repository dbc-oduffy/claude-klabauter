"""Tests for the run-scratch-reap cost profile: base removes nothing, end stays inside the bar."""
from __future__ import annotations

import os
import subprocess

from coordinator_core.benchmarks import run_scratch_reap_profile as prof


def test_base_removes_nothing_end_reaps_under_bar(tmp_path):
    rows = {r.label: r for r in prof.run(tmp_path)}
    base, end = rows["base"], rows["end"]
    assert base.removed == 0
    # 200 plain + 25 expired keeps + the run's warp dir; the 25 unexpired keeps stay
    assert end.removed == prof.LEDGER_PATHS + prof.KEEPS_EXPIRED + 1
    assert end.kept == prof.KEEPS_LIVE
    assert end.process_time_ms < prof.BAR_MS
    assert end.spawns == 0
    assert end.listings == 0
    assert not prof.over_bar(list(rows.values()))


def test_meter_counts_spawns_and_listings_and_restores(tmp_path):
    real_init, real_scandir = subprocess.Popen.__init__, os.scandir
    with prof._Meter() as meter:
        os.listdir(tmp_path)
        list(os.walk(tmp_path))
        subprocess.run(["python", "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert meter.spawns == 1
    assert meter.listings >= 2
    assert subprocess.Popen.__init__ is real_init
    assert os.scandir is real_scandir


def test_over_bar_flags_a_spawn_a_listing_and_a_slow_end_row():
    ok = prof.ReapRow("end", 1, 0, 1.0, 0, 0)
    assert not prof.over_bar([ok])
    assert prof.over_bar([prof.ReapRow("end", 1, 0, 1.0, 1, 0)])
    assert prof.over_bar([prof.ReapRow("end", 1, 0, 1.0, 0, 1)])
    assert prof.over_bar([prof.ReapRow("end", 1, 0, prof.BAR_MS, 0, 0)])
    assert not prof.over_bar([prof.ReapRow("base", 0, 0, 900.0, 3, 3)])


def test_render_prints_base_and_end_rows():
    text = prof.render([prof.ReapRow("base", 0, 0, 0.0, 0, 0), prof.ReapRow("end", 226, 25, 12.5, 0, 0)])
    assert text.splitlines()[1].startswith("base")
    assert text.splitlines()[2].startswith("end")
