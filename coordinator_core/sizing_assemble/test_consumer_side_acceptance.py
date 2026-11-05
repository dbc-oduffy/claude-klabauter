"""Consumer-side acceptance at the sizing gate: producer-only wire clauses are refused, and a
named-but-unowned seam mints a `seam` register row.

Run (from repo root):
    python -m pytest coordinator_core/sizing_assemble/test_consumer_side_acceptance.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.session import fleet_mode as fleet_mode_module
from coordinator_core.session import mode_resolution as mode_resolution_module
from coordinator_core.session import record_homes

_NAME = "2026-11-04-consumer.yaml"
_RULING = "docs/plans/ruling.md"
_PRODUCER_ONLY = "the loader writes points.json that feeds the fitter"
_CONSUMER = "`fitter report --json` shows points.json consumed counts above zero"
_PLAN_CRITERION = (
    "A sizing whose exit criterion names only a producer artefact for a wire/use/feed/read/inject "
    "clause is refused at the sizing gate with a message naming the clause, a producer seam with "
    "no consumer row mints a sizing row owning its wiring, and tests covering touched files pass."
)
_DRAFT = """schema: sizing-object
intent: "Ship the thing"
estimate:
  tshirt: XS
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
_AMEND_DOC = _DRAFT.replace("route: dispatch", "route: plan") + """interaction_mode: ceo
exit_criterion:
  statement: The old criterion.
  accepted:
    source: apm
    apm_ruling: first ruling
    ruling_ref: run-1
    'on': '2026-10-01'
    mode: ceo
"""


@pytest.fixture(autouse=True)
def _fleet(monkeypatch):
    fn = lambda: {}
    monkeypatch.setattr(fleet_mode_module, "read_fleet_mode", fn)
    monkeypatch.setattr(mode_resolution_module, "read_fleet_mode", fn)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.record_path(str(tmp_path), "sizings", _NAME)).parent.mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / _RULING).write_text("ruling\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _sizing(repo: Path, text: str) -> tuple[Path, str]:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(text, encoding="utf-8")
    return path, path.relative_to(repo).as_posix()


def test_cli_refuses_producer_only_wire_clause_naming_it(capsys):
    rc = sa.main(["--tshirt", "S", "--exit-criterion", _PRODUCER_ONLY])
    assert rc == sa.EXIT_USAGE
    assert "feeds the fitter" in capsys.readouterr().err


def test_cli_accepts_consumer_side_command(capsys):
    assert sa.main(["--tshirt", "S", "--exit-criterion", _CONSUMER]) == sa.EXIT_OK


def test_plan_exit_criterion_is_accepted_unchanged(capsys):
    assert sa.main(["--tshirt", "S", "--exit-criterion", _PLAN_CRITERION]) == sa.EXIT_OK


def test_amend_refuses_producer_only_and_keeps_record(repo, capsys):
    path, rel = _sizing(repo, _AMEND_DOC)
    before = path.read_text(encoding="utf-8")
    rc = sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _PRODUCER_ONLY, "--ruling", _RULING])
    assert rc == sa.EXIT_BUSINESS_FAIL
    assert "feeds the fitter" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before


def test_amend_accepts_consumer_side_command(repo):
    _, rel = _sizing(repo, _AMEND_DOC)
    assert sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _CONSUMER, "--ruling", _RULING]) == sa.EXIT_OK


def _register_rows(repo: Path, relayed: str) -> list[dict]:
    import coordinator_core.ops.baton_pm_turns as pm_turns

    original = pm_turns.bound_window
    pm_turns.bound_window = lambda *_a, **_k: [{"turn": 1, "ts": None, "text": "chatter"}]
    try:
        path, rel = _sizing(repo, _DRAFT)
        sa.write_back(repo, rel, sa.route(estimate={"tshirt": "M"}, intent="Ship the thing"),
                      relayed_ask_text=relayed)
    finally:
        pm_turns.bound_window = original
    return yaml.safe_load(path.read_text(encoding="utf-8"))["requirement_register"]["rows"]


def test_relayed_ask_with_unowned_seam_mints_seam_row(repo):
    rows = _register_rows(repo, "wire the loader; nothing feeds the fitter")
    seam = [r for r in rows if r["anchor"] == "seam"]
    assert [r["id"] for r in rows] == ["R1", "S1"]
    assert len(seam) == 1 and "own the wiring of fitter" in seam[0]["source_text"]
    assert seam[0]["status"] == "open"


def test_seam_with_a_consumer_row_mints_nothing():
    doc = {"pm_verbatims": [], "acceptance_rows": [
        {"id": "A1", "statement": "the loader writes points.json"},
        {"id": "A2", "statement": "`fitter report` shows points.json consumed"},
    ]}
    assert [r["anchor"] for r in sa.derive_register(doc, None, "ship it")[0]] == ["relayed"]


def test_unconsumed_acceptance_artefact_mints_seam_row():
    doc = {"pm_verbatims": [], "acceptance_rows": [{"id": "A1", "statement": "the loader writes points.json"}]}
    rows = sa.derive_register(doc, None, "ship it")[0]
    assert [r["anchor"] for r in rows] == ["relayed", "seam"]
    assert "points.json" in rows[1]["source_text"]


def test_em_brief_seam_is_read():
    doc = {"pm_verbatims": [], "em_analysis": {"em_brief": "points.json has no consumer"}}
    rows = sa.derive_register(doc, None, "ship it")[0]
    assert rows[-1]["anchor"] == "seam"
