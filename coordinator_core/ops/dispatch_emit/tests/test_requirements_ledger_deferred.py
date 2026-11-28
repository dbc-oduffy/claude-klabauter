"""judge() and apply_verdict(): the `deferred` status, with its deferrer, reason and blocking run ids."""
from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = pytest.mark.complete

RUN = "ask-20261123T000000-abc123"
REL = "state/sizings/2026-11-23-deferred.yaml"
DEFER = {
    "status": "deferred", "deferred_by": "G-EM", "deferral_reason": "waits on 690b0e and 0cbad1",
    "blocking_run_ids": ["690b0e", "0cbad1"],
}


@pytest.fixture
def run(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}, {"turn": 2, "text": "keep it under 500ms"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=True)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path, tmp_path / RECORD_DIR_ROOT / RUN


@pytest.fixture(autouse=True)
def _wired_commands_pass(monkeypatch):
    """The verdict's `wired` commands are placeholders; `apply_verdict` would demote them unrerunnable."""
    monkeypatch.setattr(
        rl, "_rerun_wired_commands",
        lambda repo_root, items: {i["command"]: {"passed": True, "reason": "", "output": ""} for i in items if i.get("command")},
    )


def _ledger(run):
    return rl.read_ledger(run[0], run[1])


def _rows(repo):
    return {r["id"]: r for r in rr.read_register((repo / REL).read_text(encoding="utf-8")).rows}


def _verdict(**r1):
    met = {"status": "met", "met_by": "a.py", "wired": True, "command": "c", "observed": "o"}
    return {"items": [{"id": "R1", **r1}, {"id": "R2", **met}, {"id": "R3", **met}]}


def test_a_deferred_item_with_all_three_fields_passes_and_is_listed_open(run) -> None:
    out = rl.judge(_ledger(run), _verdict(**DEFER))
    assert out["pass"] is True and out["failures"] == []
    assert out["open"] == [{
        "id": "R1", "reason": "deferred by G-EM: waits on 690b0e and 0cbad1 (blocked by 690b0e,0cbad1)",
        "deferred": True, "deferred_by": "G-EM", "blocking_run_ids": ["690b0e", "0cbad1"],
    }]


def test_a_single_blocking_run_id_string_is_accepted(run) -> None:
    assert rl.judge(_ledger(run), _verdict(**{**DEFER, "blocking_run_ids": "690b0e"}))["pass"] is True


@pytest.mark.parametrize("field", ["deferred_by", "deferral_reason", "blocking_run_ids"])
@pytest.mark.parametrize("blank", ["absent", "", "   ", [], [" "], 7])
def test_a_deferred_item_missing_any_field_fails_naming_it(run, field, blank) -> None:
    item = {k: v for k, v in DEFER.items() if k != field}
    if blank != "absent":
        item[field] = blank
    out = rl.judge(_ledger(run), _verdict(**item))
    assert out["pass"] is False
    assert out["failures"] == [{"id": "R1", "reason": f"deferred without {field}"}]
    assert out["open"] == []


def test_a_design_row_cannot_be_deferred(run) -> None:
    ledger = rl.Ledger(
        items=(rl.Item("D1", "words", "", "", anchor="design-registry:board-1"),), all_of_pm_ask=False,
        sizing_rel=REL,
    )
    out = rl.judge(ledger, {"items": [{"id": "D1", **DEFER}]})
    assert out["pass"] is False and out["open"] == []
    assert out["failures"][0]["reason"].endswith("never deferred")


def test_deferred_lands_the_met_rows_and_leaves_the_register_row_deferred(run) -> None:
    repo, _d = run
    verdict = _verdict(**DEFER)
    assert rl.judge(_ledger(run), verdict)["pass"] is True
    rl.apply_verdict(repo, REL, verdict, run_id=RUN, sha="abc1234")
    rows = _rows(repo)
    assert rows["R2"]["status"] == "met" and rows["R3"]["status"] == "met"
    assert rows["R1"]["status"] == rr.STATUS_DEFERRED
    assert rows["R1"]["ruling"] == {
        "source": "apm", "quote": "waits on 690b0e and 0cbad1",
        "ref": "ledger.deferred:G-EM:690b0e,0cbad1", "on": rows["R1"]["last_progress_at"][:10],
    }


def test_a_successor_lists_the_deferred_row_open_and_judges_only_the_rest(run) -> None:
    repo, _d = run
    rl.apply_verdict(repo, REL, _verdict(**DEFER), run_id=RUN, sha="abc1234")
    successor = _ledger(run)
    assert [i.id for i in successor.items] == ["R2", "R3"]
    assert successor.withheld == (
        {"id": "R1", "reason": "deferred by G-EM: waits on 690b0e and 0cbad1 (blocked by 690b0e,0cbad1)"},
    )
    rest = {"items": [i for i in _verdict()["items"] if i["id"] != "R1"]}
    out = rl.judge(successor, rest)
    assert out["pass"] is True and [o["id"] for o in out["open"]] == ["R1"]


def test_an_incomplete_deferral_writes_nothing_to_the_register(run) -> None:
    repo, _d = run
    rl.apply_verdict(repo, REL, _verdict(status="deferred", deferred_by="G-EM"), run_id=RUN, sha="abc1234")
    assert _rows(repo)["R1"]["status"] == rr.STATUS_OPEN
