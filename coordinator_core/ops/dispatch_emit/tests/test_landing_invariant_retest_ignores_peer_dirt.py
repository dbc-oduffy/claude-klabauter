"""The in-run invariant retest judges HEAD plus the run's landing paths: a peer run's dirty file that
breaks the invariant does not refuse the landing, and a run whose own path breaks it still fails."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, landing_retest
from coordinator_core.ops.dispatch_emit.tests.test_land_run_invariant_retest_in_run import (  # noqa: F401
    INV,
    RUN,
    _git,
    _land,
    owed,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests import test_retest_isolated_export as iso

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_the_emitted_retest_command_is_isolated_and_its_poll_inherits_the_flag():
    call = ask_compose._invariant_retest_call(head="h", anchor="a", run_id=RUN)

    assert "retest_targeted --isolated-export --from-task-output " in call
    assert "--poll" in call


def test_a_peer_dirty_file_breaking_the_invariant_leaves_the_retest_green(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = iso._repo(tmp_path)  # mod.py is a peer's uncommitted edit that reds the test in the live tree
    out = iso._task_output(tmp_path, root, manifest=True)

    rc = iso._child(root, out, [], dirty=lambda r: [])

    record = iso._record(root)
    assert rc == 0 and record["status"] == "pass", record
    assert record["live"]["status"] == "fail"
    assert "mod.py" not in record["isolated_export"]["write_set"]


def test_the_run_own_path_breaking_the_invariant_still_fails_the_retest(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = iso._repo(tmp_path)
    (root / "test_a.py").write_text("def test_value():\n    assert False\n", encoding="utf-8")
    out = iso._task_output(tmp_path, root, manifest=True)

    rc = iso._child(root, out, [], dirty=lambda r: [])

    record = iso._record(root)
    assert rc == 1 and record["status"] == "fail" and record["export"]["status"] == "fail", record


def test_the_landing_commits_on_an_export_pass_while_a_peer_dirty_file_reds_the_live_tree(owed):
    head = _git(["rev-parse", "HEAD"], owed).strip()
    record = {
        "run_id": RUN, "head": head, "status": "pass", "tests": [INV.split("::")[0]], "tests_run": 1,
        "covered": landing_retest.landing_fingerprint(owed, ["a.py"]),
        "isolated_export": {"write_set": ["a.py"], "files": ["a.py"]},
        "export": {"status": "pass", "failed_tests": []},
        "live": {"status": "fail", "failed_tests": [INV]},
    }
    path = landing_retest.fresh_verdict_path(owed, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")

    out = _land(owed, retest_attempts=1)

    assert out["committed"] is True, out
    assert "a.py" in _git(["show", "--name-only", "--format=", "HEAD"], owed).split()
