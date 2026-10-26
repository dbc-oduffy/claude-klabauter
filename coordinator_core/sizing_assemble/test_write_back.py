"""Tests for `sizing-assemble --write`: `write_back` applies a route() decision to a draft sizing."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.session import record_homes

SCHEMA = Path(sizing_assemble.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"

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
    if tshirt == "S":
        kwargs.setdefault("precedent", "shipped-before")
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
    assert doc["exit_criterion"]["statement"] == "Ship it"
    assert doc["exit_criterion"]["accepted"]["source"] == "engine-size-rule"
    assert doc["interaction_mode"] == "ceo"


def _engine_record(**over) -> dict:
    return {
        "source": "engine-size-rule", "rule": "engine-size-rule", "route": "plan",
        "tshirt": "M", "on": date.today().isoformat(), "mode": "ceo", **over,
    }


@pytest.mark.parametrize("tshirt,route", [("XS", "dispatch"), ("S", "spec-dispatch"), ("M", "plan"), ("L", "spec-dispatch")])
@pytest.mark.parametrize("mode", ["hands-on", "pm", "ceo"])
def test_size_rule_skip_is_recorded_on_the_sizing(repo: Path, tshirt: str, route: str, mode: str) -> None:
    path = _sizing(repo)
    decision = _decision(tshirt)
    assert decision["route"] == route
    sizing_assemble.write_back(
        repo, str(path), decision, exit_criterion="Ship it", interaction_mode=mode
    )
    ec = _load(path)["exit_criterion"]
    assert ec["accepted"] == _engine_record(route=route, tshirt=tshirt, mode=mode)
    assert validate_frontmatter(_load(path), SCHEMA) == []


def test_hands_on_below_xl_records_the_engine_skip(repo: Path) -> None:
    path = _sizing(repo)
    sizing_assemble.write_back(
        repo, str(path), _decision("M"), exit_criterion="Ship it", interaction_mode="hands-on"
    )
    assert _load(path)["exit_criterion"]["accepted"] == _engine_record(
        route="plan", tshirt="M", mode="hands-on"
    )


@pytest.mark.parametrize("tshirt,kwargs", [("XL", {}), ("XL", {"jtbd_unclear": True}), ("L", {"jtbd_unclear": True})])
def test_no_record_at_xl_or_shape(repo: Path, tshirt: str, kwargs: dict) -> None:
    path = _sizing(repo)
    decision = _decision(tshirt, **kwargs)
    assert decision["route"] != "plan" or tshirt == "XL"
    sizing_assemble.write_back(
        repo, str(path), decision, exit_criterion="Ship it", interaction_mode="ceo"
    )
    assert _load(path)["exit_criterion"]["accepted"] is None


def test_no_record_without_a_statement(repo: Path) -> None:
    path = _sizing(repo)
    sizing_assemble.write_back(repo, str(path), _decision("M"), interaction_mode="ceo")
    assert "exit_criterion" not in _load(path) or _load(path)["exit_criterion"] is None


def test_resize_out_of_the_rule_clears_the_record_and_rerun_keeps_its_date(repo: Path) -> None:
    path = _sizing(repo)
    sizing_assemble.write_back(
        repo, str(path), _decision("M"), exit_criterion="Ship it", interaction_mode="ceo"
    )
    text = path.read_text(encoding="utf-8").replace(date.today().isoformat(), "2026-01-01")
    path.write_text(text, encoding="utf-8")
    sizing_assemble.write_back(repo, str(path), _decision("M"))
    assert _load(path)["exit_criterion"]["accepted"]["on"] == "2026-01-01"
    sizing_assemble.write_back(repo, str(path), _decision("XL"))
    assert _load(path)["exit_criterion"]["accepted"] is None


def test_pm_and_apm_records_are_never_replaced_by_the_rule(repo: Path) -> None:
    text = (
        _DRAFT
        + "exit_criterion:\n  statement: Original\n  accepted:\n"
        + "    source: apm\n    apm_ruling: fine\n    ruling_ref: state/apm/r.md\n    'on': '2026-10-01'\n    mode: ceo\n"
        + "interaction_mode: ceo\n"
    )
    path = _sizing(repo, text)
    sizing_assemble.write_back(repo, str(path), _decision("M"), exit_criterion="X")
    assert _load(path)["exit_criterion"]["accepted"]["source"] == "apm"


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


def test_refused_write_on_a_missing_file_leaves_no_scaffold(repo: Path) -> None:
    decision = _decision("M")
    decision["detents"] = ["not-a-real-detent"]
    with pytest.raises(sizing_assemble.SizingAssembleError):
        sizing_assemble.write_back(repo, "state/sizings/2026-10-02-new.yaml", decision)
    assert not (repo / "state" / "sizings" / "2026-10-02-new.yaml").exists()


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


def test_write_mints_a_deliverable_id_when_the_sizing_has_none(repo: Path) -> None:
    path = _sizing(repo)
    rel = Path(record_homes.record_path("", "sizings", _SIZING_NAME)).as_posix()
    sizing_assemble.write_back(repo, rel, _decision("M"))
    minted = _load(path)["deliverable_id"]
    assert minted.startswith("dlv-ship-the-thing-")
    sizing_assemble.write_back(repo, rel, _decision("M"))
    assert _load(path)["deliverable_id"] == minted


def test_write_embeds_pm_verbatims_once_and_keeps_em_curation(repo: Path, monkeypatch) -> None:
    import coordinator_core.ops.baton_pm_turns as pm_turns

    window = [{"turn": 1, "ts": "2026-10-08T10:00:00Z", "text": "the real ask:\nship it"},
              {"turn": 0, "ts": "2026-10-08T09:00:00Z", "text": "hi"}]
    monkeypatch.setattr(pm_turns, "bound_window", lambda *_a, **_k: list(window))
    path = _sizing(repo)
    rel = Path(record_homes.record_path("", "sizings", _SIZING_NAME)).as_posix()
    sizing_assemble.write_back(repo, rel, _decision("M"))
    assert _load(path)["pm_verbatims"] == window

    curated = path.read_text(encoding="utf-8").replace("  - turn: 0", "  - turn: 9")
    path.write_text(curated, encoding="utf-8")
    sizing_assemble.write_back(repo, rel, _decision("M"))
    assert [e["turn"] for e in _load(path)["pm_verbatims"]] == [1, 9]


def test_relayed_ask_registers_only_its_text_and_lists_every_turn_unrelated(
    repo: Path, monkeypatch
) -> None:
    import coordinator_core.ops.baton_pm_turns as pm_turns
    from coordinator_core.ops import requirement_register as rr

    window = [{"turn": n, "ts": "2026-10-08T10:00:00Z", "text": f"session chatter {n}"}
              for n in range(44, 48)]
    monkeypatch.setattr(pm_turns, "bound_window", lambda *_a, **_k: list(window))
    path = _sizing(repo)
    rel = Path(record_homes.record_path("", "sizings", _SIZING_NAME)).as_posix()
    sizing_assemble.write_back(repo, rel, _decision("M"), relayed_ask_text="  fix the relayed thing\n")
    doc = _load(path)
    register = doc["requirement_register"]
    assert [r["source_text"] for r in register["rows"]] == ["fix the relayed thing"]
    assert register["rows"][0]["id"] == "R1"
    assert register["rows"][0]["anchor"] == "relayed"
    assert [e["turn"] for e in register["unrelated_turns"]] == [44, 45, 46, 47]
    assert all(e["reason"] for e in register["unrelated_turns"])
    assert [e["turn"] for e in doc["pm_verbatims"]] == [44, 45, 46, 47]
    assert rr.coverage_gaps(doc) == []
    assert rr.unrelated_turn_errors(doc, register) == []


def test_pm_verbatims_round_trip_yaml_hostile_prompts_from_the_real_log(
    repo: Path, monkeypatch
) -> None:
    import coordinator_core.ops.baton_pm_turns as pm_turns
    from coordinator_core.session_baton import store

    sdir = repo / ".git" / "coordinator-sessions" / "sid-wb"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-wb")
    prompts = ["# not a comment", "---\nkey: value", 'he said "no": \'never\'', "a b", "  padded  "]
    for prompt in prompts:
        pm_turns.append_turn(prompt, session_id="sid-wb")

    path = _sizing(repo)
    rel = Path(record_homes.record_path("", "sizings", _SIZING_NAME)).as_posix()
    sizing_assemble.write_back(repo, rel, _decision("M"))
    assert [e["text"] for e in _load(path)["pm_verbatims"]] == [prompts[-1]]


def test_a_one_turn_ask_in_a_many_turn_session_embeds_one_turn_and_clears_the_prep_gate(
    repo: Path, monkeypatch
) -> None:
    import coordinator_core.ops.baton_pm_turns as pm_turns
    from coordinator_core.roadmap import prep_gate
    from coordinator_core.session_baton import store

    sdir = repo / ".git" / "coordinator-sessions" / "sid-many"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-many")
    for n in range(11):
        pm_turns.append_turn(f"unrelated instruction number {n}", session_id="sid-many")

    path = _sizing(repo)
    rel = Path(record_homes.record_path("", "sizings", _SIZING_NAME)).as_posix()
    sizing_assemble.write_back(repo, rel, _decision("M"))
    doc = _load(path)
    assert [e["turn"] for e in doc["pm_verbatims"]] == [10]
    rows = doc["requirement_register"]["rows"]
    assert len(rows) == 1

    fm = {"sizing_object": rel,
          "register_claims": {"sizing": rel, "rows": [rows[0]["id"]]}}
    verdict = prep_gate._register(fm, repo)
    assert verdict["status"] == "PASS", verdict["detail"]


_NEW = "state/sizings/2026-10-11-named.yaml"


def test_name_lands_as_a_real_line_on_a_fresh_write(repo: Path) -> None:
    sizing_assemble.write_back(repo, _NEW, _decision("S"), name="A short label")
    text = (repo / _NEW).read_text(encoding="utf-8")
    assert _load(repo / _NEW)["name"] == "A short label"
    assert "# name:" not in text


def test_absent_name_keeps_the_commented_template_line(repo: Path) -> None:
    sizing_assemble.write_back(repo, _NEW, _decision("S"))
    text = (repo / _NEW).read_text(encoding="utf-8")
    assert "# name:" in text
    assert "name" not in _load(repo / _NEW)


def test_name_is_truncated_to_sixty_chars(repo: Path) -> None:
    sizing_assemble.write_back(repo, _NEW, _decision("S"), name="x" * 80)
    assert _load(repo / _NEW)["name"] == "x" * 60


def test_name_on_an_existing_record_replaces_template_or_inserts_and_keeps_real(repo: Path) -> None:
    rel = Path(record_homes.record_path("", "sizings", _SIZING_NAME)).as_posix()
    path = _sizing(repo, "# name: PLACEHOLDER  # OPTIONAL\n" + _DRAFT)
    sizing_assemble.write_back(repo, rel, _decision("M"), name="First")
    assert _load(path)["name"] == "First"
    assert "# name:" not in path.read_text(encoding="utf-8")
    sizing_assemble.write_back(repo, rel, _decision("M"), name="Second")
    assert _load(path)["name"] == "First"
    path = _sizing(repo)
    sizing_assemble.write_back(repo, rel, _decision("M"), name="Inserted")
    assert _load(path)["name"] == "Inserted"


def test_fresh_scaffold_carries_the_join_wording_not_the_rescaffold_promise(repo: Path) -> None:
    sizing_assemble.write_back(repo, _NEW, _decision("S"))
    text = (repo / _NEW).read_text(encoding="utf-8")
    assert "re-scaffold with --deliverable-id" not in text
    assert "a --deliverable-id join works on a new record" in text


def test_cli_write_carries_the_scouts_doc_consumers_and_ask_conflicts(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
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
            "--doc-consumer", "README.md:12",
            "--doc-consumer", "docs/wiki" + "/x.md#rule",
            "--ask-conflict", "state/cross-repo/memo.md=says keep the old bar",
            "--ask-conflict", "docs/decisions/DR-1.md",
        ]
    )
    assert rc == sizing_assemble.EXIT_OK, capsys.readouterr().err
    doc = _load(path)
    assert doc["doc_consumers"] == ["README.md:12", "docs/wiki" + "/x.md#rule"]
    assert doc["ask_conflicts"] == [
        {"path": "state/cross-repo/memo.md", "conflict": "says keep the old bar"},
        {"path": "docs/decisions/DR-1.md"},
    ]


def test_an_empty_scout_claim_is_written_and_absence_writes_nothing(repo: Path) -> None:
    path = _sizing(repo)
    sizing_assemble.write_back(repo, str(path), _decision("M"))
    assert "doc_consumers" not in _load(path) and "ask_conflicts" not in _load(path)
    sizing_assemble.write_back(repo, str(path), _decision("M"), doc_consumers=[], ask_conflicts=[])
    doc = _load(path)
    assert doc["doc_consumers"] == [] and doc["ask_conflicts"] == []


def test_a_plan_edged_sizing_refuses_an_xs_resize_and_other_sizes_still_write(repo: Path) -> None:
    edged = _DRAFT.replace("estimate:\n  tshirt: XS", "plan: docs/plans/x.md\nestimate:\n  tshirt: S")
    path = _sizing(repo, edged)
    with pytest.raises(sizing_assemble.SizingAssembleError, match="SIZING-PLAN-EDGE-RESIZE-TO-XS"):
        sizing_assemble.write_back(repo, str(path), _decision("XS"))
    assert path.read_text(encoding="utf-8") == edged
    sizing_assemble.write_back(repo, str(path), _decision("S"))
    assert _load(path)["estimate"]["tshirt"] == "S"
    plain = _sizing(repo)
    sizing_assemble.write_back(repo, str(plain), _decision("XS"))
    assert _load(plain)["estimate"]["tshirt"] == "XS"
