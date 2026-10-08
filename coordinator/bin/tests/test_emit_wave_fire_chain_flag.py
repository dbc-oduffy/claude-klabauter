"""`emit-wave-fire --from-sizing`: the in-session fire by default, headless chain only on `--chain` with a warning; manifest, printed driver call, trigger refusals."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emit_wave_fire_chain", _BIN / "emit-wave-fire.py")
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


def _sizing(tshirt="M", mode="pm", route="plan"):
    return {
        "schema": "sizing-object", "status": "sized", "route": route,
        "estimate": {"tshirt": tshirt}, "interaction_mode": mode, "intent": "do it",
        "exit_criterion": {"statement": "works", "accepted": {"pm_quote": "ok", "on": "2026-10-01", "mode": mode}},
    }


def _fire(tmp_path, sizing, *extra):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
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
        "--plugin-root", str(root), "--from-sizing", SIZING_REL, "--live-engine-tree", *extra,
    ])


@pytest.mark.parametrize("mode", ["pm", "ceo"])
def test_pm_and_ceo_fire_in_session_by_default(tmp_path, capsys, mode):
    assert _fire(tmp_path, _sizing(mode=mode)) == ewf.EXIT_OK
    assert (tmp_path / "trail" / "fire-0-1.mjs").is_file()
    assert not (tmp_path / "trail" / "chain-0-1.json").exists()


def test_chain_warns_that_it_runs_headless(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(), "--chain") == ewf.EXIT_OK
    assert "WARNING: --chain runs every stage as a headless background child" in capsys.readouterr().err


def test_hands_on_is_plan_only(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(mode="hands-on")) == ewf.EXIT_OK
    assert (tmp_path / "trail" / "fire-0-1.mjs").is_file()
    assert not (tmp_path / "trail" / "chain-0-1.json").exists()


def test_pm_plan_only_flag_keeps_the_plan_only_fire(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(), "--plan-only") == ewf.EXIT_OK
    assert (tmp_path / "trail" / "fire-0-1.mjs").is_file()
    assert not (tmp_path / "trail" / "chain-0-1.json").exists()


def test_pm_xl_falls_back_to_plan_only_by_default(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(tshirt="XL")) == ewf.EXIT_OK
    assert (tmp_path / "trail" / "fire-0-1.mjs").is_file()


def test_chain_and_plan_only_conflict(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(), "--chain", "--plan-only") == ewf.EXIT_REFUSED


def test_chain_writes_manifest_and_prints_the_call(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(), "--chain") == ewf.EXIT_OK
    manifest = (tmp_path / "trail" / "chain-0-1.json").resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["sizing_object"] == SIZING_REL
    assert data["baton"] == BATON_REL
    assert data["interaction_mode"] == "pm"
    assert data["wave_args"]["mode"] == "single"
    assert data["wave_args"]["batons"][0]["id"] == "hnd-1"
    out = capsys.readouterr().out
    assert "emit-wave-fire: chain fire for hnd-1 (mode=single, chain)." in out
    assert f'  Bash(run_in_background: true): plan-chain-run --manifest "{manifest}"   # its exit is your wake' in out
    assert not list((tmp_path / "trail").glob("*.mjs"))


def test_chain_json_shape(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(), "--chain", "--json") == ewf.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["waveIndex"] == 0
    assert out["chain"]["batons"] == ["hnd-1"]
    assert out["chain"]["manifest"].endswith("chain-0-1.json")


@pytest.mark.parametrize("sizing,field", [
    (_sizing(mode="hands-on"), "interaction_mode"),
    (_sizing(tshirt="XL"), "estimate.tshirt"),
    (_sizing(route="spec-dispatch"), "route"),
])
def test_trigger_refusals_name_their_field(tmp_path, capsys, sizing, field):
    assert _fire(tmp_path, sizing, "--chain") == ewf.EXIT_REFUSED
    assert field in capsys.readouterr().err


def test_chain_without_from_sizing_is_refused(tmp_path, capsys):
    rc = ewf.main(["--repo-root", str(tmp_path), "--trail-dir", str(tmp_path), "--chain"])
    assert rc == ewf.EXIT_REFUSED
    assert "--from-sizing" in capsys.readouterr().err


def test_plan_only_fire_still_writes_the_mjs(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(), "--plan-only") == ewf.EXIT_OK
    assert (tmp_path / "trail" / "fire-0-1.mjs").is_file()
    assert "single-plan fire for hnd-1" in capsys.readouterr().out
