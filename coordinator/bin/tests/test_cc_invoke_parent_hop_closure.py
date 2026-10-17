"""Structural cost pins for cc_invoke's per-call parent work on the cold op-CLI hop.

Each check runs in a fresh interpreter and asserts modules loaded / spawns made,
never a timing.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_LIB = Path(__file__).resolve().parents[1] / "lib"
_ENGINE_ROOT = Path(__file__).resolve().parents[3]


def _run(body: str) -> dict:
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(_BIN_LIB)!r})\n"
        f"sys.path.insert(0, {str(_ENGINE_ROOT)!r})\n"
        "import cc_invoke\n"
        + body
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_op_timeout_cache_path_loads_no_openssl_hash_module() -> None:
    out = _run(
        "p = cc_invoke._op_timeout_map_cache_path(['engine-root-key'])\n"
        "loaded = ('hashlib' in sys.modules, '_hashlib' in sys.modules)\n"
        "import hashlib\n"
        "want = hashlib.sha1('engine-root-key'.encode('utf-8')).hexdigest()[:12]\n"
        "print(json.dumps({'loaded': loaded, 'path': p, 'want': want}))\n"
    )
    assert out["loaded"] == [False, False]
    assert out["path"].endswith(f"op-timeouts-{out['want']}.json")


def test_reader_root_question_makes_no_spawn() -> None:
    out = _run(
        "spawns = []\n"
        "import subprocess\n"
        "_orig = subprocess.Popen.__init__\n"
        "def _spy(self, *a, **k):\n"
        "    spawns.append(a)\n"
        "    return _orig(self, *a, **k)\n"
        "subprocess.Popen.__init__ = _spy\n"
        "root = cc_invoke.resolve_engine_root(cc_invoke.__file__)\n"
        "cc_invoke._reader_owns_one_of_the_split_trees(root)\n"
        "print(json.dumps({'spawns': len(spawns)}))\n"
    )
    assert out["spawns"] == 0
