"""Structural no-write / no-subprocess assertion for the render path (C7, AC6).

Mirrors strang-03's C6 store-less-ness architecture-test
(``coordinator_core/ops/fleet/tests/test_memo_send.py::test_no_memo_index``):
a code assertion, not a convention, so a later chunk cannot quietly add a
write or a subprocess spawn to the render path and leave the
``scaffold``-last sequencing only nominally intact (see the plan's Anti-scope
and Out-of-scope: the terminal filesystem write is deferred to plan 2's
``doc.scaffold`` by read-before-write discipline).

Two independent legs, both load-bearing:

  - **Dynamic leg** — monkeypatch every write/spawn primitive reachable from
    ``render.py`` (``builtins.open`` in write/append/update modes,
    ``pathlib.Path.write_text``/``write_bytes``, ``os.system``,
    ``subprocess.Popen``/``run``/``call``/``check_call``/``check_output``) to
    raise if invoked, then render every one of the 22 staged types plus the
    in-memory ``render_template`` path. A render function that happens not to
    exercise a write today but retains the *capability* would still pass a
    purely-dynamic check if the call path weren't hit — the static leg closes
    that gap.
  - **Static leg** — AST-inspect ``render.py``'s own source: no
    ``import subprocess`` (or ``from subprocess import ...``), and no call
    whose callee resolves to ``open``/``.write_text``/``.write_bytes``
    anywhere in the module. This catches an added-but-untriggered-by-tests
    write/spawn that the dynamic leg's fixed input set wouldn't reach.

Spec backlink: pln-strang-12-document-generation--75a7eb § C7 (AC6)
"""

from __future__ import annotations

import ast
import builtins
import inspect
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.docgen import render as render_module
from coordinator_core.ops.docgen import template_format as tf
from coordinator_core.ops.docgen.render import render_document, render_template
from coordinator_core.ops.docgen.tests.test_doc_render import FULL_VALUES

_WRITE_MODE_CHARS = {"w", "a", "x", "+"}


def _forbid_open(*args, **kwargs):
    mode = kwargs.get("mode")
    if mode is None and len(args) > 1:
        mode = args[1]
    mode = mode or "r"
    if any(ch in mode for ch in _WRITE_MODE_CHARS):
        raise AssertionError(f"render path attempted a write-mode open(): mode={mode!r}")
    return _REAL_OPEN(*args, **kwargs)


_REAL_OPEN = builtins.open


def _forbid_write_text(self, *args, **kwargs):
    raise AssertionError(f"render path attempted Path.write_text on {self!r}")


def _forbid_write_bytes(self, *args, **kwargs):
    raise AssertionError(f"render path attempted Path.write_bytes on {self!r}")


def _forbid_subprocess(*args, **kwargs):
    raise AssertionError(f"render path attempted a subprocess spawn: args={args!r}")


def _forbid_os_system(*args, **kwargs):
    raise AssertionError(f"render path attempted os.system: args={args!r}")


@pytest.fixture
def no_write_no_subprocess(monkeypatch):
    """Fail loud if anything under test reaches a write or subprocess primitive."""
    monkeypatch.setattr(builtins, "open", _forbid_open)
    monkeypatch.setattr(Path, "write_text", _forbid_write_text)
    monkeypatch.setattr(Path, "write_bytes", _forbid_write_bytes)
    monkeypatch.setattr(os, "system", _forbid_os_system)
    for name in ("run", "call", "check_call", "check_output", "Popen"):
        monkeypatch.setattr(subprocess, name, _forbid_subprocess)
    yield


@pytest.mark.parametrize("doc_type", tf.available_template_types())
def test_render_document_performs_no_write_or_subprocess(no_write_no_subprocess, doc_type):
    output = render_document(doc_type, FULL_VALUES)
    assert isinstance(output, str)


def test_render_template_in_memory_performs_no_write_or_subprocess(no_write_no_subprocess):
    template = {
        "format_version": tf.FORMAT_VERSION,
        "doc_type": "inline-structural-test",
        "frontmatter": {
            "style": "fenced",
            "fields": [{"kind": "value", "key": "title", "field": "title", "quote": True}],
        },
        "body": [{"kind": "raw", "lines": ["hello {title}"]}],
    }
    output = render_template(template, {"title": "Inline"})
    assert "Inline" in output


def _collect_call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_render_module_source_imports_no_subprocess():
    source = inspect.getsource(render_module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name.split(".")[0] == "subprocess" for alias in node.names), (
                "render.py must not import subprocess"
            )
        if isinstance(node, ast.ImportFrom):
            assert node.module != "subprocess", "render.py must not import from subprocess"


def test_render_module_source_calls_no_write_or_subprocess_primitives():
    source = inspect.getsource(render_module)
    tree = ast.parse(source)
    call_names = _collect_call_names(tree)
    forbidden = {"write_text", "write_bytes", "system", "Popen", "run", "call", "check_call", "check_output"}
    hit = call_names & forbidden
    assert not hit, f"render.py source calls forbidden write/subprocess primitives: {hit}"
    # `open` is used (read-only, for template loading via template_format) — assert
    # every direct `open(...)` call site in this module is read-mode, not merely
    # absent, since a bare name-check above wouldn't distinguish read from write.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            mode_arg = None
            if len(node.args) > 1:
                mode_arg = node.args[1]
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode_arg = kw.value
            if mode_arg is not None and isinstance(mode_arg, ast.Constant):
                assert not any(ch in mode_arg.value for ch in _WRITE_MODE_CHARS), (
                    f"render.py has a write-mode open() call: {ast.dump(node)}"
                )
