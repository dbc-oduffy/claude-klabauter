"""gem-triage-turn-intent places the engine from a klabauter-shaped tree with no _engine_root."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent
_ENGINE = _BIN.parent.parent


def test_places_engine_without_engine_root_module(tmp_path, monkeypatch):
    mirror_bin = tmp_path / "coordinator" / "bin"
    mirror_bin.mkdir(parents=True)
    shutil.copy(_BIN / "gem-triage-turn-intent.py", mirror_bin / "gem-triage-turn-intent.py")
    shutil.copytree(_BIN / "lib", mirror_bin / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    assert not (tmp_path / "coordinator" / "hooks" / "scripts" / "_engine_root.py").exists()

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "engine_bootstrap", raising=False)
    spec = importlib.util.spec_from_file_location("gtti_mirror", mirror_bin / "gem-triage-turn-intent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "import _engine_root" not in (mirror_bin / "gem-triage-turn-intent.py").read_text(encoding="utf-8")

    import engine_bootstrap  # resolved from the mirror's lib via the placement

    monkeypatch.setattr(engine_bootstrap, "_resolve_engine_root", lambda caller_file=None: str(_ENGINE))
    mod._place_engine_on_path()
    assert str(_ENGINE) in sys.path
    assert str(mirror_bin / "lib") in sys.path


def test_unresolved_engine_raises(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gtti_live", _BIN / "gem-triage-turn-intent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "path", list(sys.path))
    import engine_bootstrap

    monkeypatch.setattr(engine_bootstrap, "_resolve_engine_root", lambda caller_file=None: "")
    with pytest.raises(RuntimeError):
        mod._place_engine_on_path()
