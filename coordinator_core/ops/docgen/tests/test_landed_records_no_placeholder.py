"""Landed-record templates (roadmap-seed, goal-seed, roadmap-baton) never render PLACEHOLDER.

Templates that are deliberate fill-step scaffolds (goal, plan, review, ...) are out of scope.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.docgen.render import render_document

LANDED = ("roadmap-seed", "goal-seed", "roadmap-baton")
_DOC_NEW = Path(__file__).resolve().parents[4] / "coordinator" / "bin" / "coordinator-doc-new.py"

BASE = {
    "title": "T",
    "created": "2026-10-08",
    "branch": "work/x/2026-10-08",
    "placeholder_summary": "summary",
    "deployment_state": "ready_to_fire",
    "roadmap_id": "rmp-x",
    "stub_id": "rmp-01",
    "authoring_session_path": "state/roadmap/rmp-x/",
}


def _fm(text: str) -> str:
    return text.split("---")[1]


@pytest.mark.parametrize("doc_type", LANDED)
def test_no_gate_no_workstream_omits_keys(doc_type):
    lines = _fm(render_document(doc_type, BASE)).splitlines()
    assert not any(line.startswith(("blocking_notes:", "workstream:", "gate_dependency:")) for line in lines)


@pytest.mark.parametrize("doc_type", LANDED)
def test_workstream_and_gate_render_when_supplied(doc_type):
    values = dict(BASE, workstream="rmp", gate_dependency="g", deployment_state="awaiting_gate")
    lines = _fm(render_document(doc_type, values)).splitlines()
    assert 'workstream: "rmp"' in lines
    assert any(line.startswith("gate_dependency:") for line in lines)


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_under_test", _DOC_NEW)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_scaffolded_seeds_carry_no_placeholder_key_or_value(cli):
    for text in (
        cli._scaffold_goal_seed(title="t", branch="b", summary="real summary"),
        cli._scaffold_roadmap_seed(title="t", branch="b", summary="real summary"),
    ):
        fm = _fm(text)
        assert "PLACEHOLDER" not in fm
        assert "blocking_notes" not in fm
        assert "workstream" not in fm
        assert "deployment_state: ready_to_fire" in fm


def test_scaffolded_seed_with_gate_stays_awaiting_gate(cli):
    fm = _fm(cli._scaffold_goal_seed(title="t", branch="b", summary="s", gate_dependency="g", workstream="ws"))
    assert "deployment_state: awaiting_gate" in fm
    assert 'workstream: "ws"' in fm


def test_baton_workstream_derived_from_stub_prefix(cli):
    fm = _fm(cli._scaffold_roadmap_baton(title="t", branch="b", roadmap_id="rmp-x", stub_id="fifa-03"))
    assert 'workstream: "fifa"' in fm
    assert "blocking_notes" not in fm
    assert "workstream: PLACEHOLDER" not in fm


def test_baton_explicit_workstream_wins_and_underivable_is_omitted(cli):
    assert 'workstream: "ws"' in _fm(
        cli._scaffold_roadmap_baton(title="t", branch="b", roadmap_id="r", stub_id="x", workstream="ws")
    )
    assert "workstream" not in _fm(cli._scaffold_roadmap_baton(title="t", branch="b", roadmap_id="r", stub_id="x"))


def test_baton_cli_refuses_missing_ids_naming_flags(cli, capsys):
    rc = cli.main(["--type", "roadmap-baton", "--title", "t", "--branch", "b", "--no-sizing-object"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "--roadmap-id" in err and "--stub-id" in err
