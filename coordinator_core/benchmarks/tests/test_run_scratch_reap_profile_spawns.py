"""Tests for the run-scratch-reap cost profile: base removes nothing, end stays inside the bar."""
from __future__ import annotations

import os
import subprocess

from coordinator_core.benchmarks import run_scratch_reap_profile as prof
from pathlib import Path

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_meter_counts_spawns_and_listings_and_restores(tmp_path: Path) -> None:
    real_init, real_scandir = subprocess.Popen.__init__, os.scandir
    with prof._Meter() as meter:
        os.listdir(tmp_path)
        list(os.walk(tmp_path))
        subprocess.run(["python", "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert meter.spawns == 1
    assert meter.listings >= 2
    assert subprocess.Popen.__init__ is real_init
    assert os.scandir is real_scandir
