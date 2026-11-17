"""A landing whose run retest is still running waits for its verdict through the default ``dispatch.land_run``
and the emitted commit stage's wait command, then lands when green, refuses naming red nodes, and reports an
abandoned retest as ``retest-abandoned`` with a re-run command that starts a fresh retest."""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, landing_retest, retest_targeted
from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import (  # noqa: F401
    RUN,
    SCRIPT,
    _author,
    _declare,
    _git,
    _land,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TEST_FILE = "tests/test_a.py"
NODE = f"{TEST_FILE}::test_one"
_WAIT = re.compile(r"-m coordinator_core\.ops\.dispatch_emit\.retest_targeted (--poll --wait --params-file [^\s\\']+)")


@pytest.fixture
def run(repo, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    _write(repo, TEST_FILE, "def test_one():\n    pass\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "tests"], repo)
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    params_file = params_path(repo, RUN)
    params_file.parent.mkdir(parents=True, exist_ok=True)
    params_file.write_text(json.dumps({"run_id": RUN, "script_path": SCRIPT}), encoding="utf-8")
    return repo


def _record(root: Path, status: str, deadline_in: float | None = 3600, **extra) -> None:
    body = {
        "run_id": RUN, "head": _git(["rev-parse", "HEAD"], root).strip(), "status": status,
        "tests": [TEST_FILE], "covered": landing_retest.landing_fingerprint(root, ["a.py"]), **extra,
    }
    if deadline_in is not None:
        body["deadline"] = (datetime.now(timezone.utc) + timedelta(seconds=deadline_in)).isoformat()
    path = landing_retest.fresh_verdict_path(root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _emitted_wait_argv(root: Path) -> list[str]:
    stage: list = []
    ask_compose._append_docs_and_commit(stage, head="h", anchor="a", repo_root=str(root), run_id=RUN, manifest_rel="m.json")
    text = "\n".join(stage)
    loop = text[text.index("_commit.awaiting"):text.index("_commit.remedy_step")]
    match = _WAIT.search(loop)
    assert match is not None, loop
    return [*shlex.split(match.group(1)), "--repo-root", str(root)]


def _wait_flipping_to(monkeypatch: pytest.MonkeyPatch, root: Path, argv: list[str], flip) -> int:
    """Run the emitted wait command; the retest 'finishes' on its first sleep by rewriting the record."""
    slept: list[float] = []

    def _sleep(secs: float) -> None:
        slept.append(secs)
        flip()

    monkeypatch.setattr(retest_targeted.time, "sleep", _sleep)
    code = retest_targeted.main(argv)
    assert slept, "the wait command returned without waiting on a running record"
    return code


def test_running_then_pass_lands_through_the_emitted_wait(run, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(run, "running")
    awaiting = _land(run)
    assert awaiting["committed"] is False and awaiting["refused"] is None and awaiting["remedy_step"] is None, awaiting
    assert awaiting["awaiting"]["awaiting"] == "retest" and "targeted-tests-failed" not in awaiting["error"]

    code = _wait_flipping_to(
        monkeypatch, run, _emitted_wait_argv(run),
        lambda: _record(run, "pass", tests_run=1, tests_failed=0),
    )
    assert code == 0

    out = _land(run)
    assert out["committed"] is True, out
    assert "a.py" in _git(["show", "--name-only", "--format=", "HEAD"], run).split()


def test_running_then_fail_refuses_naming_the_red_node(run, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(run, "running")
    assert _land(run)["awaiting"]["awaiting"] == "retest"

    code = _wait_flipping_to(
        monkeypatch, run, _emitted_wait_argv(run),
        lambda: _record(
            run, "fail", failed_tests=[NODE], tests_run=1, tests_failed=1,
            baseline={"caused": [NODE], "pre_existing": [], "unverified": []},
        ),
    )
    assert code == 1

    out = _land(run)
    assert out["committed"] is False and out["refused"] == "targeted-tests-failed", out
    assert "awaiting" not in out and NODE in out["error"] + json.dumps(out["integrity"]), out
    assert out["remedy_step"]["nodes"], out


def test_running_past_deadline_refuses_abandoned_and_the_rerun_starts_a_fresh_retest(run, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(run, "running")
    assert _land(run)["awaiting"]["awaiting"] == "retest"

    code = _wait_flipping_to(
        monkeypatch, run, _emitted_wait_argv(run), lambda: _record(run, "running", deadline_in=-5),
    )
    assert code == 2

    task_output = run.parent / "task.output"
    task_output.write_text(
        json.dumps({"next_action": {"params": {"run_id": RUN, "script_path": SCRIPT}}}), encoding="utf-8"
    )
    out = _land(run, task_output_path=str(task_output))
    assert out["committed"] is False and out["refused"] == "retest-abandoned", out
    assert "abandoned" in out["error"] and "awaiting" not in out and "targeted-tests-failed" not in out["error"]
    assert out["remedy_step"]["nodes"] == [TEST_FILE], out

    rerun = re.search(r"re-run: emit-dispatch-workflow --retest-targeted (--from-task-output)", out["error"])
    assert rerun is not None, out["error"]
    spawned: list = []
    argv = [rerun.group(1), str(task_output), "--tests", TEST_FILE, "--repo-root", str(run)]
    assert retest_targeted.main(argv, spawn=lambda *a, **k: spawned.append(a) or True) == 0

    fresh = json.loads(landing_retest.fresh_verdict_path(run, RUN).read_text(encoding="utf-8"))
    assert spawned and fresh["status"] == landing_retest.RETEST_RUNNING and not retest_targeted.retest_abandoned(fresh), fresh
