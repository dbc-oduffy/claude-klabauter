"""Waiver keys are declared once, in their defining modules, and unioned in the package."""

import ast
from pathlib import Path

import coordinator_core.workstream_complete as wsc
from coordinator_core.workstream_complete import (
    directives_session_hygiene as hygiene,
    directives_spine_worklist as worklist,
)

_PKG = Path(wsc.__file__).parent


def test_each_module_waiver_keys_subset_of_free_value_keys():
    for mod in (hygiene, worklist):
        assert mod.WAIVER_KEYS
        assert set(mod.WAIVER_KEYS) <= set(mod.FREE_VALUE_KEYS)


def test_package_waiver_keys_is_ordered_union():
    expected = tuple(dict.fromkeys((*hygiene.WAIVER_KEYS, *worklist.WAIVER_KEYS)))
    assert wsc.WAIVER_KEYS == expected


def test_no_waiver_key_literal_outside_defining_modules():
    keys = set(wsc.WAIVER_KEYS)
    for name in ("apply.py", "__init__.py"):
        p = _PKG / name
        if not p.exists():
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in keys:
                raise AssertionError(f"{name}:{node.lineno} re-types waiver key {node.value!r}")
