"""A verdict id outside the register is an advisory, never a judge failure or a refused landing."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_land_run_revise_note_met_lands import (  # noqa: F401
    CODE,
    R1_MET,
    _land,
    _stage,
    repo,
)

RUN = "ask-20261109T000000-aaaaaa"
REL = "state/sizings/2026-11-09-unknown-verdict-id.yaml"
_DOC = {
    "schema": "sizing-object", "intent": "do it", "estimate": {"tshirt": "S", "provisional": True},
    "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
    "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    "pm_verbatims": [{"turn": 1, "text": "first"}, {"turn": 2, "text": "second"}],
}
X1 = {
    "id": "X1", "status": "not_met", "anchor": "memo", "remaining": ["sha_reply", "sha_memo"],
}


def _ledger(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_DOC, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return rl.read_ledger(tmp_path, tmp_path / RECORD_DIR_ROOT / RUN)


def test_judge_passes_met_rows_plus_an_unregistered_x1_and_names_it(tmp_path):
    ledger = _ledger(tmp_path)
    assert [i.id for i in ledger.items] == ["R1", "R2"]

    got = rl.judge(ledger, {"items": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "met"}, X1]})

    assert got["pass"] is True and got["failures"] == []
    assert [a["id"] for a in got["advisory"]] == ["X1"]
    assert rl.advisory_text(got) == "verdict ids that are not register rows were ignored: X1"


def test_judge_without_unknown_ids_carries_no_advisory(tmp_path):
    got = rl.judge(_ledger(tmp_path), {"items": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "met"}]})

    assert got["pass"] is True and "advisory" not in got and rl.advisory_text(got) is None


def test_an_unmet_register_row_still_fails_beside_an_unregistered_id(tmp_path):
    got = rl.judge(
        _ledger(tmp_path), {"items": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "not_met"}, X1]},
    )

    assert got["pass"] is False
    assert [f["id"] for f in got["failures"]] == ["R2"]
    assert [a["id"] for a in got["advisory"]] == ["X1"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_default_land_path_commits_the_x1_shape_with_the_advisory(repo):  # noqa: F811
    token = _stage(repo, CODE)

    out = _land(repo, token, [R1_MET, X1])

    assert out.get("committed") is True and out.get("refused") is None, out
    assert out["ledger_advisory"] == ["verdict ids that are not register rows were ignored: X1"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_default_land_path_still_refuses_an_unmet_row_beside_x1(repo):  # noqa: F811
    token = _stage(repo, CODE)

    out = _land(repo, token, [{"id": "R1", "status": "not_met"}, X1])

    assert out.get("committed") is False and out.get("refused") == "ledger-unmet", out
    assert "R1: not met" in out["error"] and "X1" not in out["error"]
