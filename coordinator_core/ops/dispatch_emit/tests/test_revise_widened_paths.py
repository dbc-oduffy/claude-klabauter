"""workflow.revise `widened`: recorded in the run's widen record, unknown keys refused, and the
halt-composed revise next_action only carries keys the op accepts."""

from __future__ import annotations

import json
import re

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import in_run_widen
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261029T000000-abc123"
REL = "state/sizings/2026-10-29-widen.yaml"
PATH = "pkg/scripts/install_plugin.py"


@pytest.fixture
def run(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "do x"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path, tmp_path / RECORD_DIR_ROOT / RUN


def _revise(root, **params):
    return rl._handler({"run_id": RUN, **params}, repo_root=root)


def test_widened_is_recorded_where_the_resumed_run_reads_it(run):
    repo, d = run
    out = _revise(repo, widened=[{"row": "C1", "path": PATH}])
    assert out["ok"] is True and out["widened"] == 1 and out["refused"] == []
    lines = (d / in_run_widen.WIDEN_RECORD_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [{"row": "C1", "path": PATH}]


def test_a_malformed_widened_item_is_refused_not_dropped(run):
    repo, d = run
    out = _revise(repo, widened=[{"row": "C1", "path": "../escape.py"}])
    assert out["ok"] is False and out["refused"][0]["input"] == "widened"
    assert not (d / in_run_widen.WIDEN_RECORD_NAME).exists()


def test_an_unknown_key_is_refused_naming_it_and_the_accepted_keys(run):
    repo, _d = run
    out = _revise(repo, widenned=[{"row": "C1", "path": PATH}])
    assert "params.widenned is not accepted" in out["error"] and "widened" in out["error"]


def test_revise_next_action_params_are_accepted_fields():
    emitted = set(re.findall(r"[{,]\s*(\w+):", ask_compose.REVISE_NEXT_ACTION_JS.split("params:", 1)[1]))
    emitted |= {"widened"}
    assert emitted <= {f.name for f in rl._PARAMS}
