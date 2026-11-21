"""Cold hook path import bound: the grok DIY-nag answer loads none of the in-process guard chain.

Process time and module sets only; no wall-clock assertion.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REPO = Path(__file__).resolve().parents[3]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The in-process Claude guard chain (`shim._cold_verdict`) and the engine-side modules; none belongs on the nag branch.
HEAVY = (
    "coordinator_core.host_hooks.tool_policy",
    "coordinator_core.bash_guards.dispatch",
    "coordinator_core.bash_guards.dispatch_checks",
    "coordinator_core.write_guards.engine",
    "coordinator_core.search.engine",
    "coordinator_core.deny_registry",
    "coordinator_core.warm.telemetry",
    "coordinator_core.warp_fire",
)

_PROBE = """
import json, sys
import coordinator_core.host_hooks.shim as shim
from coordinator_core.session import op_dispatch_nag
mode = sys.argv[1]
if mode == "nag":
    op_dispatch_nag.advisory_for_dispatch = lambda *a, **k: "NAGTEXT"
shim._dial = lambda request: None
payload = {"hook_event_name": "PreToolUse", "session_id": "s-1", "cwd": sys.argv[2],
           "tool_name": "Bash", "tool_input": {"command": "ls"}}
answer = shim.run("grok", json.dumps(payload))
print(json.dumps({"answer": answer, "modules": sorted(sys.modules)}))
"""


def _cold(mode: str, tmp_path: Path) -> dict:
    env = {**__import__("os").environ, "COORDINATOR_SETTINGS_HOME": str(tmp_path / "settings")}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, mode, str(tmp_path)],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_nag_answer_loads_no_heavy_module(tmp_path):
    out = _cold("nag", tmp_path)
    assert "NAGTEXT" in json.dumps(out["answer"])
    leaked = [m for m in HEAVY if m in out["modules"]]
    assert not leaked, "heavy module on the cold nag path: %s" % leaked


def test_chain_branch_is_what_loads_the_heavy_set(tmp_path):
    out = _cold("chain", tmp_path)
    present = [m for m in HEAVY[:4] if m in out["modules"]]
    assert present, "pin is vacuous: the no-nag branch no longer loads the guard chain"


def test_nag_module_import_defers_warp_fire(tmp_path):
    script = "import sys, coordinator_core.session.op_dispatch_nag; print('coordinator_core.warp_fire' in sys.modules)"
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO), capture_output=True, text=True,
                          timeout=60, creationflags=_NO_WINDOW)
    assert proc.stdout.strip() == "False", proc.stderr


@pytest.mark.parametrize("host", ["codex", "agy"])
def test_adapters_import_light(host, tmp_path):
    mod = {"codex": "codex_adapter", "agy": "agy_adapter"}[host]
    script = "import sys, coordinator_core.host_hooks.%s; print(any(m in sys.modules for m in %r))" % (mod, HEAVY)
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO), capture_output=True, text=True,
                          timeout=60, creationflags=_NO_WINDOW)
    assert proc.stdout.strip() == "False", proc.stderr
