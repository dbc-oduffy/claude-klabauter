"""Targeted tests for the import-context regex prefilter in
`coordinator_core.source_edit_gate.selection` -- the replacement for the
raw-substring `_leaf_tokens` prefilter that forced an `ast.parse` on any
file merely mentioning a common leaf name (`derive`, `stamp`) in prose.

Covers: multi-line parenthesised `from ... import (...)`, a relative
import naming the leaf as the module itself, a dynamic
`importlib.import_module("...")` string, and the negative case -- a leaf
name appearing only in prose must neither match the prefilter regex nor
reach `ast.parse`/selection.
"""

from __future__ import annotations

from unittest.mock import patch

from coordinator_core.source_edit_gate.selection import (
    _build_import_prefilter_regex,
    select_test_files,
)


def _regex_for(leaf: str):
    return _build_import_prefilter_regex({leaf})


def test_multiline_parenthesized_from_import_matches():
    regex = _regex_for("derive")
    source = (
        "from pkg import (\n"
        "    foo,\n"
        "    derive,\n"
        "    bar,\n"
        ")\n"
    )
    assert regex.search(source)


def test_relative_import_naming_leaf_as_module_matches():
    regex = _regex_for("derive")
    assert regex.search("from .derive import thing\n")
    assert regex.search("from ..pkg.derive import thing\n")


def test_importlib_import_module_string_matches():
    regex = _regex_for("derive")
    assert regex.search('importlib.import_module("pkg.sub.derive")\n')
    assert regex.search('__import__("pkg.sub.derive")\n')


def test_leaf_in_prose_does_not_match():
    regex = _regex_for("derive")
    assert not regex.search("# we derive the value from configuration here\n")
    assert not regex.search("derived = compute_derived_value()\n")


def test_prose_only_file_not_parsed_and_not_selected(tmp_path):
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "derive.py").write_text("x = 1\n")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    prose_test = tests_dir / "test_prose.py"
    prose_test.write_text(
        "def test_thing():\n"
        "    # a derived quantity, unrelated to any import\n"
        "    assert True\n"
    )
    real_import_test = tests_dir / "test_real_import.py"
    real_import_test.write_text(
        "from pkg.derive import (\n"
        "    something,\n"
        "    another,\n"
        ")\n\n"
        "def test_thing():\n"
        "    assert something\n"
    )

    all_files = [
        "pkg/derive.py",
        "tests/test_prose.py",
        "tests/test_real_import.py",
    ]

    with patch(
        "coordinator_core.source_edit_gate.selection._python_imports",
        wraps=__import__(
            "coordinator_core.source_edit_gate.selection", fromlist=["_python_imports"]
        )._python_imports,
    ) as spy:
        selected = select_test_files(str(repo), ["pkg/derive.py"], all_files)

    parsed_paths = {call.args[0] for call in spy.call_args_list}
    assert prose_test.read_text() not in parsed_paths
    assert "tests/test_real_import.py" in selected
    assert "tests/test_prose.py" not in selected
