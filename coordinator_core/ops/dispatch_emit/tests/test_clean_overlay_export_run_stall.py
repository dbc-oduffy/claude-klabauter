"""`--export-run` is bounded by silence, never by a flat wall (example-game-repo 2da5dc needed ~660s against a 120s cap)."""

from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

pytestmark = [pytest.mark.spawns_process]


def _route_export(monkeypatch, tmp_path: Path) -> None:
    @contextmanager
    def fake_export(root, run_id, paths, leg=None, argv=()):
        yield tmp_path

    monkeypatch.setattr(cov, "clean_export", fake_export)
    monkeypatch.setattr(cov, "_mirror_env", lambda env, dest: env)


def test_a_chatty_command_outlives_the_old_flat_cap(monkeypatch, tmp_path: Path) -> None:
    _route_export(monkeypatch, tmp_path)
    monkeypatch.setattr(cov, "TEST_TIMEOUT_S", 1)
    monkeypatch.setattr(cov, "PYTEST_STARTUP_S", 1)
    monkeypatch.setenv(cov.EXPORT_RUN_STALL_ENV, "1")
    chatty = "import time\nfor _ in range(8):\n    print('.', flush=True); time.sleep(0.3)\n"
    done = cov.run_in_export(tmp_path, "r", [], [sys.executable, "-c", chatty])
    assert done.returncode == 0 and done.stdout.count(".") == 8


def test_a_silent_command_is_stopped_as_a_stall(monkeypatch, tmp_path: Path) -> None:
    _route_export(monkeypatch, tmp_path)
    monkeypatch.setattr(cov, "PYTEST_STARTUP_S", 1)
    monkeypatch.setenv(cov.EXPORT_RUN_STALL_ENV, "1")
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        cov.run_in_export(tmp_path, "r", [], [sys.executable, "-c", "import time; time.sleep(30)"])
    assert "stalled" in str(exc.value)


@pytest.mark.parametrize("raw, want", [("", None), ("900", 900.0), ("0", None), ("-5", None), ("x", None)])
def test_the_stall_window_override(monkeypatch, raw: str, want) -> None:
    monkeypatch.setenv(cov.EXPORT_RUN_STALL_ENV, raw)
    assert cov.export_run_stall_s() == (float(cov.PYTEST_STALL_S) if want is None else want)
