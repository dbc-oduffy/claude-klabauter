"""dispatch.test_leg reads the recorded test verdict into a whole-diff-review-result."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import dispatch_test_leg as leg
from coordinator_core.ops.dispatch_emit.verdict_supersession import record_tests_supersession
from coordinator_core.win_portability import no_console_creationflags

PLAN_ID = "pln-test-leg-fixture-000001"
RESULT_KEYS = {"sidecar_path", "agent_type", "verdict", "findings", "rebuild_recommended", "em_may_think_differently"}


def _git(root: Path, *args: str, date: str = "") -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    if date:
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout.strip()


def _repo(tmp_path: Path, date: str = "") -> "tuple[Path, str]":
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "plan.md").write_text(f"---\nplan_id: {PLAN_ID}\n---\n\n# p\n", encoding="utf-8")
    _git(root, "add", "plan.md")
    _git(root, "commit", "-q", "-m", "base", date=date)
    return root, _git(root, "rev-parse", "HEAD")


def _record(root: Path, status: str, **extra) -> None:
    record_tests_supersession(repo_root=root, plan_id=PLAN_ID, tests={"status": status, "run": 10, **extra})


def _call(root: Path, sha: str) -> dict:
    return leg._test_leg({"plan_path": "plan.md", "run_base_sha": sha, "repo_root": str(root)})


def test_failing_record_requires_changes_with_failure_count(tmp_path):
    root, sha = _repo(tmp_path, date="2020-01-01T00:00:00Z")
    _record(root, "fail", failed=3)
    out = _call(root, sha)
    assert set(out) == RESULT_KEYS
    assert (out["verdict"], out["findings"], out["agent_type"]) == ("REQUIRES_CHANGES", 3, "dispatch.test_leg")
    assert out["rebuild_recommended"] is False and out["em_may_think_differently"] == []
    assert (root / out["sidecar_path"]).is_file()


def test_passing_fresh_record_is_approved(tmp_path):
    root, sha = _repo(tmp_path, date="2020-01-01T00:00:00Z")
    _record(root, "pass", failed=0)
    out = _call(root, sha)
    assert (out["verdict"], out["findings"]) == ("APPROVED", 0)


def test_absent_record_is_approved_with_one_note(tmp_path):
    root, sha = _repo(tmp_path, date="2020-01-01T00:00:00Z")
    out = _call(root, sha)
    assert (out["verdict"], out["findings"]) == ("APPROVED_WITH_NOTES", 1)
    assert "unmeasured" in (root / out["sidecar_path"]).read_text(encoding="utf-8")


def test_record_older_than_the_run_base_is_not_fresh(tmp_path):
    root, sha = _repo(tmp_path, date="2099-01-01T00:00:00Z")
    _record(root, "fail", failed=2)
    out = _call(root, sha)
    assert (out["verdict"], out["findings"]) == ("APPROVED_WITH_NOTES", 1)


def test_errored_record_is_unmeasured_not_a_pass(tmp_path):
    root, sha = _repo(tmp_path, date="2020-01-01T00:00:00Z")
    _record(root, "errored")
    assert _call(root, sha)["verdict"] == "APPROVED_WITH_NOTES"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"plan_path": "", "run_base_sha": "abcd1234"},
        {"plan_path": "plan.md", "run_base_sha": "not-a-sha"},
        {"plan_path": "plan.md"},
        {"plan_path": "missing.md", "run_base_sha": "abcd1234"},
    ],
)
def test_bad_params_raise_a_caller_facing_error(tmp_path, params):
    root, _ = _repo(tmp_path)
    with pytest.raises(CallerFacingValidationError):
        leg._test_leg({**params, "repo_root": str(root)})


def test_unknown_commit_raises_a_caller_facing_error(tmp_path):
    root, _ = _repo(tmp_path)
    with pytest.raises(CallerFacingValidationError):
        _call(root, "deadbeef" * 5)


def test_one_git_spawn_no_pytest_and_under_the_bar(tmp_path, monkeypatch):
    root, sha = _repo(tmp_path, date="2020-01-01T00:00:00Z")
    _record(root, "pass", failed=0)
    spawned: list = []
    real = subprocess.run

    def counting(argv, *a, **kw):
        spawned.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(leg.subprocess, "run", counting)
    started = time.process_time()
    out = _call(root, sha)
    elapsed = time.process_time() - started
    assert out["verdict"] == "APPROVED"
    assert len(spawned) == 1 and spawned[0][0] == "git"
    assert not any("pytest" in part for argv in spawned for part in argv)
    assert elapsed < 0.5
