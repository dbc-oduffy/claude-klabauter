"""A halt commit never carries the run's `overlay-inflight` process marker: a detached overlay child releases
it between the commit's listing and its read, and a tracked marker is never staged as a deletion."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git.commit import CommitRefused, commit_paths
from coordinator_core.ops.dispatch_emit import land_run_halt, run_artifacts
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.clean_overlay_verify import INFLIGHT_FILE
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-cccccc"
SIZING = f"{run_artifacts._SIZINGS_REL}/2026-11-18-s.yaml"
RECORD = f"{RECORD_DIR_ROOT}/{RUN}"
MARKER = f"{RECORD}/{INFLIGHT_FILE}"
LEDGER = {"halted": "ledger-check", "reason": "row C2 open"}
PASSING = {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "README.md", "seed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _write(tmp_path, SIZING, "plan: none\n")
    _write(tmp_path, f"{RECORD}/ask.md", "ask\n")
    _write(tmp_path, f"{RECORD}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(tmp_path, "reports/C1.md", "DONE: C1\n")
    _write(tmp_path, "coordinator_core/x/fallback.py", "x = 1\n")
    chunk = ChunkCommit(id="C1", title="C1", paths=("coordinator_core/x/fallback.py",), report="reports/C1.md")
    _write(tmp_path, "run.mjs", render_marker(CommitRequest(chunks=(chunk,), session_id=None, repo_root=None, plan_path=None)) + "\n")
    return tmp_path


def _release_after_listing(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """The overlay child releasing its marker: the first scan that lists the run dir's files deletes it."""
    listed = land_run_halt._differs

    def differs(*args, **kwargs):
        found = listed(*args, **kwargs)
        (root / MARKER).unlink(missing_ok=True)
        return found

    monkeypatch.setattr(land_run_halt, "_differs", differs)


def _land(root: Path) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: LEDGER, **PASSING}
    return land_run_halt.land(root, root, params)


def test_a_marker_released_after_listing_does_not_fail_the_halt_commit(repo, monkeypatch):
    _write(repo, MARKER, "1234")
    _release_after_listing(monkeypatch, repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert MARKER not in out["run_artifacts"]["landed"]
    assert f"{RECORD}/run.json" in out["run_artifacts"]["landed"]
    assert INFLIGHT_FILE not in _git(repo, "ls-tree", "-r", "--name-only", out["sha"])


def test_a_present_marker_is_left_out_of_the_halt_commit(repo):
    _write(repo, MARKER, "1234")

    out = _land(repo)

    assert out["committed"] is True, out
    assert INFLIGHT_FILE not in _git(repo, "ls-tree", "-r", "--name-only", out["sha"])
    assert (repo / MARKER).is_file()


def test_a_tracked_marker_is_never_staged_as_a_deletion(repo):
    _write(repo, MARKER, "1234")
    _git(repo, "add", "-f", MARKER)
    _git(repo, "commit", "-q", "-m", "marker tracked at HEAD")
    (repo / MARKER).unlink()

    out = _land(repo)

    assert out["committed"] is True, out
    assert MARKER not in out["run_artifacts"]["landed"]
    assert "Removes:" not in _git(repo, "log", "-1", "--format=%B")
    assert MARKER in _git(repo, "ls-tree", "-r", "--name-only", out["sha"]).split()


def test_the_record_commit_split_keeps_an_unignored_marker_out_of_the_landable_paths():
    from coordinator_core.ops.dispatch_emit import halted_record_commit

    landable, state = halted_record_commit._split_run_state([f"{RECORD}/run.json", MARKER], RECORD, False)

    assert landable == [f"{RECORD}/run.json"]
    assert state == [MARKER]


def test_commit_paths_still_refuses_a_listed_path_that_vanished(repo):
    with pytest.raises(CommitRefused, match="cannot read"):
        commit_paths(repo, [f"{RECORD}/never-written.json"], "m")
