"""Run 3f0406 (terminal commit 81027dc285, hand landing 4042fe4510) reported R1 met while the re-plan-appended
row C5's edit was withheld as `outside-row-footprint` and stayed dirty: a ledger resume's landing now reads
the plan's serving rows at landing time (C5 lands), and refuses `ledger-unmet` when a met item rests on a
path still withheld as a breach."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import footprint_breach
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_FOOTPRINT_FILE,
    RUN_DIR_ROOT,
    ManifestRow,
    StageManifest,
    record_dir,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    RUN2,
    _second_run,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    DLV,
    PLAN,
    _git,
    _write,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import _composed

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

C3_PATH = "a.py"
C4_PATH = "docs/note.md"
LANE = "x/lane.py"
LANE_TEST = "x/test_lane.py"
_VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": ["C5"]}]}


def _plan_with_rows(root, c5_register_rows: str) -> None:
    """The plan as the re-plan leg leaves it: plan-level `register_claims` serving R1, rows C3 and C4,
    and the appended C5 (no per-row `register_rows` unless `c5_register_rows` is given)."""
    text = (root / PLAN).read_text(encoding="utf-8")
    head, _, _ = text.partition("## Tasks")
    head = head.replace("---\n", '---\nregister_claims:\n  rows: ["R1"]\n', 1)
    rows = (
        f"- id: C3\n  title: t3\n  disposition: open\n  writes: [{C3_PATH}]\n  body: |\n    b\n"
        f"- id: C4\n  title: t4\n  disposition: open\n  writes: [{C4_PATH}]\n  body: |\n    b\n"
    )
    c5 = (
        f"- id: C5\n  title: t5\n  disposition: open\n{c5_register_rows}"
        f"  writes: [{LANE}, {LANE_TEST}]\n  body: |\n    b\n"
    )
    _write(root, PLAN, f"{head}## Tasks\n\n```yaml plan-tasks\n{rows}{c5}```\n")


def _stage_replanned_run(root, c5_register_rows: str) -> str:
    """Halted plan rows C3 and C4 record `leg-footprint.json`; C5 is appended after; C3's and C5's paths are dirty."""
    token = _second_run(root)
    _plan_with_rows(root, "")
    halted_writes = [C3_PATH, C4_PATH]
    target = record_dir(root, RUN2) / LEG_FOOTPRINT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"action": "dispatch", "writes": halted_writes}), encoding="utf-8")
    _plan_with_rows(root, c5_register_rows)

    paths = {"C3": (C3_PATH,), "C4": (C4_PATH,), "C5": (LANE, LANE_TEST)}
    marker = f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt"
    _write(
        root, marker,
        render_marker(CommitRequest(
            chunks=tuple(ChunkCommit(id=i, title="t", paths=p) for i, p in paths.items()),
            deliverable_id=DLV, plan_path=PLAN,
        )) + "\n",
    )
    rows = tuple(
        ManifestRow(id=i, agent_type="x", model="m", brief_path=f"briefs/{i}.md", writes=p, wave=0)
        for i, p in paths.items()
    )
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN2}", rows=rows, review_declared_paths=(), marker_path=marker,
    ).to_json()
    manifest["head_sha"] = _git(root, "rev-parse", "HEAD").strip()
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/manifest.json", json.dumps(manifest))
    _write(root, C3_PATH, "a = 2\n")
    _write(root, LANE, "lane = 1\n")
    _write(root, LANE_TEST, "def test_lane():\n    pass\n")
    return token


def test_row_appended_after_the_footprint_was_recorded_lands_with_no_unlanded_trailer(halted):  # noqa: F811
    resume = _composed(halted)
    token = _stage_replanned_run(halted, "")

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    assert "footprint_breach" not in out
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert {LANE, LANE_TEST} <= landed
    assert "Row-Unlanded: C5" not in _git(halted, "show", "-s", "--format=%B", out["sha"])


def test_met_resting_on_a_row_whose_paths_are_withheld_refuses_and_leaves_them_dirty(halted):  # noqa: F811
    resume = _composed(halted)
    token = _stage_replanned_run(halted, '  register_rows: ["R2"]\n')
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    failures = out["ledger_failures"]
    assert [f["id"] for f in failures] == ["R1"]
    assert failures[0]["reason"].startswith(footprint_breach.MET_ON_FOOTPRINT_BREACH_REASON)
    assert _git(halted, "rev-parse", "HEAD") == head
    status = _git(halted, "status", "--porcelain", "--", LANE, LANE_TEST)
    assert LANE in status and LANE_TEST in status
