"""fan-out-dispatch's pinned-interface check matches a definition, not a substring."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "fan_out_dispatch_pin", str(_BIN_DIR / "fan-out-dispatch.py")
    )
    spec = importlib.util.spec_from_loader("fan_out_dispatch_pin", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def defines():
    return _load()._defines_symbol


def test_python_def_class_and_assignment_are_definitions(defines):
    src = "def foo():\n    pass\nclass Bar:\n    pass\nBAZ = 1\nasync def qux():\n    pass\n"
    for name in ("foo", "Bar", "BAZ", "qux"):
        assert defines("m.py", src, name)


def test_python_comment_or_call_mention_is_not_a_definition(defines):
    src = "# TODO: foo is coming\nx = foo()\nprint('foo')\n"
    assert not defines("m.py", src, "foo")


def test_python_nested_def_is_not_top_level(defines):
    assert not defines("m.py", "class A:\n    def foo(self):\n        pass\n", "foo")


def test_unparseable_python_falls_back_to_the_keyword_pattern(defines):
    assert defines("m.py", "def foo(:\n", "foo")
    assert not defines("m.py", "# foo\ndef (:\n", "foo")


def test_other_languages_need_a_definition_keyword(defines):
    assert defines("m.ts", "export function foo() {}\n", "foo")
    assert defines("m.ts", "const foo = 1;\n", "foo")
    assert defines("m.rs", "pub fn foo() {}\n", "foo")
    assert not defines("m.ts", "// foo later\nbar(foo);\n", "foo")
    assert not defines("m.ts", "function foobar() {}\n", "foo")
