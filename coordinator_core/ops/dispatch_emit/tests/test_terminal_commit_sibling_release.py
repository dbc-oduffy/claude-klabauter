"""dispatch.terminal_commit closes a fan-out slice's own sibling status and names what it frees.

The ask script lives at ``scratch/warp/<run_id>.workflow.mjs``; the run id is the one the request
carries, never the script stem.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.warp_fire import fire_command

pytestmark = [pytest.mark.spawns_process]

PARENT = "ask-parent-20261016T000000-abcdef"
SCRIPT_REL = "scratch/warp/A.workflow.mjs"


def _run(rid, after=(), status=c.STATUS_READY):
    return c.SiblingRun(rid, rid, tuple(after), f"state/sizings/{rid}.yaml", status)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
    noop = parent / "noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(parent))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(terminal_commit, "CLOSE_JOB_SCRIPT", str(noop))
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


def _fire(repo: Path, **extra) -> dict:
    return terminal_commit._handler(
        {
            "script_path": SCRIPT_REL,
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
            **extra,
        },
        repo_root=repo / ".git",
    )


def test_terminal_commit_lands_status_and_names_released_sibling(env):
    out = _fire(env, run_id="A")

    assert out["committed"] is True
    status = json.loads((env / c.status_rel("A")).read_text(encoding="utf-8"))
    assert status["status"] == "landed" and status["sha"] == out["sha"]
    expected = {"parent_id": PARENT, "released": [fire_command("state/sizings/B.yaml")], "blocked": []}
    assert out["close"]["siblings"] == expected
    assert out["siblings"] == expected
    assert not (env / "scratch/warp/A.workflow").exists()


def _fail_review() -> dict:
    """A review the commit refuses after the close context is populated (no integration stem)."""
    review = copy.deepcopy(driver.INLINE_REVIEW)
    review["integration_stem"] = ""
    return review


def _status(repo: Path) -> dict:
    return json.loads((repo / c.status_rel("A")).read_text(encoding="utf-8"))


def test_refused_terminal_commit_writes_halted_and_names_blocked_successor(env):
    out = _fire(env, run_id="A", inline_review=_fail_review())

    assert out["committed"] is not True
    assert _status(env)["status"] == "halted"
    assert out["close"]["siblings"] == {
        "parent_id": PARENT,
        "released": [],
        "blocked": [{"run_id": "B", "slice_sizing": "state/sizings/B.yaml"}],
    }


def test_refused_then_landed_terminal_commit_ends_landed(env):
    refused = _fire(env, run_id="A", inline_review=_fail_review())
    assert refused["committed"] is not True and _status(env)["status"] == "halted"

    out = _fire(env, run_id="A")

    assert out["committed"] is True
    assert _status(env) == {"status": "landed", "sha": out["sha"], "parent_id": PARENT}
    assert out["close"]["siblings"]["released"] == [fire_command("state/sizings/B.yaml")]


def test_run_without_status_file_gets_no_siblings_block(env):
    (env / c.status_rel("A")).unlink()
    out = _fire(env, run_id="A")

    assert out["committed"] is True
    assert "siblings" not in out["close"] and "siblings" not in out
