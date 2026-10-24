"""`terminal_commit._live_run_claims`: a halted, unresumed sibling run's declared writes are its
claims; the committing run's own halted record is never a claim against itself."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)

JOB = "tools/runpod-jobs/job.py"
PROTO_PREFIX = "tools/likeness-prototype"


def _run_id(age_seconds: float = 60, suffix: str = "aaaaaa") -> str:
    stamp = datetime.fromtimestamp(time.time() - age_seconds, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"ask-{stamp}-{suffix}"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _halted_run(root: Path, run_id: str, *, status: str = "halted", writes=(JOB,),
                prefixes=(PROTO_PREFIX,), doc_writes=(), withheld=()) -> None:
    marker = {"status": status}
    if withheld:
        marker["run_artifacts"] = {"withheld": {p: "peer-held" for p in withheld}}
    _write(root, f"state/warp/{run_id}/halted.json", json.dumps(marker))
    _write(root, f"state/warp/{run_id}/manifest.json",
           json.dumps({"rows": [{"id": "C1", "writes": list(writes)}], "doc_writes": list(doc_writes)}))
    if prefixes:
        request = CommitRequest(chunks=(ChunkCommit(id="C1", title="h", paths=(), prefixes=tuple(prefixes)),))
        _write(root, f"scratch/warp/{run_id}/commit-request.txt", render_marker(request) + "\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_the_runs_own_halted_record_is_not_a_hold_against_itself(repo):
    own = "run-r"
    assert terminal_commit._live_run_claims(repo, [JOB], own) == set()
    _halted_run(repo, _run_id(), withheld=(JOB,))
    held: dict = {}
    assert terminal_commit._live_run_claims(repo, [JOB, "src/a.py"], own, held) == {JOB}
    assert list(held) == [JOB]


def test_a_resumed_halted_run_claims_nothing(repo):
    _halted_run(repo, _run_id(), status="resumed")
    assert terminal_commit._live_run_claims(repo, [JOB], "run-r") == set()
