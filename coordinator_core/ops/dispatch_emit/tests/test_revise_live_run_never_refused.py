"""workflow.revise on a live run never refuses a row or note, briefed or not: a briefed run queues a delta cycle."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

RUN = "ask-20261027T000000-def456"
REL = "state/sizings/2026-10-27-revise-live.yaml"


def _repo(tmp_path, tshirt):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": tshirt, "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path


def _brief(root):
    manifest = {"rows": [{"id": "X1", "brief_path": "scratch/warp/x/briefs/X1.md"}]}
    (record_dir(root, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("tshirt", ["S", "M"])
@pytest.mark.parametrize("params", [{"verbatim": "also cover Windows"}, {"note": "drop the CLAUDE.md edit"}])
@pytest.mark.parametrize("briefed", [False, True])
def test_live_revise_never_refuses_a_row_or_note(tmp_path, monkeypatch, tshirt, params, briefed):
    root = _repo(tmp_path, tshirt)
    if briefed:
        _brief(root)
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    out = rl._handler({"run_id": RUN, **params}, repo_root=root)
    assert out["ok"] is True and out["refused"] == []
    assert out["run_live"] is True
    assert "refire fresh" not in json.dumps(out)


def test_briefed_live_revise_queues_a_delta_cycle_on_the_run(tmp_path, monkeypatch):
    root = _repo(tmp_path, "M")
    _brief(root)
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    out = rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)
    request = json.loads((record_dir(root, RUN) / rl.DELTA_REQUEST_FILE).read_text(encoding="utf-8"))
    assert out["briefed"] is True and out["delta_cycle"] == request
    assert out["item"] in request["rows"] and request["review"] == "delta-only"
    again = rl._handler({"run_id": RUN, "verbatim": "and macOS"}, repo_root=root)
    merged = json.loads((record_dir(root, RUN) / rl.DELTA_REQUEST_FILE).read_text(encoding="utf-8"))
    assert {out["item"], again["item"]} <= set(merged["rows"])


def test_test_blockers_js_excludes_designed_red_nodes():
    from coordinator_core.ops.dispatch_emit.ask_compose import _test_blockers_js

    js = _test_blockers_js("_r", "_t")
    assert "_tSrc?.designed_red ?? []" in js.split("_tExcluded = ")[1].split("\n")[0]
    assert "_tExcluded.has(n)" in js


def test_unbriefed_live_revise_queues_no_delta(tmp_path, monkeypatch):
    root = _repo(tmp_path, "S")
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    out = rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)
    assert "delta_cycle" not in out and not (record_dir(root, RUN) / rl.DELTA_REQUEST_FILE).exists()
