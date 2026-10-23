"""session-spend-report resolves engine root and provenance from a klabauter-shaped tree
that carries no coordinator/hooks/scripts/_engine_root.py."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]


def _mirror(tmp_path: Path) -> Path:
    mirror = tmp_path / "klabauter"
    (mirror / "coordinator").mkdir(parents=True)
    shutil.copytree(BIN, mirror / "coordinator" / "bin",
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    (mirror / "coordinator_core").mkdir()
    (mirror / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return mirror


def _load(mirror: Path):
    path = mirror / "coordinator" / "bin" / "session-spend-report.py"
    spec = importlib.util.spec_from_file_location("ssr_mirror", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def _isolate(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in ("cc_invoke", "session_spend", "engine_bootstrap", "_engine_root"):
        monkeypatch.delitem(sys.modules, name, raising=False)


def test_report_resolves_root_without_engine_root_module(tmp_path, monkeypatch, capsys):
    mirror = _mirror(tmp_path)
    assert not (mirror / "coordinator" / "hooks" / "scripts" / "_engine_root.py").exists()
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(mirror))
    _isolate(monkeypatch)
    mod, path = _load(mirror)
    assert mod.main([str(path), str(transcript)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert Path(report["engine_root"]) == mirror
    assert report["engine_provenance"] is not None
    assert "_engine_root" not in sys.modules


def test_missing_transcript_still_exits_2(tmp_path, monkeypatch):
    mirror = _mirror(tmp_path)
    _isolate(monkeypatch)
    mod, path = _load(mirror)
    assert mod.main([str(path), str(tmp_path / "nope.jsonl")]) == 2
