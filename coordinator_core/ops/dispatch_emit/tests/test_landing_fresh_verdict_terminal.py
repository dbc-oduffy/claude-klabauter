"""terminal_commit and land_run read the test verdict through landing_test_gate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, landing_integrity as li, terminal_commit
from coordinator_core.ops.dispatch_emit import landing_targeted_tests
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run-1"
RECORDED_FAIL = {"status": "fail", "tests_failed": 1, "baseline": None}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "t.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "t.py")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _fresh(root: Path, status: str, *, head: str | None = None, tests=("t.py",)) -> None:
    path = landing_retest.fresh_verdict_path(root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": RUN, "head": head or _git(root, "rev-parse", "HEAD"), "tests": list(tests),
        "covered": landing_retest.landing_fingerprint(root, ["t.py"]), "status": status,
    }), encoding="utf-8")


def _terminal(root):
    return terminal_commit._landing_integrity_gate(
        root, [], [], None, {"targeted_tests": RECORDED_FAIL}, RUN
    )


def _land(root):
    return land_run._integrity(root, {"targeted_tests": RECORDED_FAIL}, [], [], None, RUN, None)


def test_fresh_pass_clears_recorded_no_baseline_fail(repo):
    _fresh(repo, "pass")
    out = _terminal(repo)
    assert "refused" not in out
    assert out["targeted_tests_check"] == "pass-on-retest"
    assert out["retested_tests"] == ["t.py"]
    assert _land(repo) == []


def test_stale_fresh_verdict_still_refuses_and_names_retest(repo):
    _fresh(repo, "pass", head="0" * 40)
    out = _terminal(repo)
    assert out["refused"] == landing_targeted_tests.TARGETED_TESTS_FAILED
    assert "--retest-targeted" in out["integrity"][0]["detail"]
    assert "retested_tests" not in out
    assert [f.reason for f in _land(repo)] == [landing_targeted_tests.TARGETED_TESTS_FAILED]


def test_changed_covered_file_is_stale(repo):
    _fresh(repo, "pass")
    (repo / "t.py").write_text("x = 2\n", encoding="utf-8")
    assert _terminal(repo)["refused"] == landing_targeted_tests.TARGETED_TESTS_FAILED


def test_fresh_fail_refuses(repo):
    _fresh(repo, "fail")
    out = _terminal(repo)
    assert out["refused"] == landing_targeted_tests.TARGETED_TESTS_FAILED
    assert out["targeted_tests_check"] == "failed"
    assert [f.reason for f in _land(repo)] == [landing_targeted_tests.TARGETED_TESTS_FAILED]
