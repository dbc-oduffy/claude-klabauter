"""Tests for coordinator_core.pyimports."""

import ast

from coordinator_core.pyimports import (
    ImportRecord,
    collect_imports,
    module_name_for_file,
    resolve_relative_module,
)


def _collect(src):
    return collect_imports(ast.parse(src))


def test_absolute_imports():
    records, dyn = _collect("import a.b\nfrom c import d, e\n")
    assert records == [ImportRecord("a.b", (), 0, 1), ImportRecord("c", ("d", "e"), 0, 2)]
    assert dyn is False


def test_relative_levels_recorded():
    records, _ = _collect("from . import x\nfrom .. import y\nfrom ...z import w\n")
    assert [(r.module, r.names, r.level) for r in records] == [
        ("", ("x",), 1),
        ("", ("y",), 2),
        ("z", ("w",), 3),
    ]


def test_resolve_relative_levels_1_to_3():
    assert resolve_relative_module("x", 1, "a.b.c", False) == "a.b.x"
    assert resolve_relative_module("x", 2, "a.b.c", False) == "a.x"
    assert resolve_relative_module("x", 3, "a.b.c.d", False) == "a.x"
    assert resolve_relative_module("x", 1, "a.b", True) == "a.b.x"
    assert resolve_relative_module("", 1, "a.b", True) == "a.b"


def test_resolve_relative_climb_past_root_and_unknown_module():
    assert resolve_relative_module("x", 3, "a.b", False) is None
    assert resolve_relative_module("x", 1, None, False) is None


def test_type_checking_body_skipped_else_walked():
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    import only_types\nelse:\n    import runtime\n"
        "import typing\nif typing.TYPE_CHECKING:\n    import also_types\n"
    )
    mods = [r.module for r in _collect(src)[0]]
    assert "only_types" not in mods and "also_types" not in mods
    assert "runtime" in mods


def test_dynamic_import_flagged():
    assert _collect("import importlib\nimportlib.import_module('x')\n")[1] is True
    assert _collect("__import__('x')\n")[1] is True
    assert _collect("import runpy\nrunpy.run_path('x')\n")[1] is True
    assert _collect("import os\nos.path.join('a')\n")[1] is False


def test_module_name_for_file():
    assert module_name_for_file("a/b/c.py") == "a.b.c"
    assert module_name_for_file("a/b/__init__.py") == "a.b"
    assert module_name_for_file("a\\b\\c.py") == "a.b.c"
    assert module_name_for_file("a\\b\\__init__.py") == "a.b"
    assert module_name_for_file("bin/tool") is None
