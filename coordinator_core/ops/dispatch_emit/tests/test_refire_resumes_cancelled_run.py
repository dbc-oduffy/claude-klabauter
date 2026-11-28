"""A refire through the hook's default entry point of a cancelled run's sizing: with `fresh` true, as dict or
JSON-string args, the cancelled run is closed superseded and the emit mints a new run; without it the run resumes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr, halted_run as hr, run_cancel
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = pytest.mark.complete

SIZING_REL = "state/sizings/2026-11-22-cancel-fixture.yaml"
RUN = "warp-2026-11-22-cancel-fixture-20261122T010101-abc123"
NEW_RUN = "warp-2026-11-22-cancel-fixture-20261122T020202-def456"
ENGINE = "a" * 40


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": "pm", "deliverable_id": "dlv-cancel-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-22", "mode": "pm"}},
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    marker = {
        "status": "halted", "halted": run_cancel.CANCELLED, "sizing_rel": SIZING_REL,
        "reason": "cancelled by workflow.cancel", "engine_sha": ENGINE,
    }
    (rd / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": "commit"}), encoding="utf-8")
    return tmp_path


def _marker(root: Path) -> dict:
    return json.loads((root / RECORD_DIR_ROOT / RUN / "halted.json").read_text(encoding="utf-8"))


def _fire(root: Path, monkeypatch: pytest.MonkeyPatch, args) -> tuple[dict, list]:
    monkeypatch.setattr(hrr, "engine_sha", lambda: ENGINE)
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: True)
    emitted: list = []

    def emit(params: dict, repo_root: Path) -> dict:
        emitted.append(params)
        return {"path": "scratch/new-run.js", "run_id": NEW_RUN}

    payload = {
        "tool_name": "Workflow", "cwd": str(root), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": args},
    }
    return mod.evaluate(payload, emit=emit), emitted


def _denied(envelope: dict) -> bool:
    return envelope.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(lambda a: a, id="dict"),
        pytest.param(json.dumps, id="json-string"),
    ],
)
def test_fresh_refire_of_a_cancelled_run_closes_it_and_mints_new(tmp_path, monkeypatch, shape):
    root = _repo(tmp_path)
    envelope, emitted = _fire(root, monkeypatch, shape({"sizing": SIZING_REL, "fresh": True}))
    assert not _denied(envelope), envelope
    assert _marker(root)["status"] == "superseded"
    assert emitted, "the refire reached the emit that mints the new run"
    assert all("fresh" not in p for p in emitted)
    assert all(p.get("predecessor_run") == RUN for p in emitted)
    assert run_cancel.cancelled_refusal(root / RECORD_DIR_ROOT / RUN) is None
    assert run_cancel.cancelled_refusal(root / RECORD_DIR_ROOT / NEW_RUN) is None


def test_a_refire_without_fresh_resumes_the_cancelled_run(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _fire(root, monkeypatch, json.dumps({"sizing": SIZING_REL}))
    assert _marker(root)["status"] != "superseded"


def test_a_plain_string_ask_stays_ask_text():
    payload = {"tool_input": {"name": "x", "args": "fix the thing, {not json}"}}
    assert mod._with_object_args(payload) is payload
    prose_json = {"tool_input": {"name": "x", "args": '{"unrelated": 1}'}}
    assert mod._with_object_args(prose_json) is prose_json


def test_a_cancelled_run_is_the_halted_run_a_refire_resumes_once(tmp_path):
    root = _repo(tmp_path)
    assert hr.halted_run_to_resume(root, SIZING_REL) == RUN
    assert hr.mark_resumed(root / RECORD_DIR_ROOT / RUN) is True
    assert run_cancel.cancelled_refusal(root / RECORD_DIR_ROOT / RUN) is None
    assert hr.halted_run_for(root, SIZING_REL) != RUN


def test_cancelled_is_a_mechanical_halt_whose_remedy_is_a_refire():
    assert hrc.is_mechanical(run_cancel.CANCELLED)
    assert hrc.HALT_REMEDY[run_cancel.CANCELLED] == (hrc.MECHANICAL, "refire-resumes-kept-work")


def _write_manifest(rec: Path, rows: list) -> None:
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "manifest.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")


def test_an_xs_refire_with_no_writes_recovers_the_predecessors_recorded_footprint(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_stage import _recorded_xs_writes

    root = _repo(tmp_path)
    own = root / RECORD_DIR_ROOT / NEW_RUN
    _write_manifest(root / RECORD_DIR_ROOT / RUN, [{"writes": ["a.py", "b.py"]}, {"writes": ["b.py", " "]}])
    _write_manifest(own, [{"writes": ["own.py"]}])
    assert _recorded_xs_writes(root, own, RUN) == ["a.py", "b.py"]
    assert _recorded_xs_writes(root, own, "") == ["own.py"]
    assert _recorded_xs_writes(root, root / RECORD_DIR_ROOT / "missing", "") == []
