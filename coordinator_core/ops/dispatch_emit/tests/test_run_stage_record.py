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

RUN = "run-1"
KEYS = {"session_id", "started_at", "stage", "last_activity_at"}


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-1")


def test_start_then_staged_then_halted(tmp_path):
    requirements_ledger.start(tmp_path, RUN)
    minted = c.read_run_stage(tmp_path, RUN)
    assert set(minted) == KEYS
    assert minted["stage"] == "minted" and minted["session_id"] == "sess-1"

    c.write_run_stage(tmp_path, RUN, "staged", steps_planned=3)
    staged = c.read_run_stage(tmp_path, RUN)
    assert set(staged) == KEYS | {"steps_planned"}
    assert staged["steps_planned"] == 3 and staged["started_at"] == minted["started_at"]

    rec = c.record_dir(tmp_path, RUN)
    halted_run.write_halt_marker(rec, halted="x", sizing_rel=None)
    halted = c.read_run_stage(tmp_path, RUN)
    assert halted["stage"] == "halted" and halted["steps_planned"] == 3
    assert halted["session_id"] == "sess-1"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reach_refused_terminal_commit_restores_the_prior_stage(repo):  # noqa: F811
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


def test_halted_then_landed(tmp_path):
    requirements_ledger.start(tmp_path, RUN)
    rec = c.record_dir(tmp_path, RUN)
    halted_run.write_halt_marker(rec, halted="x", sizing_rel=None)
    assert halted_run.mark_landed(rec, "a" * 40)
    assert c.read_run_stage(tmp_path, RUN)["stage"] == "landed"


def test_second_start_keeps_identity_and_run_json_keys(tmp_path, monkeypatch):
    requirements_ledger.start(tmp_path, RUN)
    first = c.read_run_stage(tmp_path, RUN)
    run_keys = set(json.loads((requirements_ledger.run_dir(tmp_path, RUN) / "run.json").read_text("utf-8")))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-2")
    requirements_ledger.start(tmp_path, RUN)
    second = c.read_run_stage(tmp_path, RUN)
    assert second["session_id"] == "sess-1" and second["started_at"] == first["started_at"]
    assert run_keys == {"sizing_rel", "all_of_pm_ask"}
    assert set(json.loads((requirements_ledger.run_dir(tmp_path, RUN) / "run.json").read_text("utf-8"))) == run_keys


def test_unresolvable_session_is_null(tmp_path, monkeypatch):
    monkeypatch.setattr("coordinator_core.session.core.resolve_session_id", lambda cwd=None: "")
    c.write_run_stage(tmp_path, RUN, "minted")
    assert c.read_run_stage(tmp_path, RUN)["session_id"] is None


def test_first_stamp_of_a_later_stage_still_sets_identity(tmp_path):
    c.write_run_stage(tmp_path, RUN, "staged", steps_planned=2)
    staged = c.read_run_stage(tmp_path, RUN)
    assert set(staged) == KEYS | {"steps_planned"}
    assert staged["session_id"] == "sess-1"


def test_bad_stage_and_unsafe_run_id_refused(tmp_path):
    with pytest.raises(ValueError):
        c.write_run_stage(tmp_path, RUN, "done")
    with pytest.raises(ValueError):
        c.write_run_stage(tmp_path, "../evil", "minted")
    assert c.read_run_stage(tmp_path, "../evil") is None
    assert c.read_run_stage(tmp_path, RUN) is None
    c.stamp_run_stage(tmp_path, "../evil", "minted")
