"""box-memory-snapshot.py resolves coordinator_core from the engine root, not from the checkout it sits in."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]


def _mirror(tmp_path: Path) -> Path:
    """A non-engine checkout: the script and bin/lib, no coordinator_core beside them."""
    mirror_bin = tmp_path / "mirror" / "coordinator" / "bin"
    mirror_bin.mkdir(parents=True)
    shutil.copy2(BIN / "box-memory-snapshot.py", mirror_bin / "box-memory-snapshot.py")
    shutil.copytree(BIN / "lib", mirror_bin / "lib", ignore=shutil.ignore_patterns("__pycache__", "test_*"))
    return mirror_bin


def _engine(tmp_path: Path) -> Path:
    engine = tmp_path / "engine"
    (engine / "coordinator_core").mkdir(parents=True)
    (engine / "coordinator_core" / "__init__.py").write_text("ENGINE_MARK = True\n", encoding="utf-8")
    return engine


def _isolate_imports(monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in [m for m in sys.modules if m.split(".")[0] in ("lib", "cc_invoke", "engine_bootstrap", "coordinator_core")]:
        monkeypatch.delitem(sys.modules, name)


def _load(mirror_bin: Path):
    spec = importlib.util.spec_from_file_location("bms_mirror", mirror_bin / "box-memory-snapshot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admit_peak_import_comes_from_the_resolved_engine_root(tmp_path, monkeypatch):
    mirror_bin = _mirror(tmp_path)
    engine = _engine(tmp_path)
    assert not (mirror_bin.parents[1] / "coordinator_core").exists()
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(engine))
    _isolate_imports(monkeypatch)
    mod = _load(mirror_bin)

    mod._place_engine_on_path()

    import coordinator_core

    assert Path(coordinator_core.__file__).resolve().is_relative_to(engine.resolve())
    assert str(mirror_bin.parents[1]) not in sys.path


def test_the_mirror_checkout_is_not_a_fallback_root(tmp_path, monkeypatch):
    mirror_bin = _mirror(tmp_path)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tmp_path / "no-such-engine"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    _isolate_imports(monkeypatch)
    mod = _load(mirror_bin)

    mod._place_engine_on_path()

    assert str(mirror_bin.parents[1]) not in sys.path
