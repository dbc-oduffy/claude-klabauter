"""`test_leg_run.main` stamps the leg it writes with when measurement started and the commit it measured."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.git import git_state
from coordinator_core.ops import test_leg_run as tlr

OLD, NEW, MIRROR = "a" * 40, "b" * 40, "c" * 40


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _main(repo: Path, out: Path, *extra: str) -> dict:
    tlr.main(["--repo-root", str(repo), *extra, "--out", str(out), "--", "tests/test_x.py"])
    return json.loads(out.read_text(encoding="utf-8"))


def _advancing_head(monkeypatch):
    """HEAD reads OLD until a leg run starts, then NEW: a commit landed while the leg ran."""
    state = {"ran": False}
    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: NEW if state["ran"] else OLD)
    return state


def test_default_args_stamp_measured_at_and_the_head_read_before_the_run(tmp_path, monkeypatch):
    state = _advancing_head(monkeypatch)
    seen = {}

    def fake_run(*_a, **_k):
        state["ran"] = True
        seen["after"] = datetime.now(timezone.utc)
        return {"status": "pass", "status_after": "pass", "tests_run": 1, "over_budget": False}

    monkeypatch.setattr(tlr, "run", fake_run)
    before = datetime.now(timezone.utc)
    data = _main(_repo(tmp_path), tmp_path / "o.json")
    assert data["head_sha"] == OLD
    measured = datetime.fromisoformat(data["measured_at"])
    assert before <= measured <= seen["after"]


def test_the_mirror_commit_the_leg_measured_wins_over_the_pre_run_head(tmp_path, monkeypatch):
    _advancing_head(monkeypatch)
    monkeypatch.setattr(tlr, "run", lambda *_a, **_k: {
        "status": "pass", "status_after": "pass", "tests_run": 1, "over_budget": False,
        "own_tree": {"at": MIRROR, "overlaid": []}})
    assert _main(_repo(tmp_path), tmp_path / "o.json")["head_sha"] == MIRROR


def test_an_explicit_head_sha_is_stamped_as_given(tmp_path, monkeypatch):
    _advancing_head(monkeypatch)
    monkeypatch.setattr(tlr, "run", lambda *_a, **_k: {
        "status": "pass", "status_after": "pass", "tests_run": 1, "over_budget": False})
    assert _main(_repo(tmp_path), tmp_path / "o.json", "--head-sha", "d" * 40)["head_sha"] == "d" * 40


def test_a_refused_leg_that_measured_nothing_carries_no_stamp(tmp_path):
    out = tmp_path / "o.json"
    tlr.main(["--repo-root", str(tmp_path / "nowhere"), "--out", str(out), "--", "tests/test_x.py"])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "measured_at" not in data and "head_sha" not in data
