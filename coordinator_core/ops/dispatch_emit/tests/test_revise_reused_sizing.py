"""A second ask on a reused sizing lands as a revision; a revision reaches the ledger a resumed run re-reads."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

REL = "state/sizings/2026-10-01-reuse.yaml"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    doc = {
        "schema": "sizing-object", "name": "reuse fixture", "intent": "exercise reuse",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-reuse-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return tmp_path


def _rows(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8")).rows


def _bound(repo, run_id, *, ask=None, relayed=None):
    rl.start(repo, run_id)
    return rl.bind(repo, run_id, REL, ask_text=ask, relayed_text=relayed)


def test_second_ask_on_a_reused_sizing_lands_as_a_revision(repo):
    _bound(repo, "run-one", ask="first ask")
    ledger = _bound(repo, "run-two", ask="second ask")
    assert [(r["source_text"], r["anchor"].split("@")[0]) for r in _rows(repo)] == [
        ("first ask", "ask"), ("second ask", "revision"),
    ]
    first, second = ledger.items
    assert (first.origin, second.origin) == ("ask", rl.ORIGIN_REVISION)
    verdict = {"items": [{"id": first.id, "status": "superseded", "superseded_by": second.id},
                         {"id": second.id, "status": "met"}]}
    assert rl.judge(ledger, verdict)["pass"] is True


def test_second_relayed_ask_on_a_reused_sizing_lands_as_a_revision(repo):
    _bound(repo, "run-one", relayed="first relay")
    _bound(repo, "run-two", relayed="second relay")
    assert [r["anchor"].split("@")[0] for r in _rows(repo)] == ["relayed", "revision"]


def test_rebinding_the_same_ask_adds_no_row(repo):
    _bound(repo, "run-one", ask="first ask")
    _bound(repo, "run-two", ask="first ask")
    assert [r["anchor"] for r in _rows(repo)] == ["ask"]


def test_revise_after_the_ledger_check_adds_a_row_the_cached_verdict_leaves_unjudged(repo):
    ledger = _bound(repo, "run-one", ask="first ask")
    verdict = {"items": [{"id": i.id, "status": "met"} for i in ledger.items]}
    assert rl.judge(ledger, verdict)["pass"] is True
    rl.revise(repo, "run-one", verbatim="also do this")
    live = rl.read_ledger(repo, rl.run_dir(repo, "run-one"))
    assert [i.origin for i in live.items] == ["ask", rl.ORIGIN_REVISION]
    outcome = rl.judge(live, verdict)
    assert outcome["pass"] is False and outcome["failures"][0]["reason"].startswith("unjudged")


def test_revise_on_a_finished_run_names_a_refire_whose_fresh_run_rereads_and_rejudges_the_ledger(repo):
    ledger = _bound(repo, "run-one", ask="first ask")
    cached = {"items": [{"id": i.id, "status": "met"} for i in ledger.items]}
    reply = rl._handler({"run_id": "run-one", "verbatim": "also do this"}, repo_root=repo)
    assert reply["run_live"] is False and reply["next"]["kind"] == "refire"
    fresh = _bound(repo, "run-two")
    assert [i.origin for i in fresh.items] == ["ask", rl.ORIGIN_REVISION]
    # A cached verdict of the first run cannot pass the fresh run's ledger.
    outcome = rl.judge(fresh, cached)
    assert outcome["pass"] is False and outcome["failures"][0]["reason"].startswith("unjudged")
