"""Tests for `sizing-assemble --write`: `write_back` applies a route() decision to a draft sizing."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core.session import record_homes

_SIZING_NAME = "2026-10-02-x.yaml"

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
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    return tmp_path


def _sizing(repo: Path, text: str = _DRAFT) -> Path:
    path = Path(record_homes.record_path(str(repo), "sizings", _SIZING_NAME))
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
        Path(record_homes.record_path("", "sizings", _SIZING_NAME)).as_posix(),
        decision,
        premise_provenance="read",
        premise_evidence="tests/x.py:3",
    )
    doc = _load(path)
    assert doc["estimate"] == {"tshirt": "M", "provisional": True}
    assert doc["route"] == decision["route"] == "plan"
    assert doc["detents"] == decision["detents"]
    assert "premise_unproven" in doc["detents"]
    assert doc["status"] == "routed"
    assert doc["premise"] == {"provenance": "read", "evidence": "tests/x.py:3"}
    assert "# dispatch | plan" in path.read_text(encoding="utf-8")


def test_write_joins_a_given_deliverable_id_and_never_rekeys(repo: Path) -> None:
    path = _sizing(repo)
    rel = Path(record_homes.record_path("", "sizings", _SIZING_NAME)).as_posix()
    sizing_assemble.write_back(repo, rel, _decision("M"), deliverable_id="dlv-a-111111")
    assert _load(path)["deliverable_id"] == "dlv-a-111111"
    with pytest.raises(sizing_assemble.SizingAssembleError):
        sizing_assemble.write_back(repo, rel, _decision("M"), deliverable_id="dlv-b-222222")
    assert _load(path)["deliverable_id"] == "dlv-a-111111"


def test_write_never_regresses_terminal_status(repo: Path) -> None:
    path = _sizing(repo, _DRAFT.replace("status: draft", "status: shipped"))
    sizing_assemble.write_back(repo, str(path), _decision("M"))
    assert _load(path)["status"] == "shipped"


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
    assert doc["status"] == "routed"
    assert doc["premise"] == {"provenance": "read", "evidence": "tests/x.py:3"}


def test_write_on_a_missing_file_without_intent_still_refuses(repo: Path) -> None:
    decision = sizing_assemble.route(estimate={"tshirt": "S"})
    with pytest.raises(sizing_assemble.SizingAssembleError):
        sizing_assemble.write_back(repo, "state/sizings/nope.yaml", decision)
    assert not (repo / "state" / "sizings" / "nope.yaml").exists()


def test_doc_new_draft_round_trips_to_routed_and_warp_accepts(repo: Path) -> None:
    """doc-new scaffold (placeholders) -> write_back -> routed, schema-valid, human fields kept,
    and warp's own status/placeholder gate (sizing_fire) raises no status or placeholder refusal."""
    import importlib.util

    from coordinator_core.ops.dispatch_emit import sizing_fire

    doc_new = Path(sizing_assemble.__file__).resolve().parents[2] / "coordinator" / "bin" / "coordinator-doc-new.py"
    spec = importlib.util.spec_from_file_location("doc_new_rt", doc_new)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    path = _sizing(repo, mod._scaffold_sizing(title="Ship the thing"))
    before = _load(path)
    assert before["status"] == "draft" and before["premise"]["evidence"].startswith("PLACEHOLDER")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("\nstatus: draft", "\nname: my label\nstatus: draft", 1), encoding="utf-8") \
        if "\nname:" not in text else None
    decision = _decision("S", premise_provenance="read")
    sizing_assemble.write_back(
        repo, str(path), decision,
        premise_provenance="read", premise_evidence="tests/x.py:3",
        exit_criterion="Ship it", interaction_mode="pm",
    )
    doc = _load(path)
    assert doc["status"] == "routed"
    assert doc["estimate"]["tshirt"] == "S" and doc["route"] == decision["route"]
    assert doc["detents"] == decision["detents"]
    assert doc["intent"] == "Ship the thing"
    assert not doc["premise"]["evidence"].startswith("PLACEHOLDER")  # placeholder replaced
    assert doc["premise"]["evidence"] == "tests/x.py:3"
    assert doc.get("name") == "my label"  # human-authored field preserved
    refusals = sizing_fire.collect_fire_refusals(
        doc, sizing_rel="state/sizings/2026-10-02-x.yaml",
        arm=sizing_fire.resolve_arm(doc), writes=["a.py"], repo_root=repo,
    )
    assert not [r for r in refusals if "`status`" in r or "placeholder" in r or "unrecorded" in r], refusals


def test_human_authored_intent_is_not_overwritten(repo: Path) -> None:
    path = _sizing(repo, _DRAFT.replace("Ship the thing", "Human words"))
    sizing_assemble.write_back(repo, str(path), _decision("M"))
    assert _load(path)["intent"] == "Human words"


def test_placeholder_intent_is_replaced_by_computed_intent(repo: Path) -> None:
    path = _sizing(repo, _DRAFT.replace("Ship the thing", "PLACEHOLDER - the PM's ask"))
    sizing_assemble.write_back(repo, str(path), _decision("M"))
    assert _load(path)["intent"] == "Ship the thing"


def test_cli_write_persists_default_resolved_mode(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A default-resolved mode is printed AND persisted: the reported
    `interaction_mode` must be on the record, or a consumer reading the
    record (emit-roadmap-fire) sees an empty mode."""
    from coordinator_core.session import fleet_mode as fleet_mode_module
    from coordinator_core.session import mode_resolution as mode_resolution_module

    monkeypatch.setattr(fleet_mode_module, "read_fleet_mode", lambda: {})
    monkeypatch.setattr(mode_resolution_module, "read_fleet_mode", lambda: {})
    path = _sizing(repo)
    monkeypatch.chdir(repo)
    rc = sizing_assemble.main(
        [
            "--tshirt", "M",
            "--write", Path(record_homes.record_path("", "sizings", _SIZING_NAME)).as_posix(),
            "--premise-provenance", "read",
            "--premise-evidence", "tests/x.py:3",
        ]
    )
    assert rc == sizing_assemble.EXIT_OK
    # Default is ceo per docs/plans/2026-10-06-warp-one-pass-fire.md (§ Problem: "The default mode becomes ceo").
    assert "ceo" in capsys.readouterr().out
    assert _load(path)["interaction_mode"] == "ceo"
