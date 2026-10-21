"""A run whose product is already at HEAD, with a met criterion, closes through `dispatch.land_run`.

The run's reviewed diff holds zero files, so the review stamp mints only on a met criterion. The
sizing is a sibling slice whose shared PM turn no slice row covers: the register gate must leave the
judge's met criterion alone, so the close job mints and stamps the plan implemented with no re-judge.
"""

from __future__ import annotations

import copy
import json
import subprocess

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.run_close import close_job_path
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN_REL = "docs/plans/plan.md"
SIZING_REL = "state/sizings/2026-10-16-slice.yaml"

# The pm-verbatim intent cites turn 29, so a non-slice owes it; no row covers it.
_SIZING = """schema: sizing-object
intent: please stop leaving things still owed
intent_source: pm-verbatim
estimate:
  tshirt: S
  provisional: true
route: spec-dispatch
detents: []
fork: null
xl_exit: null
status: routed
premise:
  provenance: read
  evidence: fixture
{parent}pm_verbatims:
  - {{turn: 29, text: 'please stop leaving things still owed'}}
  - {{turn: 30, text: 'ship the slice row'}}
requirement_register:
  sources:
    - {{doc: docs/fixture.md, ref: fixture}}
  rows:
    - {{id: R1, source_text: ship the slice row, anchor: a1, surface: api, status: open, claimed_by: [docs/plans/plan.md], wired: false}}
"""

_PLAN_HEAD = f"""---
title: p
status: executing
plan_id: {driver.PLAN_ID}
sizing_object: {SIZING_REL}
register_claims:
  sizing: {SIZING_REL}
  rows: [R1]
prime_exit_criterion:
  text: a.py exists
---
"""


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, **no_console_creationflags()
    )


def _review() -> dict:
    review = copy.deepcopy(driver.INLINE_REVIEW)
    review["prep"] = {"run_base_sha": "a" * 40, "product_files": 0, "foreign_claims": [], "slice_files": []}
    review["delivery"] = {"verdict": "PASS", "product_files": 0, "claims_unbacked": 0}
    review["criterion"]["register_rows"] = [
        {"id": "R1", "claim": "this-plan", "status": "met", "wired": True, "surface": "api",
         "observed_ref": "a.py::f"}
    ]
    return review


def _landed(tmp_path, monkeypatch, *, slice_sizing: bool) -> dict:
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
    plan = (repo / driver.PLAN_REL).read_text(encoding="utf-8")
    body = plan.split("---\n", 2)[2]
    (repo / PLAN_REL).parent.mkdir(parents=True)
    (repo / driver.PLAN_REL).unlink()
    (repo / PLAN_REL).write_text(_PLAN_HEAD + body, encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path=PLAN_REL)
    (repo / script).write_text("// emitted script\n" + render_marker(request) + "\n", encoding="utf-8")
    (repo / SIZING_REL).parent.mkdir(parents=True)
    parent_line = "em_analysis:\n  ask_parent: ask-parent-x ask-x\n" if slice_sizing else ""
    (repo / SIZING_REL).write_text(_SIZING.format(parent=parent_line), encoding="utf-8")
    _git(repo, "add", "-A", "docs", "state", "a.py")
    _git(repo, "commit", "-q", "-m", "slice sizing and product")
    outbox = parent / "lessons-outbox"
    outbox.mkdir()
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: True)
    out = land_run._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID,
         "inline_review": _review(), "allow_empty": True},
        repo_root=repo / ".git",
    )
    assert out.get("committed") is True and out["sha"], out
    return {"repo": repo, "out": out}


def _close(env) -> dict:
    """Run the landing's close job in-process; return its settled leg statuses and details."""
    repo, run_id = env["repo"], env["out"]["close"]["run_id"]
    record = json.loads((repo / close_job_path(run_id)).read_text(encoding="utf-8"))
    close_background._handler(
        {"run_id": run_id, "claimed_batons": record["claimed_batons"], "source_memo": record.get("source_memo")},
        repo_root=repo / ".git",
    )
    settled = json.loads((repo / close_job_path(run_id)).read_text(encoding="utf-8"))
    return {leg: (r["status"], r["detail"]) for leg, r in settled["legs"].items()}


def _rejudge_records(repo) -> list:
    root = repo / ".coordinator-local"
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file() and "rejudge" in p.name]


def test_run_with_product_at_head_and_met_criterion_closes_in_run(tmp_path, monkeypatch):
    env = _landed(tmp_path, monkeypatch, slice_sizing=True)
    repo = env["repo"]

    legs = _close(env)

    assert legs["review-stamp"][0] == "ran", legs
    text = (repo / PLAN_REL).read_text(encoding="utf-8")
    assert "status: implemented" in text and "review_stamp:" in text
    assert "status: met" in text.split("review_stamp:", 1)[1]
    assert _rejudge_records(repo) == []


def test_same_run_without_ask_parent_leaves_the_plan_unimplemented_on_the_uncovered_turn(tmp_path, monkeypatch):
    """Without an ask-parent no register row covers PM turn 29: the close job's plan-reconciliation leg
    fails naming the turn and the plan stays unimplemented (the old refire refused at the stamp instead)."""
    env = _landed(tmp_path, monkeypatch, slice_sizing=False)
    repo = env["repo"]

    legs = _close(env)

    assert legs["plan-reconciliation"][0] == "failed", legs
    assert "PM turn 29 is covered by no register row" in legs["plan-reconciliation"][1]
    assert "status: implemented" not in (repo / PLAN_REL).read_text(encoding="utf-8")


def test_zero_file_pass_anchor_lands_an_empty_delta_commit_carrying_the_review_trailer(tmp_path, monkeypatch):
    import hashlib

    env = _landed(tmp_path, monkeypatch, slice_sizing=True)
    repo = env["repo"]
    before = env["out"]["sha"]
    (repo / "anchor.mjs").write_text("// emitted\n", encoding="utf-8")
    review = _review()
    review["integration_stem"] = f"{driver.PLAN_ID}.review-wave-bookkeeping"
    review["prep_sidecar"] = None
    review["wave_sidecar_paths"] = []

    out = land_run._handler(
        {"script_path": "anchor.mjs", "plan_path": PLAN_REL, "incomplete_chunks": [],
         "session_id": "99999999-2222-3333-4444-555555555555", "inline_review": review},
        repo_root=repo / ".git",
    )

    assert out.get("committed") is True and out["sha"] != before, out
    body = subprocess.run(
        ["git", "show", "-s", "--format=%B", out["sha"]], cwd=repo, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    record = next((repo / ".coordinator-local" / "subagent-share" / "99999999-2222-3333-4444-555555555555").glob("*.md"))
    assert f"Review: {hashlib.sha256(record.read_bytes()).hexdigest()}" in body
