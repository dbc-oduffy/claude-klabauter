"""The ask emit seeds the run's requirements ledger; the stage folds in an in-run sizing's PM words."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import stage

REL = "state/sizings/2026-10-01-seed.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
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


def _ledgers(repo):
    return sorted((repo / RUN_DIR_ROOT).glob(f"*/{rl.LEDGER_FILE}"))


def test_raw_ask_seeds_the_ask_and_the_flag(repo, capsys):
    assert cli.main(["--ask", "add a thing", "--all-of-pm-ask", "--repo-root", str(repo)]) == 0
    (path,) = _ledgers(repo)
    ledger = rl.read_ledger(path.parent)
    assert [(i.text, i.origin) for i in ledger.items] == [("add a thing", "ask")]
    assert ledger.all_of_pm_ask is True
    assert (path.parent / rl.LEDGER_VIEW_FILE).is_file()


def test_ask_sizing_seeds_the_pm_verbatims(repo, capsys):
    _put(repo)
    assert cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo)]) == 0
    (path,) = _ledgers(repo)
    ledger = rl.read_ledger(path.parent)
    assert [i.text for i in ledger.items] == ["make the seed verbatim"] and ledger.all_of_pm_ask is False


def test_all_of_pm_ask_is_an_ask_route_flag(repo, capsys):
    assert cli.main(["--plan", "docs/plans/x.md", "--all-of-pm-ask", "--repo-root", str(repo)]) == cli.EXIT_USAGE


def test_stage_folds_the_sizing_words_into_an_existing_ledger_only(repo):
    _put(repo)
    stage(repo, run_id="noledger", sizing_rel=REL, writes=["pkg/a.py"])
    assert not (repo / RUN_DIR_ROOT / "noledger" / rl.LEDGER_FILE).exists()
    run_dir = repo / RUN_DIR_ROOT / "ledgered"
    rl.seed(run_dir, "ledgered", ["the raw ask"], origin=rl.ORIGIN_ASK)
    stage(repo, run_id="ledgered", sizing_rel=REL, writes=["pkg/a.py"])
    assert [i.text for i in rl.read_ledger(run_dir).items] == ["the raw ask", "make the seed verbatim"]
