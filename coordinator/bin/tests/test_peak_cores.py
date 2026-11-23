"""Tests for coordinator/bin/peak-cores.py with an injected sampler; no real counters except one smoke."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("peak_cores", BIN / "peak-cores.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _seq(points):
    it = iter(points)
    last = [points[-1]]

    def read():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]

    return read


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_peak_math_takes_max_adjacent_fraction():
    # deltas: 50/100, 90/100, 10/100 -> peak 0.9 of 8 cores = 7.2
    sampler = _seq([(0, 0), (50, 100), (140, 200), (150, 300), (150, 300)])
    clk = _Clock()
    res = mod.measure(sampler, 8, window_s=1.0, interval_s=0.25, sleep=clk.sleep, clock=clk.now)
    assert res["logical_cores"] == 8
    assert res["peak_fraction"] == 0.9
    assert res["peak_cores"] == 7.2
    assert res["samples"] == 5


def test_idle_box_reports_zero_and_clamps():
    clk = _Clock()
    res = mod.measure(_seq([(0, 0), (0, 100)]), 4, window_s=0.5, interval_s=0.25, sleep=clk.sleep, clock=clk.now)
    assert res["peak_cores"] == 0.0
    clk2 = _Clock()
    over = mod.measure(_seq([(0, 0), (500, 100)]), 4, window_s=0.25, interval_s=0.25, sleep=clk2.sleep, clock=clk2.now)
    assert over["peak_cores"] == 4.0


def test_help_exits_zero():
    proc = subprocess.run([sys.executable, str(BIN / "peak-cores.py"), "--help"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    assert "--window" in proc.stdout and "--json" in proc.stdout


def test_default_human_output_shape(capsys):
    assert mod.main(["--window", "0.1", "--interval", "0.05"], sampler=_seq([(0, 0), (10, 20)])) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("logical_cores: ")
    assert lines[1].startswith("peak_cores: ")


def test_json_output_shape(capsys):
    assert mod.main(["--json", "--window", "0.1", "--interval", "0.05"], sampler=_seq([(0, 0), (10, 20)])) == 0
    obj = json.loads(capsys.readouterr().out)
    assert set(obj) == {"logical_cores", "peak_cores", "peak_fraction", "samples", "window_s", "interval_s"}
    assert obj["peak_fraction"] == 0.5


def test_unreadable_counters_exit_6(capsys):
    def boom():
        raise OSError("nope")

    assert mod.main(["--window", "0.1"], sampler=boom) == mod.EXIT_UNREADABLE
    assert "cannot read CPU counters" in capsys.readouterr().err


def test_nonpositive_window_refused():
    with pytest.raises(SystemExit) as ei:
        mod.main(["--window", "0"], sampler=_seq([(0, 0)]))
    assert ei.value.code == 2


def test_real_host_sampler_smoke():
    proc = subprocess.run(
        [sys.executable, str(BIN / "peak-cores.py"), "--json", "--window", "0.5", "--interval", "0.1"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    obj = json.loads(proc.stdout)
    assert 0 <= obj["peak_cores"] <= obj["logical_cores"]
