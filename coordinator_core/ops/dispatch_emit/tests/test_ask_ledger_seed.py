"""The ask emit makes the run ledger-bearing; the rows live in the bound sizing's requirement_register."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_TEXT_FILE, RECORD_DIR_ROOT, RELAYED_ASK_FILE
from coordinator_core.ops.dispatch_emit.ask_stage import stage

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

REL = "state/sizings/2026-10-01-seed.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _put(repo):
    doc = {
        "schema": "sizing-object", "name": "seed fixture", "intent": "exercise the seed",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-seed-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "make the seed verbatim"}],
    }
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _runs(repo):
    return sorted((repo / RECORD_DIR_ROOT).glob(f"*/{rl.RUN_FILE}"))


def _rows(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8")).rows


def test_raw_ask_is_ledger_bearing_and_unbound_until_its_gate(repo, capsys):
    assert cli.main(["--ask", "add a thing", "--all-of-pm-ask", "--repo-root", str(repo)]) == 0
    (path,) = _runs(repo)
    ledger = rl.read_ledger(repo, path.parent)
    assert not ledger.items and ledger.sizing_rel is None and ledger.all_of_pm_ask is True
    assert (path.parent / rl.LEDGER_VIEW_FILE).is_file()
    _put(repo)
    rl.bind(repo, path.parent.name, REL, ask_text="add a thing")
    assert [(r["source_text"], r["anchor"]) for r in _rows(repo)] == [
        ("make the seed verbatim", "pm_verbatims"), ("add a thing", "ask"),
    ]


def test_ask_sizing_seeds_the_pm_verbatims(repo, capsys):
    _put(repo)
    assert cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo)]) == 0
    (path,) = _runs(repo)
    ledger = rl.read_ledger(repo, path.parent)
    assert [i.text for i in ledger.items] == ["make the seed verbatim"] and ledger.all_of_pm_ask is False
    assert ledger.sizing_rel == REL and [r["id"] for r in _rows(repo)] == ["R1"]


def test_relayed_ask_is_written_beside_no_ask_md_and_bound_as_relayed(repo, capsys):
    assert cli.main(["--ask", "RELAYED-MARK add a thing", "--ask-source", "relayed", "--repo-root", str(repo)]) == 0
    (path,) = _runs(repo)
    assert (path.parent / RELAYED_ASK_FILE).read_text(encoding="utf-8") == "RELAYED-MARK add a thing"
    assert not (path.parent / ASK_TEXT_FILE).exists()
    _put(repo)
    rl.bind(repo, path.parent.name, REL, relayed_text="RELAYED-MARK add a thing")
    assert [(r["source_text"], r["anchor"]) for r in _rows(repo)] == [
        ("RELAYED-MARK add a thing", "relayed"),
    ]
    ledger = rl.read_ledger(repo, path.parent)
    assert [(i.text, i.origin) for i in ledger.items][-1] == ("RELAYED-MARK add a thing", "relayed")


def test_an_unmarked_ask_still_writes_ask_md(repo, capsys):
    assert cli.main(["--ask", "add a thing", "--repo-root", str(repo)]) == 0
    (path,) = _runs(repo)
    assert (path.parent / ASK_TEXT_FILE).is_file() and not (path.parent / RELAYED_ASK_FILE).exists()


def test_ask_source_is_refused_off_the_ask_route_and_beside_a_sizing(repo, capsys):
    assert cli.main(["--plan", "docs/plans/x.md", "--ask-source", "relayed", "--repo-root", str(repo)]) == cli.EXIT_USAGE
    _put(repo)
    with pytest.raises(ValueError, match="relayed"):
        op._dispatch_emit({"ask": True, "sizing_path": REL, "ask_source": "relayed"}, repo_root=repo)
    with pytest.raises(ValueError, match="ask_source"):
        op._dispatch_emit({"ask": "x", "ask_source": "em"}, repo_root=repo)


def test_a_relayed_runs_sizing_takes_pm_verbatims_only_from_the_pm_turn_log(tmp_path, monkeypatch, capsys):
    import coordinator_core.ops.baton_pm_turns as pm_turns
    import coordinator_core.sizing_assemble as sizing_assemble
    from coordinator_core.session import record_homes
    from coordinator_core.session_baton import store

    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    sdir = tmp_path / ".git" / "coordinator-sessions" / "sid-rl"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-rl")
    pm_turns.append_turn("the PM said this", session_id="sid-rl")
    path = Path(record_homes.record_path(str(tmp_path), "sizings", "2026-10-13-relay.yaml"))
    path.write_text(_DRAFT, encoding="utf-8")
    code = sizing_assemble.main([
        "--tshirt", "M", "--intent", "RELAYED-MARK do it", "--premise-provenance", "read",
        "--premise-evidence", "read it", "--no-plan-hand-off", "--write", str(path),
    ])
    capsys.readouterr()
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert code == 0
    assert [v["text"] for v in doc["pm_verbatims"]] == ["the PM said this"]
    assert doc.get("intent_source") != "pm-verbatim"
    assert "RELAYED-MARK" not in "".join(v["text"] for v in doc["pm_verbatims"])


def test_all_of_pm_ask_is_an_ask_route_flag(repo, capsys):
    assert cli.main(["--plan", "docs/plans/x.md", "--all-of-pm-ask", "--repo-root", str(repo)]) == cli.EXIT_USAGE


def test_sibling_slices_sharing_one_verbatim_seed_disjoint_rows_unioning_to_the_parent(repo):
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as fc

    rels = []
    for i, intent in enumerate(("fix the a thing", "fix the b thing")):
        rel = f"state/sizings/2026-10-01-slice-{i}.yaml"
        doc = {
            "schema": "sizing-object", "name": f"slice {i}", "intent": intent, "intent_source": "em-elaborated",
            "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
            "fork": None, "xl_exit": None, "status": "draft",
            "premise": {"provenance": "not-applicable", "evidence": "fixture"},
            "pm_verbatims": [{"turn": 1, "text": "one shared PM verbatim"}],
            "em_analysis": {"ask_parent": fc.ask_parent_marker("ask-parent-x", f"run-{i}")},
        }
        (repo / rel).write_text(yaml.safe_dump(doc), encoding="utf-8")
        rl.start(repo, f"run-{i}")
        rl.bind(repo, f"run-{i}", rel)
        rels.append(rel)
    seeded = []
    for rel in rels:
        rows = rr.read_register((repo / rel).read_text(encoding="utf-8")).rows
        assert all("shared PM verbatim" not in r["source_text"] for r in rows)
        seeded.append([r["source_text"] for r in rows])
    assert seeded == [["fix the a thing"], ["fix the b thing"]]


def test_a_slice_holding_its_assigned_rows_seeds_nothing_more(repo):
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as fc

    _put(repo)
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["em_analysis"] = {"ask_parent": fc.ask_parent_marker("ask-parent-x", "run-9")}
    doc[rr.REGISTER_KEY] = {"sources": [{"doc": "p", "ref": "r"}], "rows": [{
        "id": "R4", "source_text": "assigned row", "anchor": "pm_verbatims", "surface": "internal",
        "status": "open", "claimed_by": [],
    }]}
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8")
    rl.start(repo, "run-9")
    rl.bind(repo, "run-9", REL)
    assert [r["id"] for r in _rows(repo)] == ["R4"]


def test_stage_binds_a_ledger_bearing_run_only(repo):
    _put(repo)
    stage(repo, run_id="noledger", sizing_rel=REL, writes=["pkg/a.py"])
    assert rr.read_register((repo / REL).read_text(encoding="utf-8")) is None
    run_dir = rl.start(repo, "ledgered")
    (run_dir / "ask.md").write_text("the raw ask", encoding="utf-8")
    stage(repo, run_id="ledgered", sizing_rel=REL, writes=["pkg/a.py"])
    assert [i.text for i in rl.read_ledger(repo, run_dir).items] == ["make the seed verbatim", "the raw ask"]
