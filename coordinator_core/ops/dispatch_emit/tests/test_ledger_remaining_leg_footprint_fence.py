"""A ledger resume's landing withholds a declared path changed since the halt that the recorded leg
footprint omits (`outside-row-footprint`), names it in the reply, and still lands in-footprint paths;
with no footprint artifact it reports `footprint: "unrecorded"` and withholds nothing on that ground."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_FOOTPRINT_FILE,
    OUTSIDE_ROW_FOOTPRINT,
    RUN_DIR_ROOT,
    UNCHANGED_SINCE_HALT_REASON,
    ManifestRow,
    StageManifest,
    record_dir,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
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
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import (
    B,
    _composed,
    _terminal,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _stage_refire(root, *, change_in: bool, change_out: bool) -> str:
    """Stage `RUN2` declaring `a.py` and `B`; dirty the chosen ones; return the run token."""
    token = _second_run(root)
    _write(
        root, f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt",
        render_marker(CommitRequest(
            chunks=(ChunkCommit(id="C1", title="t", paths=("a.py", B)),), deliverable_id=DLV, plan_path=PLAN
        )) + "\n",
    )
    row = ManifestRow(id="C1", agent_type="x", model="m", brief_path="briefs/C1.md", writes=("a.py", B), wave=0)
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN2}", rows=(row,), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt",
    ).to_json()
    manifest["head_sha"] = _git(root, "rev-parse", "HEAD").strip()
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/manifest.json", json.dumps(manifest))
    if change_in:
        _write(root, "a.py", "a = 2\n")
    if change_out:
        _write(root, B, "b = 2\n")
    return token


def _record_footprint(root, writes) -> None:
    target = record_dir(root, RUN2) / LEG_FOOTPRINT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"action": "dispatch", "writes": list(writes)}), encoding="utf-8")


def test_out_of_footprint_path_is_withheld_and_named_while_the_in_footprint_path_lands(halted):  # noqa: F811
    resume = _composed(halted)
    token = _stage_refire(halted, change_in=True, change_out=True)
    _record_footprint(halted, ["a.py"])

    out = _terminal(halted, token, resume)

    assert out["committed"] is True, out
    assert out["footprint_breach"] == [B]
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert "a.py" in landed and B not in landed
    assert f"{B} {OUTSIDE_ROW_FOOTPRINT}" in _git(halted, "show", "-s", "--format=%B", out["sha"])
    assert (halted / B).read_text(encoding="utf-8") == "b = 2\n"


def test_out_of_footprint_change_alone_is_not_work_since_the_halt(halted):  # noqa: F811
    resume = _composed(halted)
    token = _stage_refire(halted, change_in=False, change_out=True)
    _record_footprint(halted, ["a.py"])
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal(halted, token, resume)

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert out["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    assert _git(halted, "rev-parse", "HEAD") == head


def test_absent_footprint_artifact_reports_unrecorded_and_withholds_nothing_on_that_ground(halted):  # noqa: F811
    resume = _composed(halted)
    token = _stage_refire(halted, change_in=True, change_out=True)

    out = _terminal(halted, token, resume)

    assert out["committed"] is True, out
    assert out["footprint"] == "unrecorded"
    assert "footprint_breach" not in out
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert {"a.py", B} <= landed


def _append_serving_row(root) -> None:
    """Append a spine row serving `R1` that declares `B`, after the footprint artifact was recorded."""
    text = (root / PLAN).read_text(encoding="utf-8")
    row = f"- id: C2\n  title: t2\n  disposition: open\n  register_rows: [R1]\n  writes: [{B}]\n  body: |\n    b\n"
    _write(root, PLAN, text.replace("```\n", row + "```\n", 1) if text.endswith("```\n") else text + row)


def _terminal_with(root, token: str, resume: dict, verdict: dict) -> dict:
    return land_run._handler(
        {
            "script_path": "run2.mjs", "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": reviewed(),
            "session_id": "11111111-2222-3333-4444-555555555555", "ledger_verdict": verdict,
            "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def test_row_appended_to_the_plan_after_the_footprint_was_recorded_lands(halted):  # noqa: F811
    resume = _composed(halted)
    token = _stage_refire(halted, change_in=True, change_out=True)
    _record_footprint(halted, ["a.py"])
    _append_serving_row(halted)

    out = _terminal(halted, token, resume)

    assert out["committed"] is True, out
    assert "footprint_breach" not in out
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert {"a.py", B} <= landed
    assert "Row-Unlanded" not in _git(halted, "show", "-s", "--format=%B", out["sha"])


def test_met_resting_on_a_breach_path_refuses_and_leaves_it_dirty(halted):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import footprint_breach

    resume = _composed(halted)
    token = _stage_refire(halted, change_in=True, change_out=True)
    _record_footprint(halted, ["a.py"])
    head = _git(halted, "rev-parse", "HEAD")
    verdict = {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": [B]}]}

    out = _terminal_with(halted, token, resume, verdict)

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert [f["id"] for f in out["ledger_failures"]] == ["R1"]
    assert out["ledger_failures"][0]["reason"].startswith(footprint_breach.MET_ON_FOOTPRINT_BREACH_REASON)
    assert "since_halt_close" not in out and not out.get(land_run.ALREADY_LANDED_KEY)
    assert _git(halted, "rev-parse", "HEAD") == head
    assert (halted / B).read_text(encoding="utf-8") == "b = 2\n"
    assert B in _git(halted, "status", "--porcelain", "--", B)


def test_fence_reply_keys():
    assert land_run._fence_reply({}) == {}
    assert land_run._fence_reply({"recorded": False}) == {"footprint": "unrecorded"}
    assert land_run._fence_reply({"recorded": True, "breach": []}) == {}
    assert land_run._fence_reply({"recorded": True, "breach": ["x"]}) == {"footprint_breach": ["x"]}
