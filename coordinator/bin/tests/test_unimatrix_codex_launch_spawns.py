"""Pins for the Codex launcher (daemon + --remote argv) and the SessionStart peer registration."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


launch = _load("unimatrix-codex-launch.py", "unimatrix_codex_launch")
hook = _load("unimatrix-codex-hook-sessionstart.py", "unimatrix_codex_hook")
contract = _load("unimatrix-peer-contract.py", "unimatrix_peer_contract_t")

SOCK = "/home/u/.codex/app-server-control/app-server-control.sock"  # abs-path-ok: synthetic socket path fixture, never opened


T1, T2, T3 = (f"00000000-0000-4000-8000-00000000000{i}" for i in (1, 2, 3))


def test_hook_process_exits_zero_on_garbage_stdin():
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-codex-hook-sessionstart.py")],
                       input="not json", capture_output=True, text=True)
    assert r.returncode == 0
