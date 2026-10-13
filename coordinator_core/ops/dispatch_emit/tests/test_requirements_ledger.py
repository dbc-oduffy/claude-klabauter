"""requirements_ledger: seed, workflow.revise, the view stages read, and the one pass rule."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

RUN = "ask-20261013T000000-abc123"


@pytest.fixture
def run(tmp_path):
    d = tmp_path / RUN_DIR_ROOT / RUN
    d.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    rl.seed(d, RUN, ["build the op", "keep it under 500ms"], origin=rl.ORIGIN_PM_VERBATIMS, all_of_pm_ask=False)
    return tmp_path, d


def _revise(root, **params):
    return rl._handler({"run_id": RUN, **params}, repo_root=root)


def test_seed_is_ordered_and_idempotent(run):
    root, d = run
    rl.seed(d, RUN, ["keep it under 500ms", "a raw ask"], origin=rl.ORIGIN_ASK)
    ledger = rl.read_ledger(d)
    assert [(i.id, i.text, i.origin) for i in ledger.items] == [
        ("R1", "build the op", "pm_verbatims"),
        ("R2", "keep it under 500ms", "pm_verbatims"),
        ("R3", "a raw ask", "ask"),
    ]
    assert ledger.all_of_pm_ask is False


def test_revise_appends_a_stamped_item_and_rewrites_the_view(run):
    root, d = run
    out = _revise(root, verbatim="also cover Windows paths")
    assert out["ok"] is True and out["item"] == "R3" and out["items"] == 3
    assert out["ledger"] == f"{RUN_DIR_ROOT}/{RUN}/{rl.LEDGER_VIEW_FILE}"
    line = json.loads((d / rl.LEDGER_FILE).read_text(encoding="utf-8").splitlines()[-1])
    assert line["origin"] == "revision" and line["at"].endswith("Z")
    view = (d / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert view.index("## R1") < view.index("## R3 (revision") and "> also cover Windows paths" in view


def test_revise_sets_all_of_pm_ask_alone(run):
    root, d = run
    out = _revise(root, all_of_pm_ask=True)
    assert out["ok"] is True and out["item"] is None and out["all_of_pm_ask"] is True
    assert "(all_of_pm_ask): yes" in (d / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "params, needle",
    [
        ({}, "needs a non-empty verbatim"),
        ({"verbatim": "   "}, "needs a non-empty verbatim"),
        ({"verbatim": 3}, "must be a string"),
        ({"run_id": "../x", "verbatim": "v"}, "safe path segment"),
        ({"run_id": "never-emitted", "verbatim": "v"}, "emit it first"),
    ],
)
def test_revise_refusals_name_the_cause(run, params, needle):
    root, _d = run
    out = rl._handler({"run_id": RUN, **params}, repo_root=root)
    assert needle in out["error"]


def test_run_live_reports_the_unconsumed_run_token(run):
    root, _d = run
    assert _revise(root, verbatim="a")["run_live"] is False
    run_token.mint(root, RUN, "s.mjs")
    assert _revise(root, verbatim="b")["run_live"] is True


def test_revise_spawns_nothing_and_holds_the_bar(run, monkeypatch):
    root, _d = run

    def _boom(*_a, **_k):
        raise AssertionError("workflow.revise spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    started = time.process_time()
    for n in range(20):
        assert _revise(root, verbatim=f"revision {n}")["ok"] is True
    assert (time.process_time() - started) / 20 < 0.5


def _verdict(*rows):
    return {"items": [dict(zip(("id", "status"), r[:2]), **(r[2] if len(r) > 2 else {})) for r in rows]}


def test_judge_passes_every_item_met(run):
    _root, d = run
    assert rl.judge(rl.read_ledger(d), _verdict(("R1", "met"), ("R2", "met"))) == {"pass": True, "failures": []}


def test_judge_fails_drift_and_an_unjudged_revision(run):
    root, d = run
    _revise(root, verbatim="late revision")
    out = rl.judge(rl.read_ledger(d), _verdict(("R1", "met"), ("R2", "drifted")))
    assert not out["pass"]
    assert {f["id"] for f in out["failures"]} == {"R2", "R3"}


def test_superseded_needs_a_later_revision(run):
    root, d = run
    _revise(root, verbatim="drop the 500ms bar")
    ledger = rl.read_ledger(d)
    assert rl.judge(ledger, _verdict(("R1", "met"), ("R2", "superseded", {"superseded_by": "R3"}), ("R3", "met")))["pass"]
    out = rl.judge(ledger, _verdict(("R1", "superseded", {"superseded_by": "R2"}), ("R2", "met"), ("R3", "met")))
    assert [f["id"] for f in out["failures"]] == ["R1"]


def test_all_of_pm_ask_makes_an_inessential_item_mandatory(run):
    root, d = run
    verdict = _verdict(("R1", "met"), ("R2", "not_met", {"essential": False}))
    assert rl.judge(rl.read_ledger(d), verdict)["pass"] is True
    _revise(root, all_of_pm_ask=True)
    out = rl.judge(rl.read_ledger(d), verdict)
    assert not out["pass"] and "all_of_pm_ask" in out["failures"][0]["reason"]


def test_not_met_defaults_to_essential(run):
    _root, d = run
    assert not rl.judge(rl.read_ledger(d), _verdict(("R1", "met"), ("R2", "not_met")))["pass"]


def test_judge_refuses_unknown_and_duplicate_ids_and_a_shapeless_verdict(run):
    _root, d = run
    ledger = rl.read_ledger(d)
    out = rl.judge(ledger, _verdict(("R1", "met"), ("R1", "met"), ("R2", "met"), ("R9", "met")))
    assert {(f["id"], f["reason"]) for f in out["failures"]} == {("R1", "judged twice"), ("R9", "names no ledger item")}
    assert not rl.judge(ledger, None)["pass"]


def test_a_torn_tail_line_does_not_lose_the_ledger(run):
    _root, d = run
    with (d / rl.LEDGER_FILE).open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "requirement", "te')
    assert [i.id for i in rl.read_ledger(d).items] == ["R1", "R2"]


def test_pm_words_prefers_verbatims_and_never_takes_an_em_paraphrase():
    assert rl.pm_words({"pm_verbatims": [{"text": " a "}, {"text": ""}], "intent": "x"}) == ["a"]
    assert rl.pm_words({"intent": "do x", "intent_source": "pm-verbatim"}) == ["do x"]
    assert rl.pm_words({"intent": "do x", "intent_source": "em-elaborated"}) == []
