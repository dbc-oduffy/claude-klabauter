"""`emit-wave-fire` batons[] entries carry the sizing's effective route."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emit_wave_fire_route", _BIN / "emit-wave-fire.py")
ewf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ewf)


def _record(sizing_objects=None):
    rec = {
        "id": "b1",
        "path": "state/handoffs/b1.md",
        "title": "t",
        "sized": bool(sizing_objects),
        "execution_gate": {"open": True, "blocking": []},
    }
    if sizing_objects is not None:
        rec["sizing_objects"] = sizing_objects
    return rec


def _sizing_file(root: Path, **fields) -> str:
    rel = "state/sizings/s.yaml"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(yaml.safe_dump({"route": "pm-decision", **fields}), encoding="utf-8")
    return rel


def test_accepted_multi_session_xl_exit_routes_to_plan(tmp_path):
    rel = _sizing_file(tmp_path, xl_exit="accept_multi_session")
    assert ewf._baton_arg(_record([rel]), tmp_path)["route"] == "plan"


def test_unresolved_pm_decision_stays_pm_decision(tmp_path):
    rel = _sizing_file(tmp_path, xl_exit=None)
    assert ewf._baton_arg(_record([rel]), tmp_path)["route"] == "pm-decision"


def test_other_routes_are_unchanged(tmp_path):
    rel = _sizing_file(tmp_path, route="spec-dispatch")
    assert ewf._baton_arg(_record([rel]), tmp_path)["route"] == "spec-dispatch"


def test_unsized_baton_carries_no_route(tmp_path):
    assert ewf._baton_arg(_record(), tmp_path)["route"] is None


def test_unreadable_sizing_carries_no_route(tmp_path):
    assert ewf._baton_arg(_record(["state/sizings/gone.yaml"]), tmp_path)["route"] is None
