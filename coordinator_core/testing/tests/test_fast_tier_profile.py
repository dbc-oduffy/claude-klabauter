from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pluggy
import pytest

from coordinator_core.testing import fast_tier_profile as ftp


class _Config:
    def __init__(self, workerid=None):
        self.pluginmanager = pluggy.PluginManager("pytest")
        if workerid:
            self.workerinput = {"workerid": workerid}


def test_unset_env_registers_nothing():
    config = _Config()
    assert ftp.register(config, {}) is None
    assert ftp.register(config, {ftp.ENV_KEY: ""}) is None
    assert config.pluginmanager.get_plugins() == set()


def test_env_set_registers_plugin_with_worker_id(tmp_path):
    config = _Config("gw3")
    plugin = ftp.register(config, {ftp.ENV_KEY: str(tmp_path / "p.json")})
    assert config.pluginmanager.get_plugin(ftp.PLUGIN_NAME) is plugin
    assert plugin.worker == "gw3"


def test_merge_tail_idle_is_wall_minus_busiest_worker():
    workers = [
        {"worker": "gw0", "tests": 5, "collection_s": 40.0, "session_setup_s": 50.0, "busy_s": 300.0, "tail_s": 2.0},
        {"worker": "gw1", "tests": 4, "collection_s": 42.0, "session_setup_s": 55.0, "busy_s": 200.0, "tail_s": 90.0},
        {"worker": "controller", "tests": 0, "collection_s": 1.0, "session_setup_s": 1.0, "busy_s": 0.0, "tail_s": 0.0},
    ]
    merged = ftp.merge(workers, 500.0)
    assert merged["tail_idle_s"] == 200.0 and merged["max_busy_s"] == 300.0
    assert merged["collection_s"] == 42.0 and merged["session_setup_s"] == 55.0 and merged["worker_tail_s"] == 90.0
    assert merged["workers"] == 2 and merged["sum_busy_s"] == 500.0


def test_real_pytest_run_writes_merged_profile_and_removes_parts(tmp_path):
    (tmp_path / "test_x.py").write_text("import time\n\ndef test_a():\n    time.sleep(0.05)\n\ndef test_b():\n    pass\n")
    out = tmp_path / "profile.json"
    # Stale part from an earlier run must not leak into the merge.
    Path(f"{out}.gwOLD.part").write_text(json.dumps({"worker": "gwOLD", "tests": 9, "collection_s": 0, "session_setup_s": 0, "busy_s": 999.0, "tail_s": 0}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "COORDINATOR_PYTEST"))}
    env[ftp.ENV_KEY] = str(out)
    env["PYTHONPATH"] = str(Path(ftp.__file__).resolve().parents[2])
    code = (
        "import sys, pytest\n"
        "from coordinator_core.testing import fast_tier_profile as f\n"
        "class P:\n"
        "    def pytest_configure(self, config):\n"
        "        f.register(config)\n"
        "sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', '--rootdir', sys.argv[1], sys.argv[1]], plugins=[P()]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    merged = json.loads(out.read_text())
    assert merged["workers"] == 1 and merged["per_worker"][0]["tests"] == 2
    assert merged["max_busy_s"] >= 0.05 and merged["sum_busy_s"] < 100
    assert merged["tail_idle_s"] == pytest.approx(merged["wall_s"] - merged["max_busy_s"], abs=0.002)
    assert not list(tmp_path.glob("profile.json.*.part"))
