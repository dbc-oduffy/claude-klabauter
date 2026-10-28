"""`emit-wave-fire` binds the plugin root's `plan_review` block into every emitted fire's args.

`_bind` is stubbed: this pins wave_args only. The planned repo carries no roster fragment."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emit_wave_fire_plan_review", _BIN / "emit-wave-fire.py")
ewf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ewf)

SIZING_REL = "state/sizings/s1.yaml"
BATON_REL = "state/handoffs/b1.md"
BATON = "---\nhandoff_id: hnd-1\ntitle: Minted baton\nstatus: open\n---\n\n# body\n"
BLOCK = {"tiers": {"M": {"reviewers": [{"agentType": "coordinator:staff-eng", "model": "opus"}]}}}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-install"))
    monkeypatch.setattr(ewf, "_bind", lambda p, a, live=False: "const args = " + json.dumps(a) + ";\n")
    monkeypatch.setattr(
        ewf, "_load_mint",
        lambda: (lambda *a, **k: {"id": "hnd-1", "path": BATON_REL, "title": "Minted baton"}),
    )


def _fire(tmp_path, fragment, *extra):
    """Run a single-baton emit; `fragment` is the plugin-root fragment text, or None for no file."""
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
    if fragment is not None:
        (root / "contract").mkdir()
        (root / "contract" / "review-roster-fragment.json").write_text(fragment, encoding="utf-8")
    return ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        "--plugin-root", str(root), "--from-sizing", SIZING_REL, "--live-engine-tree",
        "--json", *extra,
    ])


def _fired_args(tmp_path):
    text = next((tmp_path / "trail").glob("fire-*.mjs")).read_text(encoding="utf-8")
    return json.loads(text.split("const args = ", 1)[1].rsplit(";", 1)[0])


def test_single_fire_binds_plugin_root_block_with_no_local_fragment(tmp_path):
    assert not (tmp_path / "coordinator" / "contract").exists()
    assert _fire(tmp_path, json.dumps({"plan_review": BLOCK})) == ewf.EXIT_OK
    assert _fired_args(tmp_path)["plan_review"] == BLOCK


def test_chain_fire_binds_the_block_into_wave_args(tmp_path, capsys):
    assert _fire(tmp_path, json.dumps({"plan_review": BLOCK}), "--chain") == ewf.EXIT_OK
    manifest = json.loads(capsys.readouterr().out)["chain"]["manifest"]
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    assert data["wave_args"]["plan_review"] == BLOCK


@pytest.mark.parametrize("fragment", [
    None,
    "not json",
    json.dumps({"execute_review": {}}),
    json.dumps({"plan_review": {}}),
    json.dumps({"plan_review": "x"}),
])
def test_missing_or_empty_block_refuses_naming_the_path(tmp_path, capsys, fragment):
    assert _fire(tmp_path, fragment) == ewf.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "review-roster-fragment.json" in err
    assert not list((tmp_path / "trail").glob("fire-*.mjs"))


def test_block_reader_probes_the_repo_root_layout(tmp_path):
    (tmp_path / "coordinator" / "contract").mkdir(parents=True)
    (tmp_path / "coordinator" / "contract" / "review-roster-fragment.json").write_text(
        json.dumps({"plan_review": BLOCK}), encoding="utf-8")
    assert ewf._plan_review_block(tmp_path) == (BLOCK, None)


def _repair(tmp_path, fragment):
    """Run a repair emit over a one-baton trail; `fragment` is the plugin-root fragment text or None."""
    trail = tmp_path / "trail"
    slot = trail / "wave-0-aaaa"
    slot.mkdir(parents=True)
    (slot / "b1.planning-report.md").write_text(
        "# Planning report: b1\n\n**Plan:** `docs/plans/p.md` (status: draft)\n", encoding="utf-8")
    rel = ".coordinator-local/subagent-share/s/r.md"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("findings", encoding="utf-8")
    (slot / "b1.review-x-pointer.md").write_text(
        json.dumps({"sidecarPath": rel, "verdict": "OK", "premiseFailure": None}), encoding="utf-8")
    root = tmp_path / "doctrine" / "coordinator"
    (root / "workflows").mkdir(parents=True)
    (root / "workflows" / "plan-blitz.mjs").write_text("// stub\n", encoding="utf-8")
    if fragment is not None:
        (root / "contract").mkdir()
        (root / "contract" / "review-roster-fragment.json").write_text(fragment, encoding="utf-8")
    return ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(trail),
        "--plugin-root", str(root), "--live-engine-tree", "--repair", "b1",
    ])


def test_repair_fire_binds_the_plugin_root_block(tmp_path):
    assert _repair(tmp_path, json.dumps({"plan_review": BLOCK})) == ewf.EXIT_OK
    text = next((tmp_path / "trail").glob("repair-fire-*.mjs")).read_text(encoding="utf-8")
    args = json.loads(text.split("const args = ", 1)[1].rsplit(";", 1)[0])
    assert args["mode"] == "repair"
    assert args["plan_review"] == BLOCK


@pytest.mark.parametrize("fragment", [None, json.dumps({"plan_review": {}})])
def test_repair_refuses_without_a_roster_block(tmp_path, capsys, fragment):
    assert _repair(tmp_path, fragment) == ewf.EXIT_REFUSED
    assert "review-roster-fragment.json" in capsys.readouterr().err
    assert not list((tmp_path / "trail").glob("repair-fire-*.mjs"))


def _wave(tmp_path, fragment):
    """Run a plain (non-sizing, non-chain) wave emit over a one-baton frozen gate report."""
    trail = tmp_path / "trail"
    trail.mkdir()
    baton = {"id": "b1", "path": BATON_REL, "title": "T", "sized": False,
             "execution_gate": {"open": False}}
    (trail / "gate-report.json").write_text(
        json.dumps({"waves": [["b1"]], "batons": [baton]}), encoding="utf-8")
    root = tmp_path / "doctrine" / "coordinator"
    (root / "workflows").mkdir(parents=True)
    (root / "workflows" / "plan-blitz.mjs").write_text("// stub\n", encoding="utf-8")
    if fragment is not None:
        (root / "contract").mkdir()
        (root / "contract" / "review-roster-fragment.json").write_text(fragment, encoding="utf-8")
    return ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(trail),
        "--plugin-root", str(root), "--live-engine-tree",
    ])


def test_wave_fire_binds_the_plugin_root_block(tmp_path):
    assert _wave(tmp_path, json.dumps({"plan_review": BLOCK})) == ewf.EXIT_OK
    assert _fired_args(tmp_path)["plan_review"] == BLOCK


def test_wave_refuses_without_a_roster_block(tmp_path, capsys):
    assert _wave(tmp_path, None) == ewf.EXIT_REFUSED
    assert "review-roster-fragment.json" in capsys.readouterr().err
    assert not list((tmp_path / "trail").glob("fire-*.mjs"))
