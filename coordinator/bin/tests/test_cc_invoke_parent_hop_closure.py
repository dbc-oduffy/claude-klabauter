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


def test_import_does_not_load_tempfile() -> None:
    out = _run("print(json.dumps({'tempfile': 'tempfile' in sys.modules}))\n")
    assert out["tempfile"] is False


def test_params_file_is_private_exclusive_and_tempfile_free() -> None:
    out = _run(
        "import os\n"
        "cc_invoke._params_tempdir = lambda root: os.path.dirname(os.path.abspath(cc_invoke.__file__))\n"
        "loaded = 'tempfile' in sys.modules\n"
        "fd, path = cc_invoke._create_params_file('x')\n"
        "os.close(fd)\n"
        "try:\n"
        "    again = cc_invoke._create_params_file('x')\n"
        "    os.close(again[0]); os.unlink(again[1])\n"
        "    distinct = again[1] != path\n"
        "    named = os.path.basename(path).startswith('cc-invoke-params-')\n"
        "    os.urandom = lambda n: b'\\x00' * n\n"
        "    first = cc_invoke._create_params_file('x')\n"
        "    os.close(first[0])\n"
        "    try:\n"
        "        cc_invoke._create_params_file('x')\n"
        "        collided = False\n"
        "    except FileExistsError:\n"
        "        collided = True\n"
        "    finally:\n"
        "        os.unlink(first[1])\n"
        "finally:\n"
        "    os.unlink(path)\n"
        "print(json.dumps({'loaded': loaded, 'distinct': distinct, 'named': named, 'collided': collided}))\n"
    )
    assert out == {"loaded": False, "distinct": True, "named": True, "collided": True}


def test_rung3_declines_divergent_engine_before_importing_ipc() -> None:
    out = _run(
        "import coordinator_core, tempfile, os\n"
        "other = tempfile.mkdtemp()\n"
        "before = 'coordinator_core.ipc' in sys.modules\n"
        "path_before = list(sys.path)\n"
        "r = cc_invoke._try_stamped_in_process_dispatch('x.y', {}, other, other)\n"
        "print(json.dumps({'r': r, 'before': before, 'ipc': 'coordinator_core.ipc' in sys.modules,"
        " 'path_same': path_before == sys.path}))\n"
    )
    assert out["r"] is None
    assert out["before"] is False
    assert out["ipc"] is False
    assert out["path_same"] is True


def test_invoke_spawn_inherits_console_when_one_is_attached() -> None:
    out = _run(
        "cc_invoke._parent_has_console = lambda: True\n"
        "import os\n"
        "os_name = os.name\n"
        "kw = cc_invoke._invoke_spawn_kw('x')\n"
        "print(json.dumps({'nt': os_name == 'nt', 'kw': kw}))\n"
    )
    assert out["kw"] == ({"creationflags": 0} if out["nt"] else {})


def test_invoke_spawn_keeps_no_window_when_no_console() -> None:
    out = _run(
        "cc_invoke._parent_has_console = lambda: False\n"
        "kw = cc_invoke._invoke_spawn_kw('x')\n"
        "print(json.dumps({'kw': kw, 'flag': getattr(__import__('subprocess'), 'CREATE_NO_WINDOW', 0)}))\n"
    )
    if sys.platform == "win32":
        assert out["kw"] == {"creationflags": out["flag"]}
    else:
        assert out["kw"] == {}


@pytest.mark.skipif(sys.platform != "win32", reason="console probe is Windows-only")
@pytest.mark.parametrize(
    ("flag_name", "expected"),
    [("DETACHED_PROCESS", "False"), ("CREATE_NO_WINDOW", "True")],
)
def test_console_probe_follows_the_parents_console(flag_name: str, expected: str) -> None:
    code = (
        f"import sys; sys.path.insert(0, {str(_BIN_LIB)!r}); sys.path.insert(0, {str(_ENGINE_ROOT)!r})\n"
        "import cc_invoke; print(cc_invoke._parent_has_console())\n"
    )
    proc = subprocess.run(  # popup-safe-env-suppressed
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, flag_name),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_every_invoke_child_spawn_goes_through_the_console_seam() -> None:
    import ast

    tree = ast.parse((_BIN_LIB / "cc_invoke.py").read_text(encoding="utf-8"))
    seam_calls = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.run"):
            continue
        argv_src = ast.unparse(node.args[0]) if node.args else ""
        if "coordinator_core.invoke" not in argv_src and not argv_src.startswith(("argv", "_argv")):
            continue
        for kw in node.keywords:
            if kw.arg is None and "_invoke_spawn_kw" in ast.unparse(kw.value):
                seam_calls += 1
    assert seam_calls == 3


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
