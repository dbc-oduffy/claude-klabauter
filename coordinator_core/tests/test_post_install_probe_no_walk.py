"""
coordinator_core.tests.test_post_install_probe_no_walk -- the post-install probe walks no filesystem.

`scripts/setup.py :: run_health_probe` runs `bin/claude-klabauter-doctor-probe.py --step-zero --required-only`
on the install path. This falsifier loads the probe module in-process, records every filesystem-walk
call (os.walk, os.scandir, Path.rglob, Path.glob, glob.glob) with the probe id that made it and the
call site, delegates to the real function so one run names every walking probe, and asserts none
was recorded. Per-probe cost is process time, never wall clock.
"""

from __future__ import annotations

import glob as _glob_mod
import importlib.util
import os
import pathlib
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"

_OUTSIDE_PROBE = "<outside-probe>"
_PROBE_FN_PREFIX = "_run_probe_"


def _load_probe_module():
    spec = importlib.util.spec_from_file_location("claude_klabauter_doctor_probe_no_walk", _BIN_PROBE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _call_site(depth: int) -> str:
    """First non-patcher, non-frozen caller frame as `<file>:<line>`."""
    frame = sys._getframe(depth)
    while frame is not None and frame.f_code.co_filename.startswith("<frozen"):
        frame = frame.f_back
    if frame is None:
        return "<unknown>"
    return f"{frame.f_code.co_filename}:{frame.f_lineno}"


def test_step_zero_required_only_walks_no_filesystem(tmp_path, monkeypatch, capsys):
    """`--step-zero --required-only` makes zero filesystem-walk calls.

    The failure message is the worklist of walking probe ids with per-probe process time.
    """
    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("CLAUDE_HOME", str(settings_home))
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(_REPO_ROOT))
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(_REPO_ROOT))
    monkeypatch.chdir(tmp_path)

    probe = _load_probe_module()

    current = {"probe": _OUTSIDE_PROBE}
    walks: list[tuple[str, str, str]] = []
    cpu_by_probe: dict[str, float] = {}

    real_timed = probe._timed

    def recording_timed(fn, *args):
        name = getattr(fn, "__name__", str(fn))
        pid = name[len(_PROBE_FN_PREFIX):] if name.startswith(_PROBE_FN_PREFIX) else name
        prev = current["probe"]
        current["probe"] = pid
        t0 = time.process_time()
        try:
            return real_timed(fn, *args)
        finally:
            cpu_by_probe[pid] = cpu_by_probe.get(pid, 0.0) + (time.process_time() - t0)
            current["probe"] = prev

    monkeypatch.setattr(probe, "_timed", recording_timed)

    def _wrap_function(owner, attr: str, label: str) -> None:
        real = getattr(owner, attr)

        def recorder(*a, **kw):
            walks.append((current["probe"], label, _call_site(2)))
            return real(*a, **kw)

        monkeypatch.setattr(owner, attr, recorder)

    _wrap_function(os, "walk", "os.walk")
    _wrap_function(os, "scandir", "os.scandir")
    _wrap_function(_glob_mod, "glob", "glob.glob")

    real_rglob = pathlib.Path.rglob
    real_glob = pathlib.Path.glob

    def rglob(self, *a, **kw):
        walks.append((current["probe"], "Path.rglob", _call_site(2)))
        return real_rglob(self, *a, **kw)

    def glob(self, *a, **kw):
        walks.append((current["probe"], "Path.glob", _call_site(2)))
        return real_glob(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "rglob", rglob)
    monkeypatch.setattr(pathlib.Path, "glob", glob)

    monkeypatch.setattr(sys, "argv", ["claude-klabauter-doctor-probe", "--step-zero", "--required-only"])
    probe.main()
    capsys.readouterr()

    probe_walks = [w for w in walks if w[0] != _OUTSIDE_PROBE]
    by_probe: dict[str, list[str]] = {}
    for pid, label, site in probe_walks:
        by_probe.setdefault(pid, []).append(f"{label} @ {site}")

    cpu_report = ", ".join(
        f"{pid}={cpu * 1000.0:.1f}ms" for pid, cpu in sorted(cpu_by_probe.items())
    )
    walker_report = "; ".join(
        f"{pid} ({len(sites)} walk call(s), first: {sites[0]})" for pid, sites in sorted(by_probe.items())
    )
    assert not by_probe, (
        f"--step-zero --required-only walked the filesystem in probe(s): {walker_report}. "
        f"Per-probe process time: {cpu_report}"
    )
