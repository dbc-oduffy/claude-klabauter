"""dispatch.land_run lands a DONE chunk whose plan ADDS an absolute machine path and reports the gate's
diagnostics as `abs_path_advisory` (false-refusal probe: no deny-registry entry covers it); the clean variant
lands with no advisory.

Path literals are built from parts so this file carries none.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CHUNK_PLAN = "docs/plans/q.md"
_DRIVE = "C" + ":" + "\\" + "Users" + "\\" + "jdoe_dev" + "\\" + "repo"


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A built run whose one DONE chunk writes `a.py` and the plan `docs/plans/q.md`; returns `(repo, land)`."""
    repo, script = driver.build_fixture(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", lambda root, key, batons, memo: True)
    (repo / _CHUNK_PLAN).parent.mkdir(parents=True, exist_ok=True)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py", _CHUNK_PLAN)),), plan_path=driver.PLAN_REL
    )
    (repo / script).write_text("// emitted script\n" + render_marker(request) + "\n", encoding="utf-8")
    inline_review = copy.deepcopy(driver.INLINE_REVIEW)
    sidecar = tmp_path / "review-sidecar.md"
    sidecar.write_text("verdict: PASS\n", encoding="utf-8")
    inline_review["wave_reviews"] = [{"slice_id": "s1", "verdict": "PASS", "sidecar_path": str(sidecar)}]

    def land() -> dict:
        return land_run._handler(
            {
                "script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID,
                "inline_review": inline_review,
            },
            repo_root=repo / ".git",
        )

    return repo, land


def test_a_chunk_plan_adding_an_absolute_path_lands_with_an_advisory(run):
    repo, land = run
    (repo / _CHUNK_PLAN).write_text("# q\nsee " + _DRIVE + "\n", encoding="utf-8")
    before = _head(repo)

    out = land()

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert _head(repo) != before
    text = " ".join(out["abs_path_advisory"])
    assert f"{_CHUNK_PLAN}:2" in text, out
    assert "abs-path-ok" in text


def test_the_clean_chunk_plan_lands(run) -> None:
    repo, land = run
    (repo / _CHUNK_PLAN).write_text("# q\nclean prose\n", encoding="utf-8")
    before = _head(repo)

    out = land()

    assert out["committed"] is True, out
    assert "abs_path_advisory" not in out, out
    assert _head(repo) != before
