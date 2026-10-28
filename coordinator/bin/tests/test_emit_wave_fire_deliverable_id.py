"""`emit-wave-fire --from-sizing` single fire binds the sizing's deliverable_id onto the baton."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from _roster_fixture import write_roster_fragment

import pytest
import yaml

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emit_wave_fire_did", _BIN / "emit-wave-fire.py")
ewf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ewf)

SIZING_REL = "state/sizings/s1.yaml"
BATON_REL = "state/handoffs/b1.md"
BATON = "---\nhandoff_id: hnd-1\ntitle: Minted baton\nstatus: open\n---\n\n# body\n"
DLV = "dlv-we-want-to-show-here-s-what-we-could-ee7a7b"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-install"))
    monkeypatch.setattr(
        ewf, "_bind",
        lambda script_path, args, live=False: "const args = " + json.dumps(args) + ";\n",
    )
    monkeypatch.setattr(ewf, "_report_pair", lambda *a, **k: None)
    monkeypatch.setattr(
        ewf, "_load_mint",
        lambda: (lambda sizing_rel, repo_root, **kw: {
            "id": "hnd-1", "path": BATON_REL, "title": "Minted baton", "created": True,
        }),
    )


def _sizing(**over):
    body = {
        "schema": "sizing-object",
        "status": "sized",
        "premise": {"provenance": "unrecorded"},
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "appetite": "medium",
        "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan",
        "interaction_mode": "hands-on",
        "intent": "do the thing",
        "exit_criterion": {
            "statement": "it works",
            "accepted": {"pm_quote": "ok", "on": "2026-10-01", "mode": "hands-on"},
        },
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


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
    write_roster_fragment(root)
    (root / "agents").mkdir()
    rc = ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        "--plugin-root", str(root), "--from-sizing", SIZING_REL, "--live-engine-tree",
        "--plan-only", *extra,
    ])
    assert rc == ewf.EXIT_OK
    text = (tmp_path / "trail" / "fire-0-1.mjs").read_text(encoding="utf-8")
    return json.loads(text[len("const args = "):].rstrip().rstrip(";"))["batons"][0]


def test_sizing_deliverable_id_is_bound_onto_the_baton(tmp_path):
    assert _fire(tmp_path, _sizing(deliverable_id=DLV))["deliverableId"] == DLV


def test_flag_matching_the_sizing_id_binds_it_once(tmp_path):
    baton = _fire(tmp_path, _sizing(deliverable_id=DLV), "--deliverable-id", DLV)
    assert baton["deliverableId"] == DLV


def test_sizing_without_an_id_binds_no_key(tmp_path):
    assert "deliverableId" not in _fire(tmp_path, _sizing())
