"""`emit-wave-fire --from-sizing`: chain by default for pm/ceo, `--plan-only` escape; manifest, printed driver call, trigger refusals."""

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


def test_pm_chains_by_default(tmp_path, capsys):
    assert _fire(tmp_path, _sizing()) == ewf.EXIT_OK
    assert (tmp_path / "trail" / "chain-0-hnd-1.json").is_file()
    assert not list((tmp_path / "trail").glob("*.mjs"))


def test_ceo_chains_by_default(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(mode="ceo")) == ewf.EXIT_OK
    assert (tmp_path / "trail" / "chain-0-hnd-1.json").is_file()


def test_hands_on_is_plan_only(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(mode="hands-on")) == ewf.EXIT_OK
    assert (tmp_path / "trail" / "fire-0-1.mjs").is_file()
    assert not (tmp_path / "trail" / "chain-0-hnd-1.json").exists()


def test_pm_plan_only_flag_keeps_the_plan_only_fire(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(), "--plan-only") == ewf.EXIT_OK
    assert (tmp_path / "trail" / "fire-0-1.mjs").is_file()
    assert not (tmp_path / "trail" / "chain-0-hnd-1.json").exists()


def test_pm_xl_falls_back_to_plan_only_by_default(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(tshirt="XL")) == ewf.EXIT_OK
    assert (tmp_path / "trail" / "fire-0-1.mjs").is_file()


def test_chain_and_plan_only_conflict(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(), "--chain", "--plan-only") == ewf.EXIT_REFUSED


def test_chain_writes_manifest_and_prints_the_call(tmp_path, capsys):
    assert _fire(tmp_path, _sizing(), "--chain") == ewf.EXIT_OK
    manifest = (tmp_path / "trail" / "chain-0-hnd-1.json").resolve()
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
    assert out["chain"]["manifest"].endswith("chain-0-hnd-1.json")


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


def _fire_second(tmp_path, monkeypatch, n):
    """Fire sizing/baton `n` into the shared trail dir; scaffolding is idempotent."""
    sizing_rel, baton_rel = f"state/sizings/s{n}.yaml", f"state/handoffs/b{n}.md"
    for d in ("state/sizings", "state/handoffs", "trail", ".git", "doctrine/coordinator/workflows",
              "doctrine/coordinator/agents"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / sizing_rel).write_text(yaml.safe_dump(_sizing()), encoding="utf-8")
    (tmp_path / baton_rel).write_text(BATON.replace("hnd-1", f"hnd-{n}"), encoding="utf-8")
    (tmp_path / "doctrine/coordinator/workflows/plan-blitz.mjs").write_text("// stub\n", encoding="utf-8")
    monkeypatch.setattr(
        ewf, "_load_mint",
        lambda: (lambda *a, **k: {"id": f"hnd-{n}", "path": baton_rel, "title": "Minted baton"}),
    )
    return ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        "--plugin-root", str(tmp_path / "doctrine/coordinator"), "--from-sizing", sizing_rel,
        "--live-engine-tree", "--chain",
    ])


def test_two_sizings_in_one_trail_keep_both_manifests(tmp_path, monkeypatch, capsys):
    assert _fire_second(tmp_path, monkeypatch, 1) == ewf.EXIT_OK
    assert _fire_second(tmp_path, monkeypatch, 2) == ewf.EXIT_OK
    first = json.loads((tmp_path / "trail" / "chain-0-hnd-1.json").read_text(encoding="utf-8"))
    second = json.loads((tmp_path / "trail" / "chain-0-hnd-2.json").read_text(encoding="utf-8"))
    assert first["sizing_object"] == "state/sizings/s1.yaml"
    assert second["sizing_object"] == "state/sizings/s2.yaml"


def test_refiring_the_same_chain_is_idempotent(tmp_path, monkeypatch, capsys):
    assert _fire_second(tmp_path, monkeypatch, 1) == ewf.EXIT_OK
    assert _fire_second(tmp_path, monkeypatch, 1) == ewf.EXIT_OK


def test_a_different_chain_does_not_overwrite_a_manifest(tmp_path, monkeypatch, capsys):
    assert _fire_second(tmp_path, monkeypatch, 1) == ewf.EXIT_OK
    squatter = tmp_path / "trail" / "chain-0-hnd-2.json"
    squatter.write_text(
        (tmp_path / "trail" / "chain-0-hnd-1.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert _fire_second(tmp_path, monkeypatch, 2) == ewf.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "state/sizings/s1.yaml" in err and "state/sizings/s2.yaml" in err
    assert "state/sizings/s1.yaml" in squatter.read_text(encoding="utf-8")
