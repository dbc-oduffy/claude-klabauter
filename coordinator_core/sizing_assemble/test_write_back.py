"""Tests for `sizing-assemble --write`: `write_back` applies a route() decision to a draft sizing."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sizing_assemble

_DRAFT = """schema: sizing-object
intent: "Ship the thing"
estimate:
  tshirt: XS  # XS | S | M
  provisional: true
route: dispatch  # dispatch | plan
detents: []  # none
fork: null
xl_exit: null
status: draft  # draft | sized
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    return tmp_path


def _sizing(repo: Path, text: str = _DRAFT) -> Path:
    path = repo / "state" / "sizings" / "2026-10-02-x.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _decision(tshirt: str = "M", **kwargs) -> dict:
    return sizing_assemble.route(
        estimate={"tshirt": tshirt}, intent="Ship the thing", **kwargs
    )


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_write_sets_estimate_route_detents_and_status(repo: Path) -> None:
    path = _sizing(repo)
    decision = _decision("M", premise_provenance="read")
    sizing_assemble.write_back(
        repo,
        "state/sizings/2026-10-02-x.yaml",
        decision,
        premise_provenance="read",
        premise_evidence="tests/x.py:3",
    )
    doc = _load(path)
    assert doc["estimate"] == {"tshirt": "M", "provisional": True}
    assert doc["route"] == decision["route"] == "plan"
    assert doc["detents"] == decision["detents"]
    assert "premise_unproven" in doc["detents"]
    assert doc["status"] == "sized"
    assert doc["premise"] == {"provenance": "read", "evidence": "tests/x.py:3"}
    assert "# dispatch | plan" in path.read_text(encoding="utf-8")


def test_write_never_regresses_status(repo: Path) -> None:
    path = _sizing(repo, _DRAFT.replace("status: draft", "status: routed"))
    sizing_assemble.write_back(repo, str(path), _decision("M"))
    assert _load(path)["status"] == "routed"


def test_write_keeps_accepted_exit_criterion_and_existing_mode(repo: Path) -> None:
    accepted = {"pm_quote": "go ahead", "on": "2026-10-01", "mode": "pm"}
    text = (
        _DRAFT
        + "exit_criterion:\n  statement: Original\n  accepted:\n"
        + "    pm_quote: 'go ahead'\n    'on': '2026-10-01'\n    mode: pm\n"
        + "interaction_mode: pm\n"
    )
    path = _sizing(repo, text)
    sizing_assemble.write_back(
        repo,
        str(path),
        _decision("M"),
        exit_criterion="Rewritten",
        interaction_mode="ceo",
    )
    doc = _load(path)
    assert doc["exit_criterion"]["statement"] == "Original"
    assert doc["exit_criterion"]["accepted"]["pm_quote"] == accepted["pm_quote"]
    assert doc["interaction_mode"] == "pm"


def test_write_sets_statement_while_unaccepted_and_mode_while_absent(repo: Path) -> None:
    path = _sizing(repo)
    sizing_assemble.write_back(
        repo,
        str(path),
        _decision("M"),
        exit_criterion="Ship it",
        interaction_mode="ceo",
    )
    doc = _load(path)
    assert doc["exit_criterion"] == {"statement": "Ship it", "accepted": None}
    assert doc["interaction_mode"] == "ceo"


def test_write_refuses_path_outside_state_sizings(repo: Path) -> None:
    outside = repo / "elsewhere.yaml"
    outside.write_text(_DRAFT, encoding="utf-8")
    with pytest.raises(sizing_assemble.SizingAssembleError):
        sizing_assemble.write_back(repo, str(outside), _decision("M"))
    assert outside.read_text(encoding="utf-8") == _DRAFT


def test_write_refuses_premise_without_evidence(repo: Path) -> None:
    path = _sizing(repo)
    with pytest.raises(sizing_assemble.SizingAssembleError):
        sizing_assemble.write_back(
            repo, str(path), _decision("M"), premise_provenance="read"
        )
    assert path.read_text(encoding="utf-8") == _DRAFT


def test_schema_invalid_result_leaves_file_untouched(repo: Path) -> None:
    path = _sizing(repo)
    decision = _decision("M")
    decision["detents"] = ["not-a-real-detent"]
    with pytest.raises(sizing_assemble.SizingAssembleError):
        sizing_assemble.write_back(repo, str(path), decision)
    assert path.read_text(encoding="utf-8") == _DRAFT


def test_write_on_a_missing_file_scaffolds_then_writes(repo: Path) -> None:
    target = repo / "state" / "sizings" / "2026-10-02-new.yaml"
    decision = _decision("S", premise_provenance="read")
    sizing_assemble.write_back(
        repo,
        "state/sizings/2026-10-02-new.yaml",
        decision,
        premise_provenance="read",
        premise_evidence="tests/x.py:3",
    )
    doc = _load(target)
    assert doc["intent"] == "Ship the thing"
    assert doc["status"] == "sized"
    assert doc["premise"] == {"provenance": "read", "evidence": "tests/x.py:3"}


def test_write_on_a_missing_file_without_intent_still_refuses(repo: Path) -> None:
    decision = sizing_assemble.route(estimate={"tshirt": "S"})
    with pytest.raises(sizing_assemble.SizingAssembleError):
        sizing_assemble.write_back(repo, "state/sizings/nope.yaml", decision)
    assert not (repo / "state" / "sizings" / "nope.yaml").exists()
