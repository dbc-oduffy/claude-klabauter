"""Engine and hook entry cap BLAS pools in-process via setdefault, never box-wide."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.testing import spawn_depth

ROOT = Path(__file__).resolve().parents[2]
ENTRY_FILES = ("coordinator_core/ipc.py", "coordinator_core/hooks/_envelope.py")
ENTRY_MODULES = ("coordinator_core.ipc", "coordinator_core.hooks._envelope")


def _env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in spawn_depth.BLAS_THREAD_KEYS}
    env["PYTHONPATH"] = str(ROOT)
    env.update(extra)
    return env


def _probe(module: str, env: dict) -> dict:
    code = (
        "import json, os, sys; import %s; "
        "print(json.dumps({k: os.environ.get(k) for k in %r}))" % (module, spawn_depth.BLAS_THREAD_KEYS)
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_entry_caps_unset_pools_at_one(module):
    got = _probe(module, _env())
    assert got == {k: "1" for k in spawn_depth.BLAS_THREAD_KEYS}


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_entry_keeps_a_caller_set_value(module):
    got = _probe(module, _env(OPENBLAS_NUM_THREADS="4"))
    assert got["OPENBLAS_NUM_THREADS"] == "4"
    assert got["OMP_NUM_THREADS"] == "1"


@pytest.mark.parametrize("rel", ENTRY_FILES)
def test_entry_keys_mirror_spawn_depth_and_only_setdefault(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    for key in spawn_depth.BLAS_THREAD_KEYS:
        assert '"%s"' % key in text
    assert not re.search(r"os\.environ\[[^\]]*\]\s*=", text)
    assert not re.search(r"\b(setx|putenv|HKCU|HKLM)\b", text)
