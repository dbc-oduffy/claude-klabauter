"""machine-local.py entrypoint: trampoline over templates/bin/_machine_local.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ENTRY = Path(__file__).resolve().parents[1] / "machine-local.py"


@pytest.fixture
def entry(tmp_path, monkeypatch):
    (tmp_path / "registry.toml").write_text('[demo]\nfoo = "bar"\n', encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("machine_local_entry", _ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_keys_lists_key(entry, capsys):
    assert entry.main(["keys"]) == 0
    assert "demo.foo" in capsys.readouterr().out.split()


def test_get_prints_value(entry, capsys):
    assert entry.main(["get", "demo.foo"]) == 0
    assert capsys.readouterr().out.strip() == "bar"


def test_has_missing_returns_1(entry):
    assert entry.main(["has", "demo.missing"]) == 1


def test_help_returns_0(entry):
    assert entry.main(["--help"]) == 0


def test_argv_restored(entry):
    before = list(sys.argv)
    entry.main(["keys"])
    entry.main(["has", "demo.missing"])
    entry.main(["--help"])
    assert sys.argv == before


def test_no_execv():
    assert "execv" not in _ENTRY.read_text(encoding="utf-8")
