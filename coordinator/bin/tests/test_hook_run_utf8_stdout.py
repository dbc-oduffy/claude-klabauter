"""hook-run writes stdout as utf-8 whatever the console code page is."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def hook_run():
    spec = importlib.util.spec_from_file_location("hook_run_utf8_under_test", _BIN / "hook-run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cp1252_stdout() -> "tuple[io.TextIOWrapper, io.BytesIO]":
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True), raw


def test_cp1252_stdout_raises_without_the_reconfigure():
    out, _ = _cp1252_stdout()
    with pytest.raises(UnicodeEncodeError):
        out.write("→")


def test_stdout_utf8_makes_non_cp1252_text_writable(hook_run, monkeypatch):
    out, raw = _cp1252_stdout()
    monkeypatch.setattr(sys, "stdout", out)
    hook_run._stdout_utf8()
    sys.stdout.write("→ ünï")
    sys.stdout.flush()
    assert raw.getvalue().decode("utf-8") == "→ ünï"


def test_stdout_without_reconfigure_is_left_alone(hook_run, monkeypatch):
    stub = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stub)
    hook_run._stdout_utf8()
    assert sys.stdout is stub


def test_main_reconfigures_before_any_write(hook_run, monkeypatch, tmp_path):
    names = tmp_path / "names.txt"
    names.write_text("ünï→name\n", encoding="utf-8")
    out, raw = _cp1252_stdout()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(hook_run, "_resolve_engine_identity", lambda: ("root", "0" * 12))
    import types

    registry = types.ModuleType("coordinator_core.ops._registry_map")
    registry.resolves = lambda name: False
    monkeypatch.setitem(sys.modules, "coordinator_core.ops._registry_map", registry)
    code = hook_run.main(["hook-run", "--check-all", "@" + str(names)])
    sys.stdout.flush()
    assert code == 2
    assert "ünï→name" in raw.getvalue().decode("utf-8")
