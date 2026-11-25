"""The publish.py delta-signature component hashes the transform closure, not the whole file:
a gate-only edit stays warm, an edit anywhere a declared root reaches goes cold."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_signature_transform_scope_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_BASE = '''\
import os

_TABLE = {"a": _helper_via_table}
_USED_BY_ROOT = 1
_GATE_ONLY = 2


def _helper_via_table():
    return "t"


def _helper():
    return _USED_BY_ROOT


def root():
    """doc"""
    _helper()
    return _TABLE


def gate():
    return _GATE_ONLY
'''
_ROOTS = ("root",)


def _sig(tmp_path: Path, source: str, roots: "tuple[str, ...]" = _ROOTS) -> str:
    path = tmp_path / "mod.py"
    path.write_text(source, encoding="utf-8")
    return publish._hash_transform_closure(path, roots)


def test_baseline_is_stable_and_not_whole_file(tmp_path: Path) -> None:
    sig = _sig(tmp_path, _BASE)
    assert sig == _sig(tmp_path, _BASE)
    assert sig != publish._hash_bytes_of(tmp_path / "mod.py")


def test_gate_body_edit_is_warm(tmp_path: Path) -> None:
    assert _sig(tmp_path, _BASE) == _sig(tmp_path, _BASE.replace("return _GATE_ONLY", "return _GATE_ONLY + 1"))


def test_new_constant_read_only_by_a_gate_is_warm(tmp_path: Path) -> None:
    edited = _BASE.replace("_GATE_ONLY = 2\n", "_GATE_ONLY = 2\n_NEW_GATE_RE = compile('x')\n").replace(
        "return _GATE_ONLY", "return _GATE_ONLY, _NEW_GATE_RE"
    )
    assert _sig(tmp_path, _BASE) == _sig(tmp_path, edited)


def test_comment_and_docstring_edits_are_warm(tmp_path: Path) -> None:
    edited = _BASE.replace('"""doc"""', '"""other doc"""').replace("def _helper():", "# note\ndef _helper():")
    assert _sig(tmp_path, _BASE) == _sig(tmp_path, edited)


def test_root_body_edit_is_cold(tmp_path: Path) -> None:
    assert _sig(tmp_path, _BASE) != _sig(tmp_path, _BASE.replace("    _helper()\n", "    _helper()\n    _helper()\n"))


def test_reached_helper_edit_is_cold(tmp_path: Path) -> None:
    assert _sig(tmp_path, _BASE) != _sig(tmp_path, _BASE.replace("return _USED_BY_ROOT", "return _USED_BY_ROOT + 1"))


def test_constant_read_by_a_reached_function_is_cold(tmp_path: Path) -> None:
    assert _sig(tmp_path, _BASE) != _sig(tmp_path, _BASE.replace("_USED_BY_ROOT = 1", "_USED_BY_ROOT = 9"))


def test_function_reached_only_through_a_dispatch_table_assignment_is_cold(tmp_path: Path) -> None:
    assert _sig(tmp_path, _BASE) != _sig(tmp_path, _BASE.replace('return "t"', 'return "u"'))


def test_new_top_level_import_is_cold(tmp_path: Path) -> None:
    assert _sig(tmp_path, _BASE) != _sig(tmp_path, _BASE.replace("import os\n", "import os\nimport re\n"))


def test_unattributable_module_level_statement_is_cold(tmp_path: Path) -> None:
    edited = _BASE + "\nif True:\n    _X = 1\n"
    assert _sig(tmp_path, _BASE) != _sig(tmp_path, edited)


def test_missing_root_is_cold_and_whole_file(tmp_path: Path) -> None:
    sig = _sig(tmp_path, _BASE, roots=("root", "renamed_away"))
    assert sig == publish._hash_bytes_of(tmp_path / "mod.py")
    assert sig != _sig(tmp_path, _BASE.replace("return _GATE_ONLY", "return 0"), roots=("root", "renamed_away"))


def test_syntax_error_falls_back_to_the_whole_file_hash(tmp_path: Path) -> None:
    assert _sig(tmp_path, "def root(:\n") == publish._hash_bytes_of(tmp_path / "mod.py")


def test_main_guard_does_not_pull_main_into_the_closure(tmp_path: Path) -> None:
    source = _BASE + '\n\ndef main():\n    return gate()\n\n\nif __name__ == "__main__":\n    main()\n'
    edited = source.replace("return _GATE_ONLY", "return _GATE_ONLY + 1")
    assert _sig(tmp_path, source) == _sig(tmp_path, edited)


def test_real_publish_py_gates_are_outside_the_closure() -> None:
    tree = ast.parse((_BIN_DIR / "publish.py").read_bytes())
    closure = publish._transform_closure_nodes(tree, publish._TRANSFORM_ROOTS)
    assert closure is not None, "a _TRANSFORM_ROOTS name no longer resolves to a top-level def"
    reached = set(closure[2])
    assert set(publish._TRANSFORM_ROOTS) <= reached
    for gate in ("preflight_version_surfaces", "describe_version_disagreement", "main", "run_pre_sync_gates"):
        assert gate not in reached
    assert not any(name.startswith("dispatch_end_of_run_") for name in reached)


def test_real_publish_py_does_not_fall_back_to_the_whole_file_hash() -> None:
    path = _BIN_DIR / "publish.py"
    assert publish._hash_transform_closure(path) != publish._hash_bytes_of(path)
