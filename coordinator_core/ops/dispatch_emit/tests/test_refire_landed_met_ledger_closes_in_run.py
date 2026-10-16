"""A refire whose product is already at HEAD, with a met criterion, closes in-run.

The run's reviewed diff holds zero files, so the review stamp mints only on a met criterion. The
sizing is a sibling slice whose shared PM turn no slice row covers: the register gate must leave the
judge's met criterion alone, so the close mints and stamps the plan implemented with no re-judge.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run"
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
    _git(repo, "add", "-A", "docs", "state")
    _git(repo, "commit", "-q", "-m", "slice sizing")
    warp = repo / "scratch" / "warp"
    warp.mkdir(parents=True)
    (warp / "run.mjs").write_text((repo / script).read_text(encoding="utf-8"), encoding="utf-8")
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
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: True)

    real_mint = terminal_commit._mint_review_stamp
    state = {"refuse": True}

    def mint_once_refused(*args, **kwargs):
        if state["refuse"]:
            state["refuse"] = False
            return {"review_stamp": "refused", "review_stamp_refusal": "withheld for the fixture"}
        return real_mint(*args, **kwargs)

    monkeypatch.setattr(terminal_commit, "_mint_review_stamp", mint_once_refused)
    first = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID,
         "inline_review": _review()},
        repo_root=repo / ".git",
    )
    assert first["review_stamp"] == "refused" and first["sha"], first
    shutil.rmtree(repo / "scratch")
    return {"repo": repo}


def _refire(env) -> dict:
    return terminal_commit._handler(
        {"plan_path": PLAN_REL, "run_id": RUN_ID, "session_id": driver.SESSION_ID},
        repo_root=env["repo"] / ".git",
    )


def _rejudge_records(repo) -> list:
    root = repo / ".coordinator-local"
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file() and "rejudge" in p.name]


def test_refire_with_product_at_head_and_met_criterion_closes_in_run(tmp_path, monkeypatch):
    env = _landed(tmp_path, monkeypatch, slice_sizing=True)
    repo = env["repo"]

    out = _refire(env)

    assert out["review_stamp"] == "minted", out
    assert out["plan_status"] == "implemented", out
    text = (repo / PLAN_REL).read_text(encoding="utf-8")
    assert "status: implemented" in text and "review_stamp:" in text
    assert "status: met" in text.split("review_stamp:", 1)[1]
    assert _rejudge_records(repo) == []


def test_same_refire_without_ask_parent_refuses_on_the_zero_file_diff(tmp_path, monkeypatch):
    env = _landed(tmp_path, monkeypatch, slice_sizing=False)
    repo = env["repo"]

    out = _refire(env)

    assert out.get("review_stamp") != "minted", out
    assert "zero files in the reviewed diff" in json.dumps(out)
    assert "status: implemented" not in (repo / PLAN_REL).read_text(encoding="utf-8")
