"""Cost pin for `doctor.run_doctor()`: zero process spawns and under 0.5 s of process time in-process.

Process time, never wall clock: wall clock measures peer load. No platform branch, so running this
file on a Windows host is the Windows measurement.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import doctor

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
_PROCESS_TIME_BUDGET_S = 0.5


@pytest.fixture
def healthy_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "coordinator-content-repo"
    scripts = root / "coordinator" / "hooks" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "real.py").write_text("import sys\nsys.exit(0)\n")
    (root / "coordinator" / "hooks" / "hooks.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
            {"type": "command",
             "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py"}]}]}
    }))
    home = tmp_path / "settings-home"
    (home / "bin").mkdir(parents=True)
    shutil.copy(
        _CLAUDE_KLABAUTER_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py",
        home / "bin" / "_resolve_claude_klabauter.py",
    )
    config = tmp_path / "claude-config"
    config.mkdir()
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(root))
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(_CLAUDE_KLABAUTER_ROOT))
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(_CLAUDE_KLABAUTER_ROOT))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(home / "machine-local"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return root


def test_run_doctor_spawns_nothing_and_stays_under_the_process_time_budget(
    healthy_install: Path, monkeypatch: pytest.MonkeyPatch
):
    constructed: list[object] = []
    real_init = subprocess.Popen.__init__

    def counting_init(self, *args, **kwargs):
        constructed.append(args[:1])
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting_init)

    start = time.process_time()
    report, _ = doctor.run_doctor()
    elapsed = time.process_time() - start

    assert report.layers, "run_doctor returned no layers; the measurement proves nothing"
    assert len(constructed) == 0, f"run_doctor constructed Popen: {constructed}"
    assert elapsed < _PROCESS_TIME_BUDGET_S, f"process time {elapsed:.3f}s >= {_PROCESS_TIME_BUDGET_S}s"
