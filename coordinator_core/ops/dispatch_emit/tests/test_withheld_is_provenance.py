"""A halted peer's withheld set is provenance for dispatch.terminal_commit: the run lands its own
paths, the peer's stay out and are recorded, and nothing refuses."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PEER_RUN = "ask-20261020T000000-bbbbbb"
OWN = "own.py"
PEER = "peer.py"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_floor

    monkeypatch.setattr(review_floor, "verdict_count", lambda *_a, **_k: 1)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _halted_peer_withholding(root: Path, *paths: str) -> None:
    rdir = root / "state" / "warp" / PEER_RUN
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({}), encoding="utf-8")
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {p: "x" for p in paths}}}),
        encoding="utf-8",
    )


def _fire(root: Path, *declared: str) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(declared)),))
    (root / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []}
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_a_halted_peers_withheld_path_is_left_out_and_the_run_lands_its_own(repo):
    _halted_peer_withholding(repo, PEER)
    (repo / OWN).write_text("x = 1\n", encoding="utf-8")
    (repo / PEER).write_text("y = 2\n", encoding="utf-8")

    out = _fire(repo, OWN)

    assert out["committed"] is True, out
    assert "refused" not in out
    landed = set(_git(["show", "--name-only", "--format=", out["sha"]], repo).split())
    assert OWN in landed and PEER not in landed
    assert _git(["status", "--porcelain", "--", PEER], repo).startswith("??")


def test_a_peer_that_withholds_the_runs_own_path_does_not_refuse_it(repo):
    _halted_peer_withholding(repo, OWN, PEER)
    (repo / OWN).write_text("x = 1\n", encoding="utf-8")
    (repo / PEER).write_text("y = 2\n", encoding="utf-8")

    out = _fire(repo, OWN)

    assert out["committed"] is True, out
    assert "refused" not in out
    landed = set(_git(["show", "--name-only", "--format=", out["sha"]], repo).split())
    assert OWN in landed and PEER not in landed
