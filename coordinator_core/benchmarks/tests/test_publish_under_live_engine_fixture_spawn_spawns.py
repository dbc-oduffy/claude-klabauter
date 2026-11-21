"""The harness fixture's env makes the client spawn its pair; a pair that never answers is explained."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks import publish_under_live_engine as h

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REPO = Path(__file__).resolve().parents[3]


def test_the_client_spawned_server_is_launched_with_the_fixture_env(tmp_path):
    dest = tmp_path / "dest"
    env = h._env_for(tmp_path / "work", dest)
    # `_child_env` reads os.environ: run it in a process that carries the harness env (PYTHONPATH
    # swapped to the real repo so the import resolves; the fixture dest does not exist here).
    probe = (
        "import json, sys\n"
        "from coordinator_core.ops.ceremony import detached_spawn as d\n"
        "e = d._child_env(sys.argv[1])\n"
        "print(json.dumps({k: e.get(k) for k in ('COORDINATOR_WARM_RUNTIME_BASE', 'PYTHONPATH', 'COORDINATOR_CLONE_ROOT')}"
        " | {'exe': d._resolve_python_exe()}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe, str(dest)], cwd=str(REPO), capture_output=True, text=True, timeout=60,
        env={**env, "PYTHONPATH": str(REPO)},
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout.splitlines()[-1])
    assert seen["COORDINATOR_WARM_RUNTIME_BASE"] == env["COORDINATOR_WARM_RUNTIME_BASE"]
    assert seen["COORDINATOR_CLONE_ROOT"] == str(dest)
    assert seen["PYTHONPATH"].split(";" if sys.platform == "win32" else ":")[0] == str(dest)
    assert Path(seen["exe"]) == Path(sys.executable)
