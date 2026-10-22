"""emit-wave-fire refuses a nonzero --wave-index on a fresh per-wave gate report."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emit_wave_fire_widx", _BIN / "emit-wave-fire.py")
ewf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ewf)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-install"))


def _run(tmp_path, report_name, *extra):
    trail = tmp_path / "trail"
    trail.mkdir(exist_ok=True)
    (trail / report_name).write_text(
        json.dumps({"waves": [["b-ready-1", "b-ready-2"], ["b-dependent"]]}), encoding="utf-8"
    )
    root = tmp_path / "doctrine" / "coordinator"
    (root / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "workflows" / "plan-blitz.mjs").write_text("// stub\n", encoding="utf-8")
    return ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(trail),
        "--plugin-root", str(root), "--live-engine-tree", *extra,
    ])


def test_nonzero_wave_index_on_fresh_per_wave_report_is_refused(tmp_path, capsys):
    rc = _run(tmp_path, "wave-1.gate-report.json", "--wave-index", "1", "--wave-number", "1")
    err = capsys.readouterr().err
    assert rc == ewf.EXIT_REFUSED
    assert "--wave-index 0" in err
    assert not list((tmp_path / "trail").glob("fire-*.mjs"))


def test_wave_index_zero_on_per_wave_report_passes_the_check(tmp_path, capsys):
    _run(tmp_path, "wave-1.gate-report.json", "--wave-index", "0", "--wave-number", "1")
    assert "own fresh report" not in capsys.readouterr().err


def test_explicit_gate_report_keeps_nonzero_wave_index(tmp_path, capsys):
    _run(
        tmp_path, "wave-1.gate-report.json", "--wave-index", "1", "--wave-number", "1",
        "--gate-report", str(tmp_path / "trail" / "wave-1.gate-report.json"),
    )
    assert "own fresh report" not in capsys.readouterr().err


def test_legacy_report_keeps_nonzero_wave_index(tmp_path, capsys):
    _run(tmp_path, "gate-report.json", "--wave-index", "1", "--wave-number", "1")
    assert "own fresh report" not in capsys.readouterr().err
