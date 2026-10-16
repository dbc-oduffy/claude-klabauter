"""An empty register judges nothing, so ledger-check and the terminal ledger gate refuse it."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261016T000000-c1c1c1"
REL = "state/sizings/2026-10-16-empty.yaml"
_DOC = {
    "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
    "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
    "premise": {"provenance": "not-applicable", "evidence": "fixture"},
}


def _repo(tmp_path, doc=None, sizing_rel=REL, write_sizing=True):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    if write_sizing:
        (tmp_path / REL).write_text(yaml.safe_dump(doc or _DOC, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    dir_ = tmp_path / RECORD_DIR_ROOT / RUN
    rl._write_run(dir_, {"sizing_rel": sizing_rel, "all_of_pm_ask": False})
    return tmp_path, dir_


@pytest.mark.parametrize("all_of", [False, True])
def test_judge_fails_an_empty_register(all_of):
    out = rl.judge(rl.Ledger((), all_of), {"items": []})
    assert out["pass"] is False
    assert out["failures"] == [{"id": None, "reason": rl.EMPTY_REGISTER_REASON}]


def test_a_ledger_whose_only_row_is_withheld_still_passes():
    ledger = rl.Ledger((), False, None, ({"id": "R1", "reason": "withheld by revision R2"},))
    assert rl.judge(ledger, {"items": []})["pass"] is True


def test_the_unbound_empty_inbox_ledger_is_empty_and_fails(tmp_path):
    (tmp_path / ".git").mkdir()
    rl.start(tmp_path, RUN)
    ledger = rl.read_ledger(tmp_path, tmp_path / RECORD_DIR_ROOT / RUN)
    assert ledger.items == () and rl.judge(ledger, {"items": []})["pass"] is False


def test_gate_refuses_an_unreadable_sizing(tmp_path):
    root, dir_ = _repo(tmp_path, sizing_rel="state/sizings/absent.yaml", write_sizing=False)
    out = tc._ledger_gate(root, dir_, {"items": []})[0]
    assert out["refused"] == "ledger-unreadable" and out["committed"] is False


def test_write_view_returns_none_on_an_unreadable_sizing(tmp_path):
    root, dir_ = _repo(tmp_path, sizing_rel="state/sizings/absent.yaml", write_sizing=False)
    assert rl._write_view(root, dir_, RUN) is None


def test_gate_refuses_a_register_with_no_rows(tmp_path):
    root, dir_ = _repo(tmp_path)
    out = tc._ledger_gate(root, dir_, {"items": []})[0]
    assert out["refused"] == "ledger-unmet"
    assert rl.EMPTY_REGISTER_REASON in out["error"] and "None:" not in out["error"]


def test_gate_passes_a_one_row_slice_register(tmp_path):
    doc = {**_DOC, "em_analysis": {"ask_parent": "p"}, "intent": "slice work"}
    root, dir_ = _repo(tmp_path, doc)
    rl.bind(root, RUN, REL)
    ledger = rl.read_ledger(root, dir_)
    assert len(ledger.items) == 1
    assert tc._ledger_gate(root, dir_, {"items": [{"id": ledger.items[0].id, "status": "met"}]})[0] is None


def test_refusal_names_every_non_register_id(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawn")))
    doc = {**_DOC, "pm_verbatims": [{"turn": 1, "text": "deliver the one thing"}]}
    root, dir_ = _repo(tmp_path, doc)
    rl.bind(root, RUN, REL)
    ghosts = [f"X{n}" for n in range(12)]
    verdict = {"items": [{"id": "R1", "status": "met"}] + [{"id": g, "status": "met"} for g in ghosts]}
    out = tc._ledger_gate(root, dir_, verdict)[0]
    assert out["refused"] == "ledger-unmet"
    clause = out["error"].split("not register rows:")[1]
    assert all(g in clause for g in ghosts)
