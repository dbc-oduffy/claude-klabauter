"""The hook's resume route closes a halted run as superseded-by-refire, and mints fresh, when the engine stamp
the run ran under differs from the running engine or `args.fresh` is true; an unknown stamp resumes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import SUPERSEDED_BY_REFIRE

pytestmark = pytest.mark.complete

SIZING_REL = "state/sizings/2026-11-18-fresh-fixture.yaml"
RUN = "warp-2026-11-18-fresh-fixture-20261118T010101-abc123"
OLD, NEW = "a" * 40, "b" * 40


def _repo(tmp_path: Path, *, stamp: str | None, status: str = "halted") -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": "pm", "deliverable_id": "dlv-fresh-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-18", "mode": "pm"}},
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    marker = {"status": status, "halted": "commit", "sizing_rel": SIZING_REL}
    if stamp is not None:
        marker["engine_sha"] = stamp
    (rd / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": "commit"}), encoding="utf-8")
    return tmp_path


def _payload(root: Path, **args) -> dict:
    return {
        "tool_name": "Workflow", "cwd": str(root), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": SIZING_REL, "resume_run_id": RUN, **args}},
    }


def _route(root: Path, monkeypatch: pytest.MonkeyPatch, running: str | None, **args):
    monkeypatch.setattr(hrr, "engine_sha", lambda: running)
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: True)
    retired: list = []
    params = {"session_id": "s1", "ask": True, "sizing_path": SIZING_REL}
    envelope, result = mod._resume_route(root, SIZING_REL, _payload(root, **args), params, "s1", None, retired=retired)
    return envelope, result, retired, params


def _marker(root: Path) -> dict:
    return json.loads((root / RECORD_DIR_ROOT / RUN / "halted.json").read_text(encoding="utf-8"))


def test_fresh_is_an_accepted_args_key():
    assert "fresh" in mod._ARGS_KEYS


def test_a_differing_stamp_closes_the_run_and_returns_no_resume(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=OLD)
    envelope, result, retired, _ = _route(root, monkeypatch, NEW)
    assert (envelope, result) == (None, None)
    assert retired == [RUN]
    marker = _marker(root)
    assert marker["status"] == "superseded"
    assert marker["superseded_evidence"]["kind"] == SUPERSEDED_BY_REFIRE
    assert marker["superseded_evidence"]["prior_engine"] == OLD
    assert marker["superseded_evidence"]["engine"] == NEW


def test_a_resumed_marker_under_a_differing_stamp_closes_too(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=OLD, status="resumed")
    monkeypatch.setattr(mod, "_reopen_own_records_run", lambda r, sizing_rel: RUN)
    envelope, result, retired, _ = _route(root, monkeypatch, NEW)
    assert (envelope, result) == (None, None)
    assert retired == [RUN]
    assert _marker(root)["superseded_evidence"]["kind"] == SUPERSEDED_BY_REFIRE


def test_the_same_stamp_resumes_the_prior_run(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=OLD)
    envelope, _result, retired, _ = _route(root, monkeypatch, OLD)
    assert envelope is not None
    assert retired == []
    assert _marker(root)["status"] == "halted"


def test_fresh_true_closes_the_run_even_on_the_same_stamp(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=OLD)
    envelope, result, retired, params = _route(root, monkeypatch, OLD, fresh=True)
    assert (envelope, result) == (None, None)
    assert retired == [RUN]
    assert _marker(root)["superseded_evidence"]["kind"] == SUPERSEDED_BY_REFIRE
    assert "fresh" not in params


@pytest.mark.parametrize("running", [NEW, None])
def test_an_unstamped_run_resumes_not_closes(tmp_path, monkeypatch, running):
    root = _repo(tmp_path, stamp=None)
    envelope, _result, retired, _ = _route(root, monkeypatch, running)
    assert envelope is not None
    assert retired == []
    assert _marker(root)["status"] == "halted"


def test_an_unknown_running_engine_resumes(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=OLD)
    envelope, _result, retired, _ = _route(root, monkeypatch, None)
    assert envelope is not None
    assert retired == []


def test_fresh_on_a_run_that_cannot_close_falls_through_to_the_resume(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=OLD)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.halted_run.mark_superseded_by_refire", lambda *a, **k: False)
    envelope, _result, retired, _ = _route(root, monkeypatch, NEW)
    assert envelope is not None
    assert retired == []
    assert _marker(root)["status"] == "halted"
