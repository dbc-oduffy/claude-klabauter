"""workflow.revise `out_of_scope`: named register rows are deferred with the revision's words as the
ruling, leave the ledger's judged items, and ride on `judge()` as open."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261015T000000-abc123"
REL = "state/sizings/2026-10-15-oos.yaml"
WORDS = "R1 lint half is out of scope per the APM"


def _sizing():
    return {
        "schema": "sizing-object", "intent": "do it", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "lint the thing"}, {"turn": 2, "text": "close the thing"}],
    }


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_sizing(), sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _rows(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8")).rows


def _revise(repo, **params):
    return rl._handler({"run_id": RUN, **params}, repo_root=repo)


def _ledger(repo):
    return rl.read_ledger(repo, repo / RECORD_DIR_ROOT / RUN)


def _met(ids):
    return {"items": [{"id": i, "status": "met", "wired": True} for i in ids]}


def test_out_of_scope_without_verbatim_refuses(repo):
    out = _revise(repo, out_of_scope=["R1"])
    assert "error" in out and "verbatim" in out["error"]
    assert _rows(repo)[0]["status"] == "open"


def test_unknown_id_refuses_and_writes_nothing(repo):
    before = (repo / REL).read_text(encoding="utf-8")
    out = _revise(repo, verbatim=WORDS, out_of_scope=["R9"])
    assert "R9" in out["error"]
    assert (repo / REL).read_text(encoding="utf-8") == before


def test_named_row_reads_deferred_with_ruling_and_leaves_items(repo):
    out = _revise(repo, verbatim=WORDS, out_of_scope=["R1"])
    assert out["ok"] and out["withheld"] == ["R1"]
    rows = _rows(repo)
    r1 = rows[0]
    assert r1["status"] == "deferred"
    assert r1["ruling"]["quote"] == WORDS
    assert r1["ruling"]["ref"] == f"workflow.revise:{RUN}:{rows[-1]['id']}"
    assert len(rows) == 3 and rows[-1]["anchor"].startswith("revision@")
    ledger = _ledger(repo)
    assert "R1" not in [i.id for i in ledger.items]
    assert ledger.withheld[0]["reason"] == f"withheld by revision {rows[-1]['id']}"
    view = (repo / RECORD_DIR_ROOT / RUN / "requirements.md").read_text(encoding="utf-8")
    assert "Out of scope" in view and "R1: withheld by revision" in view


def test_judge_passes_verdict_that_omits_it_and_lists_it_open(repo):
    _revise(repo, verbatim=WORDS, out_of_scope=["R1"])
    ledger = _ledger(repo)
    got = rl.judge(ledger, _met([i.id for i in ledger.items]))
    assert got["pass"] is True
    assert got["open"] == [{"id": "R1", "reason": f"withheld by revision {ledger.items[-1].id}"}]


def test_judge_tolerates_verdict_that_judges_it(repo):
    _revise(repo, verbatim=WORDS, out_of_scope=["R1"])
    ledger = _ledger(repo)
    verdict = _met([i.id for i in ledger.items])
    verdict["items"].append({"id": "R1", "status": "not_met"})
    got = rl.judge(ledger, verdict)
    assert got["pass"] is True
    assert [o["id"] for o in got["open"]] == ["R1"]


def test_met_row_cannot_be_withheld(repo):
    rl._mutate_register(repo, REL, lambda rows: [{**rows[0], "status": "met"}, *rows[1:]])
    out = _revise(repo, verbatim=WORDS, out_of_scope=["R1"])
    assert "already met" in out["error"]


def test_regression_unmet_essential_row_still_fails(repo):
    ledger = _ledger(repo)
    got = rl.judge(ledger, {"items": [{"id": "R1", "status": "not_met"}, {"id": "R2", "status": "met"}]})
    assert got["pass"] is False and got["failures"][0]["id"] == "R1"
    assert got["open"] == []


def test_regression_gated_withheld_row_behaves_as_before(repo):
    ledger = _ledger(repo)
    verdict = {"items": [{"id": "R1", "status": "not_met", "withheld_by": "C9"}, {"id": "R2", "status": "met"}]}
    assert rl.judge(ledger, verdict, gated=["C9"]) == {
        "pass": True, "failures": [], "open": [{"id": "R1", "reason": "withheld_by C9"}],
    }
    assert rl.judge(ledger, verdict, gated=[])["pass"] is False
