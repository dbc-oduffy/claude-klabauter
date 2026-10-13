"""row_build_gate leaf: tolerant parse, glob semantics, change_kind filtering."""

import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit.row_build_gate import (
    gate_commands_for,
    glob_regex,
    parse_row_build_gates,
)


def _plan(fm: str) -> str:
    return f"---\ntitle: P\n{fm}---\n\n# P\n"


def test_parse_reads_when_and_command():
    text = _plan(
        "row_build_gate:\n"
        "  - when: {surface_glob: 'Source/**/*.cpp', change_kind: code-edit}\n"
        "    command: '  build.bat  '\n"
        "  - command: 'always'\n"
    )
    assert parse_row_build_gates(text) == (
        ("code-edit", "Source/**/*.cpp", "build.bat"),
        (None, None, "always"),
    )


def test_parse_is_tolerant():
    assert parse_row_build_gates("no frontmatter") == ()
    assert parse_row_build_gates(_plan("title2: x\n")) == ()
    assert parse_row_build_gates(_plan("row_build_gate: [unclosed\n")) == ()
    assert parse_row_build_gates(_plan("row_build_gate: notalist\n")) == ()
    text = _plan(
        "row_build_gate:\n"
        "  - just a string\n"
        "  - {command: ''}\n"
        "  - {command: 5}\n"
        "  - {when: oops, command: 'ok'}\n"
    )
    assert parse_row_build_gates(text) == ((None, None, "ok"),)


def test_glob_semantics():
    assert glob_regex("Source/**/*.cpp").match("Source/a/b/c.cpp")
    assert glob_regex("Source/**/*.cpp").match("Source/c.cpp")
    assert not glob_regex("Source/*.cpp").match("Source/a/c.cpp")
    assert glob_regex("a?.py").match("ab.py")
    assert not glob_regex("a?.py").match("a/.py")
    assert glob_regex("docs/**").match("docs/x/y.md")
    assert not glob_regex("a.py").match("axpy")
    assert not glob_regex("a.py").match("a.pyc")


def test_gate_commands_filter_by_kind_and_glob():
    gates = (
        (None, "Source/**/*.cpp", "build"),
        ("doc-edit", None, "echo docs"),
        ("doc-edit", None, "echo docs"),
        (None, None, "all"),
    )
    assert gate_commands_for(["Source/a/b.cpp"], "code-edit", gates) == ["build", "all"]
    assert gate_commands_for(["README.md"], "doc-edit", gates) == ["echo docs", "all"]
    assert gate_commands_for(["README.md"], "code-edit", gates) == ["all"]


def test_gate_commands_normalise_backslashes_and_empty():
    gates = ((None, "Source/**/*.cpp", "build"),)
    assert gate_commands_for(["Source\\a\\b.cpp"], None, gates) == ["build"]
    assert gate_commands_for([], None, gates) == []
    assert gate_commands_for(["x"], None, ()) == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_leaf_does_not_import_emit():
    code = (
        "import sys; import coordinator_core.ops.dispatch_emit.row_build_gate; "
        "sys.exit(1 if 'coordinator_core.ops.dispatch_emit.emit' in sys.modules else 0)"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    assert r.returncode == 0
