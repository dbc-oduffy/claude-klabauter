"""`gates.review_scale` withholds a permissive verdict over an incompletely
attributed range (claude-klabauter#23).

The review-scale measurement sees only `Session-Id`-trailered commits. The
measured specimen had 1 of 68 commits attributed and resolved `resolved: true,
partition_mandatory: false` over a 22.5k-LOC range, in the same envelope whose
`review-brightline-gate` directive refused that verdict for the same reason.
These tests pin the two surfaces to one answer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc

#: Real git is the state under test: which commit objects carry a trailer.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_SID = "66666666-6666-4666-8666-666666666666"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, **_NO_WINDOW
    ).stdout.strip()


def _commit(root: Path, name: str, trailer: str = "") -> str:
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    _git(root, "add", name)
    message = f"add {name}\n\n{trailer}" if trailer else f"add {name}"
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    seed = _commit(root, "seed.txt")
    _git(root, "update-ref", "refs/remotes/origin/main", seed)
    (root / ".git" / "coordinator-sessions" / _SID).mkdir(parents=True)
    monkeypatch.setattr(
        wsc,
        "compute_session_shape_gate",
        lambda _root: wsc.SessionShapeGate(
            sid=_SID,
            disposition="single-session",
            consumed_handoff="",
            diagnostics=[],
            consumed_handoff_paths=(),
            detection={},
        ),
    )
    return root


def _review_scale(root: Path, decisions: dict) -> dict:
    decisions = {"executor_dispatched": True, "shared_schema_touched": False, **decisions}
    return wsc.brief(decisions=decisions, repo_root=root)["gates"]["review_scale"]


def test_untrailered_commit_in_range_withholds_the_permissive_scale(repo):
    _commit(repo, "own.py", f"Session-Id: {_SID}")
    _commit(repo, "dispatched.py")

    scale = _review_scale(repo, {"stage_paths": []})

    assert scale["resolved"] is False
    assert scale["partition_mandatory"] is None
    assert scale["attribution"] == {
        "range": "origin/main..HEAD",
        "attributed_commits": 1,
        "unattributed_commits": 1,
    }
    assert "commit_count_scope" in scale["remediation"]


def test_fully_attributed_range_resolves_and_reports_its_census(repo):
    _commit(repo, "own.py", f"Session-Id: {_SID}")

    scale = _review_scale(repo, {"stage_paths": []})

    assert scale["resolved"] is True
    assert scale["partition_mandatory"] is False
    assert scale["attribution"]["unattributed_commits"] == 0
    assert "remediation" not in scale


def test_hand_measured_override_is_not_second_guessed(repo):
    _commit(repo, "own.py", f"Session-Id: {_SID}")
    _commit(repo, "dispatched.py")

    scale = _review_scale(
        repo,
        {
            "stage_paths": [],
            "code_loc": 2,
            "commit_count": 2,
            "surface_count": 1,
            "commit_count_scope": "hand-measured, both commits",
        },
    )

    assert scale["resolved"] is True
    assert "attribution" not in scale


def test_absent_origin_main_leaves_the_measurement_as_it_was(repo):
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    _commit(repo, "own.py", f"Session-Id: {_SID}")
    _commit(repo, "dispatched.py")

    scale = _review_scale(repo, {"stage_paths": []})

    assert scale["resolved"] is True
    assert "attribution" not in scale
