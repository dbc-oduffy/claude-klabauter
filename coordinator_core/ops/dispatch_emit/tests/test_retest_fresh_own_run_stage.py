"""fresh_targeted_verdict ignores every covered path under the run's own record dir but no other covered path."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, landing_retest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-own"
OTHER_RUN = "run-other"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("X = 1\n", encoding="utf-8")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "init")
    for rid in (RUN_ID, OTHER_RUN):
        ask_contract.write_run_stage(root, rid, "staged")
    covered = [
        "a.py",
        f"state/warp/{RUN_ID}/run-stage.json",
        f"state/warp/{OTHER_RUN}/run-stage.json",
    ]
    path = landing_retest.fresh_verdict_path(root, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": RUN_ID, "status": "pass", "head": _git(root, "rev-parse", "HEAD"),
                "covered": landing_retest.landing_fingerprint(root, covered),
            }
        ),
        encoding="utf-8",
    )
    return root


def test_own_run_stage_rewrite_stays_fresh(repo):
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is not None
    own = repo / "state" / "warp" / RUN_ID / "run-stage.json"
    before = own.read_bytes()
    ask_contract.write_run_stage(repo, RUN_ID, "landed")
    assert own.read_bytes() != before
    record = landing_retest.fresh_targeted_verdict(repo, RUN_ID)
    assert record is not None and record["status"] == "pass"


def test_committed_own_run_stage_stays_fresh(repo):
    ask_contract.write_run_stage(repo, RUN_ID, "landed")
    _git(repo, "add", f"state/warp/{RUN_ID}/run-stage.json")
    _git(repo, "commit", "-q", "-m", "stamp run stage")
    record = landing_retest.fresh_targeted_verdict(repo, RUN_ID)
    assert record is not None and record["status"] == "pass"


def test_other_covered_file_change_reads_stale(repo):
    (repo / "a.py").write_text("X = 2\n", encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None


def test_other_runs_stage_change_reads_stale(repo):
    ask_contract.write_run_stage(repo, OTHER_RUN, "landed")
    assert landing_retest.fresh_targeted_verdict(repo, RUN_ID) is None
