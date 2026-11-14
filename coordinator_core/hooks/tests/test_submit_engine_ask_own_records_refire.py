"""A refire of a run whose rows are executed and reviewed resumes that run at landing from its own
records, though no `resume.json` exists and its marker already reads `resumed`.

The halted lookup skips a `resumed` marker, so without `_reopen_own_records_run` `_resume_route` answers
(None, None) and the plain emit mints a fresh run; `sizing_fire._kept_rows` carries the run's kept rows
into the resume plan. Also pins the stored-refusal branch: a pack-read stall retries, a gate refusal
in backoff still refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import halted_run, kept_work, row_returns, sizing_fire
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = pytest.mark.complete

SIZING_REL = "state/sizings/2026-11-13-own-fixture.yaml"
RUN = "warp-2026-11-13-own-fixture-20261113T010101-abc123"
FRESH = "warp-2026-11-13-own-fixture-20261113T020202-def456"


def _put(path: Path, doc) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def _repo(tmp_path: Path, *, status: str = "resumed", verdict: str = "WARN") -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / SIZING_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / SIZING_REL).write_text("schema: sizing-object\n", encoding="utf-8")
    for rel in ("a/one.py", "a/two.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"# {rel}\n", encoding="utf-8")
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    _put(rd / "manifest.json", {"rows": [{"id": "C1", "writes": ["a/one.py"]}, {"id": "C2", "writes": ["a/two.py"]}]})
    row_returns.write(rd, {"C1": "DONE: C1", "C2": "DONE: C2"})
    _put(rd / "kept.json", {
        "chunks": {"C2": {"paths": {"a/two.py": kept_work.fingerprint(tmp_path, "a/two.py")}}},
        "plan_gate": None, "post_checkpoint": {},
    })
    _put(rd / "run-outcome.json", {
        "incomplete_chunks": [], "plan_path": "docs/plans/p.md",
        "inline_review": {"wave_reviews": [{"verdict": verdict}], "tests": {"status": "pass"}},
    })
    _put(rd / "halted.json", {"status": status, "halted": "commit", "sizing_rel": SIZING_REL})
    _put(rd / "run-stage.json", {"stage": "commit"})
    fresh = tmp_path / RECORD_DIR_ROOT / FRESH  # the orphan an earlier refire minted: no marker
    fresh.mkdir()
    _put(fresh / "run-stage.json", {"stage": "composed"})
    return tmp_path


def _payload(root: Path) -> dict:
    return {
        "tool_name": "Workflow", "cwd": str(root), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": SIZING_REL}},
    }


@pytest.fixture
def seams(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(mod, "_spawn_recompose", lambda root, run_id: spawned.append(run_id) or True)
    emitted: list = []

    def emit(params, root):
        emitted.append(params)
        return {"path": str(root / "emitted.js"), "run_id": "fresh", "fire_args": {}}

    return spawned, emitted, emit


def _runs(root: Path) -> list:
    return sorted(p.name for p in (root / RECORD_DIR_ROOT).iterdir())


def _marker(root: Path) -> dict:
    return json.loads((root / RECORD_DIR_ROOT / RUN / "halted.json").read_text(encoding="utf-8"))


def test_default_refire_reuses_the_resumed_run_with_no_resume_record(tmp_path, seams):
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    assert not (root / RECORD_DIR_ROOT / RUN / "resume.json").exists()
    env = mod.evaluate(_payload(root), emit=emit)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert RUN in hso["additionalContext"]
    assert RUN in Path(hso["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    assert spawned == [RUN] and emitted == []
    assert _runs(root) == [RUN, FRESH]
    assert _marker(root)["status"] == "halted"


def test_run_whose_review_failed_is_not_resumed_from_records(tmp_path, seams):
    spawned, emitted, emit = seams
    root = _repo(tmp_path, verdict="FAIL")
    mod.evaluate(_payload(root), emit=emit)
    assert spawned == [] and len(emitted) == 1
    assert _marker(root)["status"] == "resumed"


def test_landed_predecessor_is_never_reopened(tmp_path, seams):
    spawned, emitted, emit = seams
    root = _repo(tmp_path, status="landed")
    mod.evaluate(_payload(root), emit=emit)
    assert spawned == [] and len(emitted) == 1
    assert _marker(root)["status"] == "landed"


def test_kept_rows_of_the_own_records_run_reach_the_resume_plan(tmp_path):
    root = _repo(tmp_path)
    assert sizing_fire.own_records_run(root, SIZING_REL) == RUN
    assert sizing_fire._kept_rows(root, SIZING_REL, ["C1", "C2"]) == (("C2",), RUN)
    assert halted_run.STATUS_RESUMED == _marker(root)["status"]


def test_kept_rows_stay_empty_without_a_passed_review(tmp_path):
    root = _repo(tmp_path, verdict="FAIL")
    assert sizing_fire.own_records_run(root, SIZING_REL) is None
    assert sizing_fire._kept_rows(root, SIZING_REL, ["C1", "C2"]) == ((), None)


def _halted_with_stored_refusal(tmp_path: Path, reason: str) -> tuple[str, str]:
    """A halted run whose matching resume record holds a refusal in backoff, no script."""
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    sizing, plan = "state/sizings/2026-11-13-stall.yaml", "docs/plans/2026-11-13-stall.md"
    run_id = "warp-2026-11-13-stall-20261113T010101-abc123"
    (tmp_path / ".git").mkdir()
    for rel, text in ((sizing, "schema: sizing-object\n"), (plan, "# plan\n")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    rd = record_dir(tmp_path, run_id)
    halted_run.write_halt_marker(rd, halted="ledger-check", sizing_rel=sizing, plan_rel=plan, run_base_sha="a" * 40)
    fp = hrr.fingerprint_for_run(tmp_path, rd, sizing, plan)
    hrr.write(rd, hrr.build_record(
        composed_script_rel=None, hoisted_fragments=[], gate_verdict={"refused": reason},
        landed_state_sha=None, fingerprint=fp, resume_plan_rel=plan, retry_not_before=4102444800.0,
    ))
    return sizing, run_id


def _refire_stored(tmp_path: Path, seams, reason: str):
    spawned, emitted, emit = seams
    sizing, run_id = _halted_with_stored_refusal(tmp_path, reason)
    payload = {
        "tool_name": "Workflow", "cwd": str(tmp_path), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": sizing}},
    }
    return mod.evaluate(payload, emit=emit), spawned, emitted, run_id


def test_a_stored_pack_read_stall_is_retried_as_a_refire_of_the_same_run_never_a_fresh_one(tmp_path, seams):
    from coordinator_core.ops.dispatch_emit.halt_resume_build import STALL_REASON_PREFIX

    env, spawned, emitted, run_id = _refire_stored(tmp_path, seams, f"{STALL_REASON_PREFIX} PackReadStalled: e:")
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert spawned == [run_id] and emitted == []
    assert run_id in env["hookSpecificOutput"]["additionalContext"]


def test_a_stored_gate_refusal_in_backoff_still_refuses(tmp_path, seams):
    env, spawned, emitted, _ = _refire_stored(tmp_path, seams, "the sizing gate refused the recompose")
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert spawned == [] and emitted == []
