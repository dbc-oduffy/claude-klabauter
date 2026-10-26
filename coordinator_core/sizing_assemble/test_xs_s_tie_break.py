"""route() keeps an S estimate at S: the retired S -> XS tie-break no longer demotes evidence-free S."""

from pathlib import Path

import pytest

from coordinator_core import sizing_assemble as sa

_FLAGS = {
    "none": {},
    "precedent": {"precedent": "shipped-before"},
    "boundary_in_notch": {"boundary_in_notch": "yes"},
    "scout_evidence": {"scout_evidence": ["docs/wiki" + "/foo.md"]},
    "non_positive": {"precedent": "novel", "boundary_in_notch": "no", "scout_evidence": []},
}


@pytest.mark.parametrize("name", sorted(_FLAGS))
def test_s_stays_s_with_or_without_evidence(name):
    decision = sa.route(estimate={"tshirt": "S"}, **_FLAGS[name])
    assert decision["resolved_estimate"]["tshirt"] == "S"
    assert decision["route"] == "spec-dispatch"
    assert decision["tie_break"] is None
    assert "Tie-break" not in decision["narration"]
    assert decision["stages"] == sa.stages("spec-dispatch", "S")


def test_collapse_probe_still_resizes_s_down():
    assert sa.route(estimate={"tshirt": "S"}, probe_signal="collapse")["route"] == "dispatch"


def test_requested_xs_stays_xs():
    decision = sa.route(estimate={"tshirt": "XS"})
    assert decision["route"] == "dispatch"
    assert decision["tie_break"] is None


@pytest.mark.parametrize("tshirt", ["XS", "M", "L", "XL", "XXL"])
def test_other_sizes_are_untouched(tshirt):
    assert sa.route(estimate={"tshirt": tshirt})["tie_break"] is None


def test_scope_floor_still_records_its_note_in_tie_break():
    decision = sa.route(estimate={"tshirt": "S"}, scope_files=sa.SCOPE_L_FLOOR_FILES)
    assert decision["resolved_estimate"]["tshirt"] == "L"
    assert decision["tie_break"].startswith("scope floor: S -> L")


def test_cli_output_keeps_s_without_a_tie_break(capsys):
    assert sa.main(["--appetite", "small", "--tshirt", "S"]) == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"tie_break": null' in out
    assert '"route": "spec-dispatch"' in out


_DRAFT = """schema: sizing-object
intent: "Ship the thing"
estimate:
  tshirt: S
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""


def _written(tmp_path, **route_kwargs):
    import yaml

    from coordinator_core.frontmatter.schema_validate import validate_frontmatter
    from coordinator_core.session import record_homes

    (tmp_path / ".git").mkdir()
    path = Path(record_homes.record_path(str(tmp_path), "sizings", "2026-10-02-x.yaml"))
    path.parent.mkdir(parents=True)
    path.write_text(_DRAFT, encoding="utf-8")
    decision = sa.route(estimate={"tshirt": "S"}, intent="Ship the thing", **route_kwargs)
    sa.write_back(tmp_path, str(path), decision)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = Path(sa.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
    assert validate_frontmatter(doc, str(schema)) == []
    return doc


def test_written_yaml_records_s_and_no_tie_break(tmp_path):
    doc = _written(tmp_path)
    assert doc["estimate"]["tshirt"] == "S"
    assert "tie_break" not in doc


def test_written_yaml_carries_the_provenance_stamp(tmp_path, monkeypatch):
    stamp = {"engine_sha": "a" * 40, "engine_dirty": False, "doe_plugin_sha": None}
    monkeypatch.setattr("coordinator_core.sizing_assemble.provenance.stamp", lambda: stamp)
    assert _written(tmp_path)["provenance"] == stamp


_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_S_REL = "state/sizings/2026-10-22-s.yaml"
_NEW_REL = "state/sizings/2026-10-23-fresh.yaml"


@pytest.fixture()
def s_plus_session(tmp_path, monkeypatch):
    from coordinator_core.session.touch_record import KIND_WRITE, append_touch_claims

    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    for name in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    monkeypatch.chdir(tmp_path)
    (tmp_path / _S_REL).write_text("estimate:\n  tshirt: S\n", encoding="utf-8")
    append_touch_claims([_S_REL], _SID, tmp_path, kind=KIND_WRITE)
    monkeypatch.setattr(sa, "_open_routed_obligation", lambda *_a, **_k: None)
    monkeypatch.setattr(sa, "_route_to_research", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sa, "write_back", lambda *a, **k: {"pm_verbatims": {"embedded": 0, "turns": None}}
    )
    return tmp_path


def test_evidence_free_s_writes_without_refusal_in_a_session_holding_an_s_plus_sizing(s_plus_session, capsys):
    rc = sa.main(["--tshirt", "S", "--write", _NEW_REL, "--no-plan-hand-off"])
    assert rc == sa.EXIT_OK
    assert "XS refused" not in capsys.readouterr().err


def test_requested_xs_refusal_keeps_the_rerun_as_s_text(s_plus_session, capsys):
    rc = sa.main(["--tshirt", "XS", "--write", _NEW_REL, "--no-plan-hand-off"])
    assert rc == sa.EXIT_USAGE
    assert "--tshirt S" in capsys.readouterr().err
