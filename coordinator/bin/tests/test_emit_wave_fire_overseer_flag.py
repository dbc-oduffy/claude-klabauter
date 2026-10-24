"""`emit-wave-fire --overseer`: binds `overseer: true` into plan-blitz wave_args; absent flag leaves the key absent.

`_bind` is stubbed: this pins wave_args only, not plan-blitz's acceptance of `overseer`."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emit_wave_fire_overseer", _BIN / "emit-wave-fire.py")
ewf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ewf)

SIZING_REL = "state/sizings/s1.yaml"
BATON_REL = "state/handoffs/b1.md"
BATON = "---\nhandoff_id: hnd-1\ntitle: Minted baton\nstatus: open\n---\n\n# body\n"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-install"))
    monkeypatch.setattr(ewf, "_bind", lambda p, a, live=False: "const args = " + json.dumps(a) + ";\n")
    monkeypatch.setattr(
        ewf, "_load_mint",
        lambda: (lambda *a, **k: {"id": "hnd-1", "path": BATON_REL, "title": "Minted baton"}),
    )


def _fire(tmp_path, *extra):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "status": "sized", "route": "plan",
        "estimate": {"tshirt": "M"}, "interaction_mode": "pm", "intent": "do it",
        "exit_criterion": {"statement": "works", "accepted": {"pm_quote": "ok", "on": "2026-10-01", "mode": "pm"}},
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8")
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / BATON_REL).write_text(BATON, encoding="utf-8")
    (tmp_path / "trail").mkdir()
    (tmp_path / ".git").mkdir()
    root = tmp_path / "doctrine" / "coordinator"
    (root / "workflows").mkdir(parents=True)
    (root / "workflows" / "plan-blitz.mjs").write_text("// stub\n", encoding="utf-8")
    (root / "agents").mkdir()
    return ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        "--plugin-root", str(root), "--from-sizing", SIZING_REL, "--live-engine-tree",
        "--chain", "--json", *extra,
    ])


def test_overseer_flag_binds_true(tmp_path, capsys):
    assert _fire(tmp_path, "--overseer") == ewf.EXIT_OK
    manifest = json.loads(capsys.readouterr().out)["chain"]["manifest"]
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    assert data["wave_args"]["overseer"] is True


def test_without_flag_the_key_is_absent(tmp_path, capsys):
    assert _fire(tmp_path) == ewf.EXIT_OK
    manifest = json.loads(capsys.readouterr().out)["chain"]["manifest"]
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    assert "overseer" not in data["wave_args"]
