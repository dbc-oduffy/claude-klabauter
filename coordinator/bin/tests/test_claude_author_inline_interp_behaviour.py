"""
Behaviour pin: `claude-author.py`'s inline interpreter-resolution ladder.

Purpose: `claude-author.py` ships STANDALONE (see its `_machine_local_argv`
docstring) and cannot import a sibling `lib/` module, so it carries its own
`_is_console_python_basename` / `_resolve_console_python` inline. This pins the
ladder's contract directly -- console CPython only, never a `pythonw` or a
non-python launcher exe; `sys.executable`, then `sys._base_executable`, then
`python3`/`python` on PATH -- so a regression in the inline copy fails here.
`test_claude_author_inline_interp_parity.py` pins the same ladder against
`lib/python_interp.py` structurally; this file pins what it does.

Negative spec: no subprocess spawn; the ladder is exercised through
monkeypatched `sys` / `shutil.which`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CLAUDE_AUTHOR_PATH = Path(__file__).resolve().parents[1] / "claude-author.py"


@pytest.fixture(scope="module")
def claude_author():
    spec = importlib.util.spec_from_file_location("_claude_author_interp_under_test", CLAUDE_AUTHOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/usr/bin/python3", True),
        ("/usr/bin/python3.12", True),
        ("Python312\\python.exe", True),
        ("Python312\\Python.EXE", True),
        ("Python312\\pythonw.exe", False),
        ("Tools\\launcher.exe", False),
        ("/usr/bin/node", False),
        ("", False),
    ],
)
def test_console_python_basename(claude_author, path, expected) -> None:
    assert claude_author._is_console_python_basename(path) is expected


def test_prefers_sys_executable_when_it_is_console_python(claude_author, monkeypatch) -> None:
    monkeypatch.setattr(sys, "executable", "/opt/py/python3")
    assert claude_author._resolve_console_python() == "/opt/py/python3"


def test_falls_back_to_base_executable(claude_author, monkeypatch) -> None:
    monkeypatch.setattr(sys, "executable", "/opt/launcher/forwarder.exe")
    monkeypatch.setattr(sys, "_base_executable", "/opt/base/python3", raising=False)
    assert claude_author._resolve_console_python() == "/opt/base/python3"


def test_falls_back_to_path_lookup_python3_before_python(claude_author, monkeypatch) -> None:
    monkeypatch.setattr(sys, "executable", "/opt/launcher/forwarder.exe")
    monkeypatch.setattr(sys, "_base_executable", "/opt/launcher/other.exe", raising=False)
    seen: list[str] = []

    def fake_which(name):
        seen.append(name)
        return {"python3": "/usr/bin/python3", "python": "/usr/bin/python"}.get(name)

    monkeypatch.setattr(claude_author.shutil, "which", fake_which)
    assert claude_author._resolve_console_python() == "/usr/bin/python3"
    assert seen == ["python3"]


def test_returns_none_when_nothing_resolves(claude_author, monkeypatch) -> None:
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.setattr(sys, "_base_executable", "", raising=False)
    monkeypatch.setattr(claude_author.shutil, "which", lambda name: None)
    assert claude_author._resolve_console_python() is None
