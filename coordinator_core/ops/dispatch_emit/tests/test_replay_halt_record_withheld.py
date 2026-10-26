"""Replay (experiments dbadfe/ee85439, fixture b7a474f): a halt-record commit never lands another run's withheld untracked file."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

LIVE_CELL = "naked-vs-coordinator/live_cell.py"
LANDING_RUN = "ask-20261015T000000-aaaaaa"
HALTED_RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-bbbbbb"
REASON = "ledger check failed"
PASSING = {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}}


@pytest.fixture(autouse=True)
def _hosts_state(monkeypatch):
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    monkeypatch.setattr(process_artifact_policy, "hosts_coordinator_state", lambda root: True)


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _halted_run_withholding_live_cell(root):
    rec = f"state/warp/{HALTED_RUN}"
    _write(root, f"{rec}/halted.json", json.dumps(
        {"status": "halted", "run_artifacts": {"withheld": {LIVE_CELL: "halt-record-only"}}}))
    _write(root, f"{rec}/manifest.json", json.dumps({"rows": [{"id": "C1", "writes": [LIVE_CELL]}], "doc_writes": []}))
    _write(root, LIVE_CELL, "# the halted run's unfinished work\n")


def _landing_run_declaring_live_cell(root):
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    _write(root, f"state/warp/{LANDING_RUN}/run.json", "{}\n")
    _write(root, "reports/C5.md", "DONE: C5\n")
    marker = render_marker(CommitRequest(
        chunks=(ChunkCommit(id="C5", title="C5", paths=(LIVE_CELL,), report="reports/C5.md"),),
        session_id=None, repo_root=None, plan_path=None,
    ))
    _write(root, "run.mjs", marker + "\n")


def test_halt_record_does_not_commit_another_runs_withheld_untracked_file(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM

    root = _repo(tmp_path)
    _halted_run_withholding_live_cell(root)
    _landing_run_declaring_live_cell(root)

    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": LANDING_RUN,
         HALTED_RECORD_PARAM: {"halted": "ledger-check", "reason": REASON}, **PASSING},
        repo_root=root / ".git",
    )

    assert out.get("committed") is True, out
    landed = _git(root, "show", "--name-only", "--format=", out["sha"]).splitlines()
    assert LIVE_CELL not in landed, f"halt-record committed {LIVE_CELL} held by {HALTED_RUN}: {landed}"


def test_landing_keeps_a_declared_path_another_halted_run_withholds_out_of_the_commit(tmp_path):
    from coordinator_core.ops.dispatch_emit import land_run

    root = _repo(tmp_path)
    _halted_run_withholding_live_cell(root)
    _landing_run_declaring_live_cell(root)

    held = land_run.foreign_halted_withheld(root, LANDING_RUN, None, [LIVE_CELL, "seed.txt"])

    assert held == {LIVE_CELL: HALTED_RUN}
    assert land_run.foreign_halted_withheld(root, HALTED_RUN, None, [LIVE_CELL]) == {}


def test_halt_record_still_lands_its_own_record_when_peer_withholds_an_undeclared_path(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM

    root = _repo(tmp_path)
    _halted_run_withholding_live_cell(root)
    _write(root, f"state/warp/{LANDING_RUN}/run.json", "{}\n")
    _write(root, "run.mjs", "// script\n")

    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": LANDING_RUN,
         HALTED_RECORD_PARAM: {"halted": "refusal", "reason": REASON}},
        repo_root=root / ".git",
    )

    assert out.get("committed") is True, out
    landed = _git(root, "show", "--name-only", "--format=", out["sha"]).splitlines()
    assert LIVE_CELL not in landed
    assert _git(root, "status", "--porcelain", "-uall", "--", LIVE_CELL).startswith("??")
