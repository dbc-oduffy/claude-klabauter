"""Fresh-session pin: a first-touch Grok guard() call delivers the DIY nag through the real worker.join budget."""
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_guard_cost_probe as probe  # noqa: E402

GUARD_SPEC = importlib.util.spec_from_file_location("unimatrix_host_guard_fresh", probe.BIN / "unimatrix-host-guard.py")
guard_mod = importlib.util.module_from_spec(GUARD_SPEC)
GUARD_SPEC.loader.exec_module(guard_mod)


def live_cli_call(work):
    """The exact Grok hook command (`python unimatrix-host-guard.py --host grok --engine-root <engine>`) as a fresh
    process on a live-shaped search_replace payload with a fresh uuid session id; returns (wall, answer, degrade rows)."""
    env = probe.grok_fresh_env(work)
    stdin, _ = probe.grok_live_payload(work, "pin", lane=True)
    t0 = time.perf_counter()
    out = subprocess.run([sys.executable, *probe.grok_live_cli(probe.live_engine_root())], input=stdin,
                         capture_output=True, text=True, timeout=120, env=env, creationflags=probe.NO_WINDOW)
    wall = time.perf_counter() - t0
    log = Path(env["COORDINATOR_SETTINGS_HOME"]) / "host-hooks" / "host-guard-degrades.jsonl"
    return wall, json.loads(out.stdout.strip().splitlines()[-1]), probe.count_lines(log)


@pytest.mark.deliberate_wall_clock(reason="the hook's DR-402 half-second join is a wall-clock budget by definition")
def test_exact_live_hook_command_delivers_nag_inside_the_budget(tmp_path):
    wall, answer, degrade_rows = live_cli_call(tmp_path)
    assert degrade_rows == 0, answer
    assert probe.carries_nag(answer), answer
    assert answer["hookSpecificOutput"]["additionalContext"], answer
    assert wall < guard_mod.DEFAULT_BUDGET_SECS, wall
