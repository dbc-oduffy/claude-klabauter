
from __future__ import annotations


import pytest

from coordinator_core.warm import server

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


_PROBE = (
    "import sys\n"
    "from coordinator_core.warm import server\n"
    "before = 'coordinator_core.warm.push_cadence' in sys.modules\n"
    "mod = server.push_cadence\n"
    "after = 'coordinator_core.warm.push_cadence' in sys.modules\n"
    "print(before, after, mod.__name__)\n"
)


def test_the_idle_sweep_import_closure_stays_off_the_boot_path():
    """`push_cadence` pulls git, ceremony and push ops (~60 modules): a cold-bytecode successor
    compiles all of them before its first answer. Loading `server` must not import it; the
    attribute stays reachable and imports on first use."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(server.__file__).resolve().parents[2]
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=root, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "True", "coordinator_core.warm.push_cadence"]


def test_a_worker_resolves_a_first_op_through_the_targeted_lookup_not_a_full_registry() -> None:
    import subprocess
    import sys

    probe = (
        "import sys;from coordinator_core import ipc;"
        "ipc.get_op_handler('ceremony.commit_v2');"
        "print(len(ipc._REGISTRY), len([m for m in sys.modules if m.startswith('coordinator_core.ops.')]))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    registered, ops_modules = (int(x) for x in out.stdout.split())
    assert registered >= 1
    assert ops_modules < 150, "a first-op lookup must not import the whole op tree"


@pytest.mark.spawns_process
def test_a_cold_guard_op_resolves_within_the_process_time_budget_and_imports_no_whole_op_tree() -> None:
    """A fresh process answers its first guard op without the whole-package import that stalled a
    fresh engine for 27s; budget is process time, never wall clock (500ms)."""
    import subprocess
    import sys

    probe = (
        "import sys,time;from coordinator_core import ipc;"
        "h=ipc.get_op_handler('hooks.guard_host_subagent_bash_ban');"
        "print(h is not None, time.process_time(),"
        " 'coordinator_core.ops.post_run_finish' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    resolved, process_s, whole_tree = out.stdout.split()
    assert resolved == "True"
    assert whole_tree == "False", "a guard op lookup must not import the whole op tree"
    assert float(process_s) < 0.5
