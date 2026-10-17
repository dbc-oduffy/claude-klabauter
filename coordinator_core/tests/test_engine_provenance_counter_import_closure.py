"""Import-closure pin: the provenance counter sink loads only what its branch uses."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_IMPORT_DEFERRED = (
    "coordinator_core.machine_profile",
    "coordinator_core.repo_standing",
    "coordinator_core.session.claimed_write",
    "coordinator_core.subagent_sandbox",
)

_PROBE = """
import sys
import coordinator_core.engine_provenance_counter as epc
deferred = {deferred!r}
{body}
loaded = [m for m in deferred if m in sys.modules]
print("LOADED=" + ",".join(loaded))
"""

_AUTHOR_CALL = """
import coordinator_core.engine_provenance_counter as epc
from coordinator_core import machine_profile
root = {root!r}
epc.resolve_git_root_cheap = lambda cwd=None: root
machine_profile.machine_profile = lambda: "author"
epc.record_engine_provenance("c", "dispatch", "match", None, None)
"""


def _loaded(body: str, tmp_path) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["PYTHONPATH"] = str(_REPO_ROOT)
    code = _PROBE.format(deferred=_IMPORT_DEFERRED, body=body)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, cwd=str(_REPO_ROOT), timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("LOADED=")][-1]
    return [m for m in line[len("LOADED="):].split(",") if m]


@pytest.mark.spawns_process
def test_module_import_loads_no_deferred_module(tmp_path):
    assert _loaded("", tmp_path) == []


@pytest.mark.spawns_process
def test_author_box_record_never_loads_repo_standing(tmp_path):
    body = _AUTHOR_CALL.format(root=str(tmp_path))
    loaded = _loaded(body, tmp_path)
    assert "coordinator_core.repo_standing" not in loaded
    assert (tmp_path / "state" / "engine-provenance-counts.jsonl").exists()
