"""publish.py must pass the end-of-run plugin-payload import gate.

A static `sys.path.insert(<bin>/lib)` makes `percolate.doc_path_refs.find_unresolved_imports` grade
publish.py, and its dynamically pinned `percolate` import then reads as unresolved: AC15 refuses the
round (coordinator 4.6.34, 2026-11-25).
"""

import shutil
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BIN.parent / "lib"))

from percolate.doc_path_refs import find_unresolved_imports  # noqa: E402


def test_publish_py_imports_resolve_in_payload(tmp_path):
    (tmp_path / "bin").mkdir()
    shutil.copy(_BIN / "publish.py", tmp_path / "bin" / "publish.py")
    unresolved = find_unresolved_imports(tmp_path, third_party=set())
    assert [u for u in unresolved if u[0] == "bin/publish.py"] == []


def test_phase_accounting_loads_by_path():
    sys.path.insert(0, str(_BIN))
    try:
        import importlib

        publish = importlib.import_module("publish")
    finally:
        sys.path.remove(str(_BIN))
    mod = publish._phase_accounting()
    assert Path(mod.__file__).resolve() == (_BIN / "lib" / "phase_accounting.py").resolve()
