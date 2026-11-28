"""dispatch.terminal_commit records a landed ask run's closure: the tracked outcome.json and the reply
read ``partial`` (rows named) while the cited sizing's register still owes a row, and ``finished``
only when none is owed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import run_artifacts, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_SIZING = "state/sizings/2026-10-09-x.yaml"
_PLAN = "docs/plans/p.md"
_RUN = "run1"


@pytest.fixture(autouse=True)
def _onboarded_repo(monkeypatch):
    """The closure and ship legs run only in a repo hosting coordinator state."""
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    monkeypatch.setattr(process_artifact_policy, "hosts_coordinator_state", lambda root: True)


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _row(rid, status="met", **extra):
    return {"id": rid, "source_text": f"ship {rid}", "surface": "ui", "status": status, "claimed_by": [], **extra}


def _met(rid):
    return _row(rid, wired=True, met_by=["x@1"])


def _sizing_text(rows):
    head = (
        "schema: sizing-object\nintent: x\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: e\n"
    )
    sources = [{"doc": "brief.md", "ref": "r@abc"}]
    return head + yaml.safe_dump({"requirement_register": {"sources": sources, "rows": rows}}, sort_keys=False)


def _land(tmp_path, rows, inline_review=_REVIEWED) -> tuple:
    root = tmp_path / "repo"
    for d in ("state/sizings", "docs/plans"):
        (root / d).mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / _SIZING).write_text(_sizing_text(rows), encoding="utf-8")
    (root / _PLAN).write_text(f"---\ntitle: p\nsizing_object: {_SIZING}\n---\nbody\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    run_dir = root / RUN_DIR_ROOT / _RUN
    run_dir.mkdir(parents=True)
    (root / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=_PLAN)
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{_RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{_RUN}/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (root / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{_RUN}/manifest.json\n", encoding="utf-8")
    out = terminal_commit._handler(
        {"inline_review": inline_review, "script_path": "run.mjs", "incomplete_chunks": []},
        repo_root=root / ".git",
    )
    return root, out


def test_a_landing_with_an_open_row_records_and_reports_partial(tmp_path):
    root, out = _land(tmp_path, [_met("R1"), _row("R2", "open")])

    assert out["committed"] is True, out
    assert out["closure"]["verdict"] == "partial"
    assert [r["id"] for r in out["closure"]["open_rows"]] == ["R2"]
    assert out["closed_as"] == "partial"
    assert out["close"]["outcome"] == "partial" and "R2" in out["close"]["open_parts"], out["close"]
    assert "sizing_shipped" not in out and "sizing_ship_skipped" in out
    outcome = run_artifacts.read_tracked_outcome(root, _RUN)
    assert outcome["state"] == "landed"
    assert outcome["closure"]["verdict"] == "partial"
    assert [r["id"] for r in outcome["closure"]["open_rows"]] == ["R2"]
    outcome_rel = run_artifacts.outcome_rel(_RUN)
    assert _git(["status", "--porcelain", "--", outcome_rel], root) == ""
    assert json.loads(_git(["show", f"HEAD:{outcome_rel}"], root))["closure"]["verdict"] == "partial"


def test_a_landing_with_a_partial_row_is_partial_too(tmp_path):
    root, out = _land(tmp_path, [_met("R1"), _row("R2", "partial")])

    assert out["closure"]["verdict"] == "partial" and out["closed_as"] == "partial"
    assert run_artifacts.read_tracked_outcome(root, _RUN)["closure"]["open_rows"][0]["status"] == "partial"


def test_a_landing_with_every_row_met_records_finished_and_reports_no_partial(tmp_path):
    root, out = _land(tmp_path, [_met("R1"), _met("R2")])

    assert out["committed"] is True, out
    assert out["closure"]["verdict"] == "finished"
    assert "closed_as" not in out
    assert out["close"].get("closure") is None
    assert run_artifacts.read_tracked_outcome(root, _RUN)["closure"] == {
        "verdict": "finished", "open_rows": [], "not_met_items": [], "ledger_met_open_rows": [],
        "register": {"R1": "met", "R2": "met"}, "ledger": {},
    }
    assert out["sizing_shipped"]["sizing"] == _SIZING


_EV = {"command": "python -m consumer --run", "observed": "consumer reached the output"}


def test_a_landing_reconciles_an_unclaimed_open_row_the_judge_met_and_ships(tmp_path):
    judged = {
        **_REVIEWED,
        "criterion": {
            "status": "met", "observation": "a.py exists at HEAD", "sidecar": "j.md",
            "register_rows": [{"id": "R2", "status": "met", "wired": True, **_EV}],
        },
    }
    root, out = _land(tmp_path, [_met("R1"), _row("R2", "open")], inline_review=judged)

    assert out["committed"] is True, out
    register = yaml.safe_load((root / _SIZING).read_text(encoding="utf-8"))["requirement_register"]
    row = {r["id"]: r for r in register["rows"]}["R2"]
    assert row["status"] == "met" and row["wired"] is True and row["met_by"], row
    assert out["closure"]["verdict"] == "finished", out["closure"]


def test_a_landing_leaves_an_open_row_the_judge_did_not_meet_open(tmp_path):
    judged = {
        **_REVIEWED,
        "criterion": {
            "status": "met", "observation": "a.py exists at HEAD", "sidecar": "j.md",
            "register_rows": [{"id": "R2", "status": "not_met"}],
        },
    }
    root, out = _land(tmp_path, [_met("R1"), _row("R2", "open")], inline_review=judged)

    register = yaml.safe_load((root / _SIZING).read_text(encoding="utf-8"))["requirement_register"]
    assert {r["id"]: r["status"] for r in register["rows"]}["R2"] == "open"
    assert out["closure"]["verdict"] == "partial" and out["closed_as"] == "partial"
