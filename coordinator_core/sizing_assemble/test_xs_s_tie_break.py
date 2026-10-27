"""XS/S tie-break in route(): an S estimate with no positive S evidence resolves to the XS dispatch route."""

from pathlib import Path

import pytest

from coordinator_core import sizing_assemble as sa

_POSITIVE = {
    "precedent": {"precedent": "shipped-before"},
    "boundary_in_notch": {"boundary_in_notch": "yes"},
    "probe_raise": {"probe_signal": "raise"},
    "scout_evidence": {"scout_evidence": ["docs" "/wiki/foo.md"]},
}


def test_evidence_free_s_collapses_to_xs_dispatch_visibly():
    decision = sa.route(estimate={"tshirt": "S"})
    assert decision["route"] == "dispatch"
    assert decision["resolved_estimate"]["tshirt"] == "XS"
    assert decision["tie_break"] == "S -> XS: no positive S evidence"
    assert "Tie-break" in decision["narration"]
    assert decision["stages"] == sa.route(estimate={"tshirt": "XS"})["stages"]


@pytest.mark.parametrize("name", sorted(_POSITIVE))
def test_any_one_positive_signal_keeps_s(name):
    decision = sa.route(estimate={"tshirt": "S"}, **_POSITIVE[name])
    assert decision["route"] != "dispatch"
    assert decision["tie_break"] is None
    if name != "probe_raise":
        assert decision["route"] == "spec-dispatch"
        assert decision["resolved_estimate"]["tshirt"] == "S"
        assert decision["stages"] == sa.stages("spec-dispatch", "S")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"precedent": "novel"},
        {"boundary_in_notch": "no"},
        {"scout_evidence": []},
        {"probe_signal": "collapse"},
    ],
)
def test_non_positive_values_still_collapse(kwargs):
    assert sa.route(estimate={"tshirt": "S"}, **kwargs)["route"] == "dispatch"


@pytest.mark.parametrize("tshirt", ["XS", "M", "L", "XL", "XXL"])
def test_other_sizes_are_untouched(tshirt):
    assert sa.route(estimate={"tshirt": tshirt})["tie_break"] is None


def test_cli_output_records_that_the_tie_break_fired(capsys):
    assert sa.main(["--appetite", "small", "--tshirt", "S"]) == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"tie_break": "S -> XS: no positive S evidence"' in out
    assert '"route": "dispatch"' in out


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


def test_written_yaml_carries_the_tie_break_when_it_fired(tmp_path):
    assert _written(tmp_path)["tie_break"] == "S -> XS: no positive S evidence"


def test_written_yaml_omits_the_tie_break_when_it_did_not_fire(tmp_path):
    assert "tie_break" not in _written(tmp_path, precedent="shipped-before")


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


def test_research_class_s_skips_the_tie_break():
    decision = sa.route(estimate={"tshirt": "S"}, research=True)
    assert decision["resolved_estimate"]["tshirt"] == "S"
    assert decision["tie_break"] is None
    assert decision["route"] == "spec-dispatch"


def test_research_s_writes_without_refusal_in_a_session_holding_an_s_plus_sizing(s_plus_session, capsys):
    argv = ["--tshirt", "S", "--write", _NEW_REL, "--no-plan-hand-off"]
    rc = sa.main([*argv, "--research-class", "corpus", "--research-source", "repo"])
    assert rc == sa.EXIT_OK
    assert "XS refused" not in capsys.readouterr().err


def test_non_research_tie_break_xs_refusal_names_the_s_evidence_flags(s_plus_session, capsys):
    rc = sa.main(["--tshirt", "S", "--write", _NEW_REL, "--no-plan-hand-off"])
    assert rc == sa.EXIT_USAGE
    err = capsys.readouterr().err
    for flag in ("--scout-evidence", "--boundary-in-notch yes", "--precedent shipped-before", "--probe-signal raise"):
        assert flag in err
    assert "--tshirt S" not in err


def test_requested_xs_refusal_keeps_the_rerun_as_s_text(s_plus_session, capsys):
    rc = sa.main(["--tshirt", "XS", "--write", _NEW_REL, "--no-plan-hand-off"])
    assert rc == sa.EXIT_USAGE
    assert "--tshirt S" in capsys.readouterr().err
