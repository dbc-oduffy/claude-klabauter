"""A `workflow.revise` out_of_scope ruling withholds its row for every run bound to the sizing,
not only the run that recorded it."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN_A = "ask-20261016T000000-aaaaaa"
RUN_B = "warp-2026-10-16-oos-20261016T010000-bbbbbb"
REL = "state/sizings/2026-10-16-oos-across.yaml"
WORDS = "R1 is out of scope per the APM"
_DOC = {
    "schema": "sizing-object", "intent": "do it", "estimate": {"tshirt": "S", "provisional": True},
    "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
    "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    "pm_verbatims": [{"turn": 1, "text": "lint the thing"}],
}


def _repo(tmp_path, doc=None):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(doc or _DOC, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN_A)
    rl.bind(tmp_path, RUN_A, REL)
    return tmp_path


def _rule_out(repo, ids=("R1",)):
    out = rl._handler({"run_id": RUN_A, "verbatim": WORDS, "out_of_scope": list(ids)}, repo_root=repo)
    assert out["ok"]


def _rows(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8")).rows


def _ledger_under(repo, run_id):
    rl.start(repo, run_id)
    rl.bind(repo, run_id, REL)
    return rl.read_ledger(repo, repo / RECORD_DIR_ROOT / run_id)


def test_ruling_recorded_under_run_a_withholds_under_run_b(tmp_path):
    repo = _repo(tmp_path)
    _rule_out(repo)
    ledger = _ledger_under(repo, RUN_B)
    assert [i.id for i in ledger.items] == []
    assert ledger.withheld == ({"id": "R1", "reason": "withheld by out-of-scope ruling"},)
    assert rl.ruled_out_ids(_rows(repo)) == ("R1",)


def test_all_withheld_ledger_passes_an_empty_verdict_with_each_row_open(tmp_path):
    repo = _repo(tmp_path)
    _rule_out(repo)
    got = rl.judge(_ledger_under(repo, RUN_B), {"items": []})
    assert got["pass"] is True and got["failures"] == []
    assert got["open"] == [{"id": "R1", "reason": "withheld by out-of-scope ruling"}]


def test_all_withheld_ledger_ignores_a_verdict_naming_the_row(tmp_path):
    repo = _repo(tmp_path)
    _rule_out(repo)
    got = rl.judge(_ledger_under(repo, RUN_B), {"items": [{"id": "R1", "status": "met"}]})
    assert got["pass"] is True
    assert rl.NOT_A_ROW_REASON not in str(got["failures"])


@pytest.mark.parametrize("ruling", [
    {"source": "pm", "quote": "later", "ref": "R7", "on": "2026-10-16"},
    {"source": "pm", "quote": "later", "ref": "workflow.revise::out_of_scope", "on": "2026-10-16"},
    {"source": "pm", "quote": "later", "ref": f"workflow.revise:{RUN_A}:", "on": "2026-10-16"},
])
def test_deferred_row_without_a_revise_ruling_is_dropped_not_withheld(tmp_path, ruling):
    repo = _repo(tmp_path)

    def edit(rows):
        row = {**rows[0], "status": "deferred"}
        row["ruling"] = ruling
        return [row]

    rl._mutate_register(repo, REL, edit)
    ledger = _ledger_under(repo, RUN_B)
    assert ledger.items == () and ledger.withheld == ()
    assert rl.ruled_out_ids(_rows(repo)) == ()


def test_legacy_revision_row_tail_still_withholds_across_runs(tmp_path):
    repo = _repo(tmp_path)
    ruling = {"source": "pm", "quote": "q", "ref": f"workflow.revise:{RUN_A}:R2", "on": "2026-10-16"}
    rl._mutate_register(repo, REL, lambda rows: [{**rows[0], "status": "deferred", "ruling": ruling}])
    ledger = _ledger_under(repo, RUN_B)
    assert ledger.withheld == ({"id": "R1", "reason": "withheld by revision R2"},)


def test_truly_empty_register_still_fails(tmp_path):
    repo = _repo(tmp_path, {**_DOC, "pm_verbatims": []})
    got = rl.judge(_ledger_under(repo, RUN_B), {"items": []})
    assert got["pass"] is False
    assert len(got["failures"]) == 1 and got["failures"][0]["id"] is None
    assert rl.EMPTY_REGISTER_REASON in got["failures"][0]["reason"]
    assert REL in got["failures"][0]["reason"]
