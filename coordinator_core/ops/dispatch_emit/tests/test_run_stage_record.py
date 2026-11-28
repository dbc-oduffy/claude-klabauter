"""Pins run-stage.json: the minted/staged/halted/landed stamps and their key set."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c
from coordinator_core.ops.dispatch_emit import halted_run, requirements_ledger

from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _REVIEWED,
    _SID,
    RUN as LAND_RUN,
    _git,
    _verdict,
    _write,
    repo,  # noqa: F401  (fixture)
)
from pathlib import Path

RUN = "run-1"
KEYS = {"session_id", "started_at", "stage", "last_activity_at"}


@pytest.fixture(autouse=True)
def _session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-1")


def test_start_then_staged_then_halted(tmp_path: Path) -> None:
    requirements_ledger.start(tmp_path, RUN)
    minted = c.read_run_stage(tmp_path, RUN)
    assert set(minted) == KEYS
    assert minted["stage"] == "minted" and minted["session_id"] == "sess-1"

    c.write_run_stage(tmp_path, RUN, "staged", steps_planned=3)
    staged = c.read_run_stage(tmp_path, RUN)
    assert set(staged) == KEYS | {"steps_planned", "staged_at"}
    assert staged["steps_planned"] == 3 and staged["started_at"] == minted["started_at"]

    rec = c.record_dir(tmp_path, RUN)
    halted_run.write_halt_marker(rec, halted="x", sizing_rel=None)
    halted = c.read_run_stage(tmp_path, RUN)
    assert halted["stage"] == "halted" and halted["steps_planned"] == 3
    assert halted["session_id"] == "sess-1"


def test_plan_phase_stamp_keeps_minted_and_is_idempotent_and_minted_only(tmp_path: Path) -> None:
    requirements_ledger.start(tmp_path, RUN)
    assert c.stamp_plan_phase(tmp_path, RUN) is True
    rec = c.read_run_stage(tmp_path, RUN)
    assert rec["stage"] == "minted" and rec["plan_started_at"] == rec["last_activity_at"]
    before = (c.record_dir(tmp_path, RUN) / c.RUN_STAGE_FILE).read_bytes()
    assert c.stamp_plan_phase(tmp_path, RUN) is False
    assert (c.record_dir(tmp_path, RUN) / c.RUN_STAGE_FILE).read_bytes() == before

    c.write_run_stage(tmp_path, RUN, "staged", steps_planned=1)
    assert c.stamp_plan_phase_at(c.record_dir(tmp_path, RUN)) is False


def test_ask_stage_entry_stamps_the_plan_phase_on_a_minted_run(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import ask_stage

    (tmp_path / ".git").mkdir()
    requirements_ledger.start(tmp_path, RUN)
    out = ask_stage._handler({"run_id": RUN, "plan_path": "docs/plans/absent.md", "writes": []}, repo_root=tmp_path)
    assert "error" in out
    rec = c.read_run_stage(tmp_path, RUN)
    assert rec["stage"] == "minted" and rec["plan_started_at"] == rec["last_activity_at"]


def test_plan_phase_stamp_refuses_absent_record_and_unsafe_id(tmp_path: Path) -> None:
    assert c.stamp_plan_phase(tmp_path, RUN) is False
    assert c.stamp_plan_phase(tmp_path, "../evil") is False


def test_a_remint_clears_the_plan_phase_stamp(tmp_path: Path) -> None:
    requirements_ledger.start(tmp_path, RUN)
    c.stamp_plan_phase(tmp_path, RUN)
    requirements_ledger.start(tmp_path, RUN)
    assert "plan_started_at" not in c.read_run_stage(tmp_path, RUN)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reach_refused_terminal_commit_restores_the_prior_stage(repo) -> None:  # noqa: F811
    from coordinator_core.ops.dispatch_emit import reach_claims, terminal_commit

    _write(repo, "lib_apart.py", "def apart_fn():\n    return 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "libs")
    _write(repo, "a.py", "def a_tool():\n    return 1\n")
    _write(repo, f"{c.DELIVERY_NOTE_DIR}/{LAND_RUN}.md", "# Delivery\n\n`a_tool` reaches `apart_fn`.\n")
    c.write_run_stage(repo, LAND_RUN, "staged", steps_planned=2)

    out = terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
            "session_id": _SID, "ledger_verdict": _verdict({"essential": False}), "run_id": LAND_RUN,
        },
        repo_root=repo / ".git",
    )

    assert out["refused"] == reach_claims.REACH_CLAIM_REFUSED
    assert c.read_run_stage(repo, LAND_RUN)["stage"] == "staged"


def test_halted_then_landed(tmp_path: Path) -> None:
    requirements_ledger.start(tmp_path, RUN)
    rec = c.record_dir(tmp_path, RUN)
    halted_run.write_halt_marker(rec, halted="x", sizing_rel=None)
    assert halted_run.mark_landed(rec, "a" * 40)
    assert c.read_run_stage(tmp_path, RUN)["stage"] == "landed"


def test_second_start_keeps_identity_and_run_json_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requirements_ledger.start(tmp_path, RUN)
    first = c.read_run_stage(tmp_path, RUN)
    run_keys = set(json.loads((requirements_ledger.run_dir(tmp_path, RUN) / "run.json").read_text("utf-8")))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-2")
    requirements_ledger.start(tmp_path, RUN)
    second = c.read_run_stage(tmp_path, RUN)
    assert second["session_id"] == "sess-1" and second["started_at"] == first["started_at"]
    assert run_keys == {"sizing_rel", "all_of_pm_ask"}
    assert set(json.loads((requirements_ledger.run_dir(tmp_path, RUN) / "run.json").read_text("utf-8"))) == run_keys


def test_unresolvable_session_is_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinator_core.session.core.resolve_session_id", lambda cwd=None: "")
    c.write_run_stage(tmp_path, RUN, "minted")
    assert c.read_run_stage(tmp_path, RUN)["session_id"] is None


def test_first_stamp_of_a_later_stage_still_sets_identity(tmp_path: Path) -> None:
    c.write_run_stage(tmp_path, RUN, "staged", steps_planned=2)
    staged = c.read_run_stage(tmp_path, RUN)
    assert set(staged) == KEYS | {"steps_planned", "staged_at"}
    assert staged["session_id"] == "sess-1"


def test_bad_stage_and_unsafe_run_id_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        c.write_run_stage(tmp_path, RUN, "done")
    with pytest.raises(ValueError):
        c.write_run_stage(tmp_path, "../evil", "minted")
    assert c.read_run_stage(tmp_path, "../evil") is None
    assert c.read_run_stage(tmp_path, RUN) is None
    c.stamp_run_stage(tmp_path, "../evil", "minted")


def test_rag_use_is_written_kept_and_absent_by_default(tmp_path: Path) -> None:
    verdict = {"verdict": "rag_not_used", "rag_calls": 0, "per_worker": [{"label": "A", "rag_calls": 0}]}
    c.write_run_stage(tmp_path, RUN, "staged", steps_planned=1)
    assert "rag_use" not in c.read_run_stage(tmp_path, RUN)
    c.write_run_stage(tmp_path, RUN, "staged", rag_use=verdict)
    staged = c.read_run_stage(tmp_path, RUN)
    assert set(staged) == KEYS | {"steps_planned", "staged_at", "rag_use"} and staged["rag_use"] == verdict
    c.stamp_run_stage_at(c.record_dir(tmp_path, RUN), "halted")
    assert c.read_run_stage(tmp_path, RUN)["rag_use"] == verdict


def _gate_js_run(reply: str) -> dict | None:
    import shutil
    import subprocess

    from coordinator_core.ops.dispatch_emit import ask_compose

    if shutil.which("node") is None:
        pytest.skip("needs node to evaluate the composed build-stage gate")
    harness = (
        "(async () => {\n  let _halted = null; const _runId = 'r1', _SESSION_ID = 's', _sizingRel = null; const log = () => {};\n"
        f"  const engineOp = async (op, p) => {{ if (op !== 'dispatch.stage_rag_gate') throw new Error(op); return {reply}; }};\n"
        + ask_compose._STAGE_RAG_GATE_JS
        + "\n  console.log(JSON.stringify(_halted));\n})();"
    )
    proc = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_the_composed_build_stage_halts_on_rag_not_used_before_review() -> None:
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

    script = _compose()
    assert script.count("dispatch.stage_rag_gate") == 1
    assert script.index("dispatch.stage_rag_gate") < script.index("const _reviewPaths")
    halted = _gate_js_run("{ verdict: 'rag_not_used', rag_calls: 0, per_worker: [] }")
    assert halted["halted"] == "rag_not_used" and halted["rag_use"]["rag_calls"] == 0
    assert "project-rag" in halted["next_action"]


@pytest.mark.parametrize("verdict", ["ok", "degraded", "transcript_unreadable", "not_applicable"])
def test_the_composed_build_stage_does_not_halt_on_any_other_verdict(verdict: str) -> None:
    assert _gate_js_run("{ verdict: '%s', rag_calls: 2, per_worker: [] }" % verdict) is None
