"""setup-chain-walker launcher: argv reaches `setup_chain_walker.main` verbatim, in one process."""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]
_ENTRY = _BIN / "setup-chain-walker.py"
_CMD = _BIN / "setup-chain-walker.cmd"
_INVENTORY = _BIN.parents[1] / "docs" / "install" / "bin-inventory.json"


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(_BIN))
    spec = importlib.util.spec_from_file_location("setup_chain_walker_launcher", _ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    from coordinator_core.ops import setup_chain_walker

    calls: list[list[str]] = []

    def fake_main(argv, out=None, err=None):
        calls.append(list(argv))
        return 7

    monkeypatch.setattr(setup_chain_walker, "main", fake_main)
    return calls


def test_argv_forwarded_verbatim_and_rc_returned(launcher, seen) -> None:
    argv = ["--coordinator-root", "/some root/x", "--check", "--coordinator-root=a b", "--", "!x%y"]
    assert launcher.main(argv) == 7
    assert seen == [argv]


def test_default_argv_is_sys_argv_tail(launcher, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["setup-chain-walker", "--coordinator-root", "/r"])
    assert launcher.main() == 7
    assert seen == [["--coordinator-root", "/r"]]


def test_module_body_imports_stdlib_only() -> None:
    tree = ast.parse(_ENTRY.read_text(encoding="utf-8"))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add(node.module or "")
    assert top <= {"__future__", "sys", "collections.abc"}


def test_no_second_interpreter_hop() -> None:
    assert "subprocess" not in _ENTRY.read_text(encoding="utf-8")


def test_cmd_runs_the_colocated_entrypoint_directly() -> None:
    body = _CMD.read_text(encoding="utf-8")
    assert '"%~dp0setup-chain-walker.py" %*' in body
    assert "__PYTHON_BIN__" in body
    assert "bash.exe" not in body.lower()


def test_enrolled_in_bin_inventory() -> None:
    inventory = json.loads(_INVENTORY.read_text(encoding="utf-8"))
    names = next(v for v in inventory.values() if isinstance(v, list) and "setup-verify" in v)
    assert "setup-chain-walker" in names
