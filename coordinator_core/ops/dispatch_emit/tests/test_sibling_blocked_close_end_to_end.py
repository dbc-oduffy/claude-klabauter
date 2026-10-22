"""A landing over a fan-out fixture feeds the real sibling_blockers leaf into the close reply and close-job inputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.warp_fire import fire_command

pytestmark = [pytest.mark.spawns_process]

PARENT = "ask-parent-20261016T000000-abcdef"
SCRIPT_REL = "scratch/warp/A.workflow.mjs"
SIB_PLAN = "docs/plans/sib.md"
OWN_SIZING = "state/sizings/A.yaml"
SIB_SIZING = "state/sizings/B.yaml"
C4_ROW = (
    "- id: C4\n  title: t4\n  change_kind: code-edit\n  surface: hook.py\n  disposition: open\n"
    "  writes:\n    - hook.py\n  depends_on_plan:\n    - plan: " + SIB_PLAN + "\n      chunk: C2\n"
)


def _run(rid, after=(), status=c.STATUS_READY):
    return c.SiblingRun(rid, rid, tuple(after), f"state/sizings/{rid}.yaml", status)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build(tmp_path, monkeypatch, sib_status: str) -> Path:
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
    plan = repo / driver.PLAN_REL
    text = plan.read_text(encoding="utf-8")
    plan.write_text(text.replace("\n```\n", "\n" + C4_ROW + "```\n", 1), encoding="utf-8")
    _write(repo / SIB_SIZING, f"plan: {SIB_PLAN}\n")
    _write(repo / OWN_SIZING, "plan: docs/plan.md\n")
    driver._git(repo, "add", driver.PLAN_REL, OWN_SIZING, SIB_SIZING)
    driver._git(repo, "commit", "-q", "-m", "c4 row and sizings")
    _write(repo / SCRIPT_REL, (repo / script).read_text(encoding="utf-8"))
    record = c.ParentRecord(PARENT, (_run("A"), _run("B", ["A"], c.STATUS_AFTER)))
    _write(repo / c.parent_record_rel(PARENT), json.dumps(record.to_json()))
    _write(repo / c.status_rel("A"), json.dumps({"status": c.STATUS_RUNNING, "parent_id": PARENT}))
    _write(repo / c.status_rel("B"), json.dumps({"status": sib_status, "parent_id": PARENT}))
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


def test_running_sibling_names_row_run_and_refire(tmp_path, monkeypatch):
    repo = _build(tmp_path, monkeypatch, c.STATUS_RUNNING)
    seen = _capture_inputs(monkeypatch)

    out = _fire(repo)

    assert out["committed"] is True, out.get("error")
    blocked = out["close"]["siblings"]["blocked_rows"]
    assert len(blocked) == 1
    assert blocked[0]["row"] == "C4"
    assert blocked[0]["run_id"] == "B"
    assert blocked[0]["via"] == "depends_on_plan"
    assert blocked[0]["refire"] == fire_command(OWN_SIZING)
    assert seen["sibling_blocked"] == blocked


def test_landed_sibling_returns_neither_key(tmp_path, monkeypatch):
    repo = _build(tmp_path, monkeypatch, c.STATUS_LANDED)
    seen = _capture_inputs(monkeypatch)

    out = _fire(repo)

    assert out["committed"] is True, out.get("error")
    assert "blocked_rows" not in (out["close"].get("siblings") or {})
    assert "sibling_blocked" not in seen
