"""workflow.revise `base_ref`: writes the run-dir marker, adds no row, refuses a bad value; the marker is
found again through the sizing from a refire."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import BASE_REF_FILE, RECORD_DIR_ROOT, read_base_ref, run_base_ref

RUN = "ask-20261029T000000-abc123"
REL = "state/sizings/2026-10-29-baseref.yaml"


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


def _other_run(root, name, sizing_rel=REL, base_ref=None, file="run.json"):
    d = root / RECORD_DIR_ROOT / name
    d.mkdir(parents=True)
    (d / file).write_text(json.dumps({"sizing_rel": sizing_rel}), encoding="utf-8")
    if base_ref:
        (d / BASE_REF_FILE).write_text(json.dumps({"base_ref": base_ref}), encoding="utf-8")
    return d


def test_revise_base_ref_writes_marker_and_adds_no_row(run):
    repo, d = run
    before = _revise(repo, verbatim="seed row")["items"]
    out = _revise(repo, base_ref="origin/main", **{"from": "peer-1"})
    assert out["ok"] is True and out["base_ref"] == "origin/main" and out["items"] == before
    marker = json.loads((d / BASE_REF_FILE).read_text(encoding="utf-8"))
    assert marker["base_ref"] == "origin/main" and marker["from"] == "peer-1" and marker["at"]
    assert read_base_ref(d) == "origin/main"


def test_a_later_revise_overwrites_the_marker(run):
    repo, d = run
    _revise(repo, base_ref="origin/main")
    _revise(repo, base_ref="abc1234")
    assert read_base_ref(d) == "abc1234"


@pytest.mark.parametrize("bad", ["", "   ", "origin main", " origin/main", 7, ["origin/main"]])
def test_bad_base_ref_refuses_and_writes_nothing(run, bad):
    repo, d = run
    out = _revise(repo, base_ref=bad)
    assert out.get("error") or out.get("ok") is False
    assert not (d / BASE_REF_FILE).exists()


def test_base_ref_on_an_unknown_run_refuses(run):
    repo, _ = run
    out = rl._handler({"run_id": "ask-20261029T000009-ffffff", "base_ref": "origin/main"}, repo_root=repo)
    assert out.get("error") or out.get("ok") is False


def test_refire_resolves_the_in_flight_runs_marker_through_the_sizing(run):
    repo, _ = run
    _revise(repo, base_ref="origin/main")
    assert run_base_ref(repo, REL) == "origin/main"
    assert run_base_ref(repo, "./" + REL) == "origin/main"


def test_walks_past_a_newer_markerless_refire_and_takes_the_newest_marker(run):
    repo, _ = run
    _revise(repo, base_ref="origin/main")
    _other_run(repo, "ask-20261029T000100-aaaaaa")
    assert run_base_ref(repo, REL) == "origin/main"
    _other_run(repo, "ask-20261029T000200-bbbbbb", base_ref="release")
    assert run_base_ref(repo, REL) == "release"


def test_a_halted_predecessor_known_only_by_its_halt_marker_counts(run):
    repo, _ = run
    _other_run(repo, "ask-20261029T000100-aaaaaa", base_ref="origin/main", file="halted.json")
    assert run_base_ref(repo, REL) == "origin/main"


def test_none_when_no_run_of_that_sizing_carries_one(run):
    repo, _ = run
    assert run_base_ref(repo, REL) is None
    _other_run(repo, "ask-20261029T000100-aaaaaa", sizing_rel="state/sizings/2026-10-29-other.yaml", base_ref="x")
    assert run_base_ref(repo, REL) is None
    assert run_base_ref(repo, None) is None and run_base_ref(repo, "") is None
    assert run_base_ref(repo, "state/sizings/undated.yaml") is None
