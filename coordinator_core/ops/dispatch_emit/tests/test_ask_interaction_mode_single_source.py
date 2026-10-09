"""Interaction mode has one source: the size prompt relays sizing-assemble's value; the gate resolves it fleet-first."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session import record_homes

REL = Path(record_homes.record_path("", "sizings", "2026-10-01-gate.yaml")).as_posix()


def test_size_prompt_names_no_mode_and_relays_the_returned_one():
    script = compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    clause = re.search(r"--interaction-mode \((.*?verbatim[^)]*)\)", script).group(1)
    assert "`interaction_mode`" in clause and "verbatim" in clause
    assert not re.search(r"\b(hands-on|pm|ceo)\b", clause)


def test_gate_honours_the_sizings_recorded_mode_over_the_fleet(tmp_path, monkeypatch):
    home = tmp_path / "settings"
    home.mkdir()
    (home / "fleet-mode.json").write_text(json.dumps({"interaction_mode": "ceo"}), encoding="utf-8")
    monkeypatch.setattr(
        "coordinator_core.session.fleet_mode.fleet_mode_path", lambda: home / "fleet-mode.json"
    )
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    doc = {
        "schema": "sizing-object",
        "name": "gate fixture",
        "intent": "exercise the gate",
        "estimate": {"tshirt": "XL", "provisional": True},
        "route": "plan",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123",
        "interaction_mode": "hands-on",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    verdict = gate(tmp_path, REL)
    assert verdict.halt["kind"] == "touchpoint"
    assert verdict.halt["reason"].startswith("hands-on mode")
