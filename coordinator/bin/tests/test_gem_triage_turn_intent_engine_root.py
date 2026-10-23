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


def _load_mirror(tmp_path, monkeypatch):
    mirror_bin = tmp_path / "coordinator" / "bin"
    mirror_bin.mkdir(parents=True)
    shutil.copy(_BIN / "gem-triage-turn-intent.py", mirror_bin / "gem-triage-turn-intent.py")
    shutil.copytree(_BIN / "lib", mirror_bin / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    assert not (tmp_path / "coordinator" / "hooks").exists()
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "engine_bootstrap", raising=False)
    spec = importlib.util.spec_from_file_location("gtti_mirror2", mirror_bin / "gem-triage-turn-intent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import engine_bootstrap

    monkeypatch.setattr(engine_bootstrap, "_resolve_engine_root", lambda caller_file=None: str(_ENGINE))
    return mod


def test_holder_lookup_runs_from_mirror_tree(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    mod = _load_mirror(tmp_path, monkeypatch)
    monkeypatch.setattr(gem_home, "gem_holder", lambda: {"session_id": "other", "live": True})
    rc = mod.main(
        ["--session-id", "s1", "--repo-root", str(tmp_path), "--turn-ended-at", "1.0", "--outcome", "cleared", "--caller-session-id", "me"]
    )
    err = capsys.readouterr().err
    assert rc == 6
    assert "caller is not the live G-EM holder" in err
    assert "unresolvable" not in err


def test_gem_holder_contract(monkeypatch):
    from coordinator_core.group_em import gem_home, nomination

    live = {"session_id": "a", "live": True}
    monkeypatch.setattr(nomination, "box_holder", lambda: live)
    assert gem_home.gem_holder() == live
    monkeypatch.setattr(nomination, "box_holder", lambda: {"session_id": "a", "live": False})
    assert gem_home.gem_holder() is None
    monkeypatch.setattr(nomination, "box_holder", lambda: None)
    assert gem_home.gem_holder() is None

    def boom():
        raise OSError("x")

    monkeypatch.setattr(nomination, "box_holder", boom)
    assert gem_home.gem_holder() is None


def test_script_has_no_legacy_imports():
    text = (_BIN / "gem-triage-turn-intent.py").read_text(encoding="utf-8")
    assert "import _gem_home" not in text
    assert "hooks" + "/scripts" not in text and "TRAP" not in text


def test_unresolved_engine_raises(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gtti_live", _BIN / "gem-triage-turn-intent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "path", list(sys.path))
    import engine_bootstrap

    monkeypatch.setattr(engine_bootstrap, "_resolve_engine_root", lambda caller_file=None: "")
    with pytest.raises(RuntimeError):
        mod._place_engine_on_path()
