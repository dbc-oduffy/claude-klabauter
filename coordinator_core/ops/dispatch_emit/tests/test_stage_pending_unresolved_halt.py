"""A fire whose script returns an unanswered `stage_pending` halts with a named cause, never a bare exit 1.

Live case: coordinator-content-repo run dd3f47, fire 7b9212c8. The re-armed run still carried its adopted `orphaned` halt, which
`halt_failed_fire` kept as is, so the exit-1 fire wrote no halt and committed nothing.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, record_dir
from coordinator_core.ops.workflow_fire import headless
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-11-20-flat-plan-new-leaf-lint-e87fcb-20261120T112408-dd3f47"
SIZING = "state/sizings/2026-11/2026-11-20-flat-plan-new-leaf-lint-e87fcb.yaml"
SHA = "6015b37e0c0edd3b4aa8f6f05293388806d682ca9f9424d5ea9e8cbff1c5d6a8"
TWIN = f"scratch/warp/{RUN}.staged-{SHA}.workflow.mjs"
PENDING = {"stage_pending": {"handle": {"path": f"state/warp/{RUN}/manifest.json", "sha256": SHA, "bytes": 2855},
                             "twin_path": TWIN}}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
                          **no_console_creationflags()).stdout


@pytest.fixture(autouse=True)
def _no_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(halted_run, "_schedule_resume_build", lambda *_a, **_k: None)
    monkeypatch.setattr(halted_run, "find_split_trees", lambda *_a, **_k: [])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "seed")
    rd = record_dir(root, RUN)
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (rd / "manifest.json").write_text(json.dumps({"head_sha": "a" * 40}), encoding="utf-8")
    halted_run.write_halt_marker(rd, halted="orphaned", sizing_rel=SIZING, reason="orphaned: session is gone",
                                 run_base_sha="a" * 40)
    _git(root, "add", RECORD_DIR_ROOT)
    _git(root, "commit", "-q", "-m", "adopted orphaned halt")
    return root


def test_unanswered_stage_pending_writes_a_named_halt_and_commits_it(repo: Path) -> None:
    record = {"repo_root": str(repo), "script_path": str(repo / "scratch" / "warp" / f"{RUN}.workflow.mjs")}
    before = _git(repo, "rev-parse", "HEAD")
    headless._halt_failed_fire(record, PENDING, "RuntimeError: engine returned an unresolved stage_pending handoff")

    marker = json.loads((record_dir(repo, RUN) / "halted.json").read_text(encoding="utf-8"))
    assert marker["halted"] == "stage_pending_unresolved", marker
    assert SHA[:12] in marker["reason"] and TWIN in marker["reason"]
    assert marker["next_action"]["resume"]["run_id"] == RUN
    assert record["halted_runs"] == [RUN]
    assert record["halt_commits"][RUN]["committed"] is True, record
    assert _git(repo, "rev-parse", "HEAD") != before
    assert "stage_pending_unresolved" in _git(repo, "log", "-1", "--format=%B")


def test_an_ordinary_failure_still_keeps_a_prior_halt(repo: Path) -> None:
    record = {"repo_root": str(repo), "script_path": str(repo / "scratch" / "warp" / f"{RUN}.workflow.mjs")}
    headless._halt_failed_fire(record, {"error": "boom"}, "RuntimeError: boom")
    marker = json.loads((record_dir(repo, RUN) / "halted.json").read_text(encoding="utf-8"))
    assert marker["halted"] == "orphaned"
    assert record["halted_runs"] == []
