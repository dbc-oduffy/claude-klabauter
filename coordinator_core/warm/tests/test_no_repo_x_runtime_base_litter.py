"""Guard: no warm test points the runtime base at a relative path, so `<repo>/x/` never appears.

Purpose: a relative `COORDINATOR_WARM_RUNTIME_BASE` resolves against the cwd (the repo root under
pytest) and a warm boot lock lands inside the repo. Pins the cause (no warm test sets a relative
base) and the symptom (no `<repo>/x/` directory exists).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WARM_TESTS = Path(__file__).resolve().parent


def _relative_base_setenvs(path: Path) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and len(node.args) >= 2):
            continue
        key, val = node.args[0], node.args[1]
        if not (isinstance(key, ast.Constant) and key.value == "COORDINATOR_WARM_RUNTIME_BASE"):
            continue
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            if not Path(val.value).is_absolute():
                hits.append(node.lineno)
    return hits


def test_no_warm_test_sets_a_relative_runtime_base():
    offenders = {
        p.name: lines
        for p in WARM_TESTS.rglob("test_*.py")
        if (lines := _relative_base_setenvs(p))
    }
    assert offenders == {}


def test_the_scanner_flags_a_relative_literal(tmp_path):
    bad = tmp_path / "test_bad.py"
    bad.write_text('m.setenv("COORDINATOR_WARM_RUNTIME_BASE", "x")\n', encoding="utf-8")
    assert _relative_base_setenvs(bad) == [1]


def test_repo_root_has_no_x_directory():
    assert not (REPO / "x").exists()
