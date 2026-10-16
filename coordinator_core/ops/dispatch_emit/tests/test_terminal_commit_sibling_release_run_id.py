"""dispatch.terminal_commit releases a held successor by the ask run's own id, while the close-job
record stays keyed on the script stem."""

from __future__ import annotations

import copy
import json

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT
from coordinator_core.warp_fire import fire_command

pytestmark = [pytest.mark.spawns_process]

RID = driver.ASK_RUN
SUCCESSOR = "ask-20261016T000000-bbbbbb"
PARENT = "ask-parent-20261016T000000-cccccc"


def test_held_successor_fires_and_close_job_keeps_the_script_stem(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, _script = driver.build_ask_fixture(parent)
    outbox = parent / "lessons-outbox"
    outbox.mkdir()
    noop = parent / "noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(terminal_commit, "CLOSE_JOB_SCRIPT", str(noop))
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )

    script = f"{RUN_DIR_ROOT}/{RID}.workflow.mjs"
    (repo / script).write_text(
        f"// emitted script\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RID}/manifest.json\n", encoding="utf-8"
    )
    runs = (
        c.SiblingRun(RID, RID, (), f"state/sizings/{RID}.yaml", c.STATUS_READY),
        c.SiblingRun(SUCCESSOR, SUCCESSOR, (RID,), f"state/sizings/{SUCCESSOR}.yaml", c.STATUS_AFTER),
    )
    record = repo / c.parent_record_rel(PARENT)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(c.ParentRecord(PARENT, runs).to_json()), encoding="utf-8")
    copy_path = repo / c.run_parent_copy_rel(RID)
    copy_path.parent.mkdir(parents=True, exist_ok=True)
    copy_path.write_text(record.read_text(encoding="utf-8"), encoding="utf-8")
    status = repo / c.status_rel(RID)
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({"status": c.STATUS_RUNNING}), encoding="utf-8")

    out = terminal_commit._handler(
        {
            "script_path": script,
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
            "ledger_verdict": {"items": [{"id": "R1", "status": "met"}]},
        },
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    assert json.loads(status.read_text(encoding="utf-8")) == {"status": "landed", "sha": out["sha"]}
    assert out["close"]["siblings"]["released"] == [fire_command(f"state/sizings/{SUCCESSOR}.yaml")]
    assert (repo / ".coordinator-local" / "close-jobs" / f"{RID}.workflow.json").is_file()
