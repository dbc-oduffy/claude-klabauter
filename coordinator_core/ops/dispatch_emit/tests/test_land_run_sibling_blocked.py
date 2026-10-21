"""A landing carries sibling-blocked open rows on ``close.siblings.blocked_rows`` and the close-job inputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import land_run, sibling_blockers
from coordinator_core.warp_fire import fire_command

pytestmark = [pytest.mark.spawns_process]

PARENT = "ask-parent-20261016T000000-abcdef"
SCRIPT_REL = "scratch/warp/A.workflow.mjs"
ENTRY = {
    "row": "C4", "run_id": "B", "label": "B", "status": "running",
    "plan": "docs/plans/sib.md", "via": "depends_on_plan", "refire": fire_command("state/sizings/A.yaml"),
}


def _run(rid, after=(), status=c.STATUS_READY):
    return c.SiblingRun(rid, rid, tuple(after), f"state/sizings/{rid}.yaml", status)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(parent))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    dest = repo / SCRIPT_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text((repo / script).read_text(encoding="utf-8"), encoding="utf-8")
    record = c.ParentRecord(PARENT, (_run("A"), _run("B", ["A"], c.STATUS_AFTER)))
    rec = repo / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(record.to_json()), encoding="utf-8")
    status = repo / c.status_rel("A")
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({"status": c.STATUS_RUNNING, "parent_id": PARENT}), encoding="utf-8")
    return repo


def _fire(repo: Path) -> dict:
    return land_run._handler(
        {
            "script_path": SCRIPT_REL,
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
            "run_id": "A",
        },
        repo_root=repo / ".git",
    )


def _capture_inputs(monkeypatch) -> dict:
    seen: dict = {}
    real = land_run._close_job

    def spy(root, ctx):
        seen.update(ctx["inputs"])
        return real(root, ctx)

    monkeypatch.setattr(land_run, "_close_job", spy)
    return seen


def test_blocked_row_rides_close_siblings_and_close_job_inputs(env, monkeypatch):
    seen_args: list = []

    def fake(root, run_id, plan_rel):
        seen_args.append((run_id, plan_rel))
        return [dict(ENTRY)]

    monkeypatch.setattr(sibling_blockers, "sibling_blockers", fake)
    seen = _capture_inputs(monkeypatch)

    out = _fire(env)

    assert out["committed"] is True, out.get("error")
    assert out["close"]["siblings"]["blocked_rows"] == [ENTRY]
    assert out["close"]["siblings"]["released"] == [fire_command("state/sizings/B.yaml")]
    assert "blocked_rows" not in out["siblings"]
    assert seen["sibling_blocked"] == [ENTRY]
    assert seen_args and seen_args[0][0] == "A" and seen_args[0][1]


def test_no_blocked_row_returns_neither_key(env, monkeypatch):
    monkeypatch.setattr(sibling_blockers, "sibling_blockers", lambda *a: [])
    seen = _capture_inputs(monkeypatch)

    out = _fire(env)

    assert out["committed"] is True, out
    assert "blocked_rows" not in out["close"]["siblings"]
    assert "sibling_blocked" not in seen


def test_a_raising_leaf_leaves_the_commit_standing(env, monkeypatch):
    def boom(*a):
        raise RuntimeError("leaf failed")

    monkeypatch.setattr(sibling_blockers, "sibling_blockers", boom)
    seen = _capture_inputs(monkeypatch)

    out = _fire(env)

    assert out["committed"] is True
    assert "blocked_rows" not in out["close"]["siblings"]
    assert "sibling_blocked" not in seen
