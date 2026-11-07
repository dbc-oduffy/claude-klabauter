"""A pinned publish whose percolate lib predates `find_commit_from_import_name_violations`
must still bootstrap, and the commit import-name gate must skip with an advisory."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

_REPO = Path(__file__).resolve().parents[3]
_PUBLISH = _REPO / "coordinator" / "bin" / "publish.py"

_DRIVER = r"""
import importlib.util, io, json, sys
from types import SimpleNamespace
sys.path[:0] = [sys.argv[2], sys.argv[3]]
spec = importlib.util.spec_from_file_location("publish_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["publish_under_test"] = mod
spec.loader.exec_module(mod)
mod._bootstrap_engine()
out, err = io.StringIO(), io.StringIO()
ok = mod._commit_import_name_gate(
    SimpleNamespace(source_dir=SimpleNamespace(parent=".")), "abc1234def", out=out, err=err
)
print(json.dumps({
    "ok": ok,
    "sentinel": [mod.CommitReadError is None, mod.find_commit_from_import_name_violations is None],
    "out": out.getvalue(),
    "err": err.getvalue(),
    "lib": mod.__getattr__("find_import_closure_violations").__module__,
}))
"""


def _run(lib: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _DRIVER, str(_PUBLISH), str(lib), str(_REPO / "coordinator" / "lib")],
        capture_output=True, text=True, cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_bootstrap_against_lib_lacking_the_names_skips_the_gate(tmp_path):
    lib = tmp_path / "lib"
    shutil.copytree(_REPO / "coordinator" / "lib" / "percolate", lib / "percolate",
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    closure = lib / "percolate" / "import_closure.py"
    text = closure.read_text(encoding="utf-8")
    text = re.sub(r"^class CommitReadError\b", "class _Gone1", text, flags=re.M)
    text = re.sub(r"^def find_commit_from_import_name_violations\b", "def _gone2", text, flags=re.M)
    closure.write_text(text, encoding="utf-8")

    result = _run(lib)

    assert result["sentinel"] == [True, True]
    assert result["ok"] is True
    assert "skipped" in result["out"] and "abc1234def" in result["out"]
    assert result["err"] == ""


def test_bootstrap_against_current_lib_binds_the_names():
    result = _run(_REPO / "coordinator" / "lib")

    assert result["sentinel"] == [False, False]
    assert "skipped" not in result["out"]
