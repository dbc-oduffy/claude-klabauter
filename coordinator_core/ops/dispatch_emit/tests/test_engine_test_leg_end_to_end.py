"""End to end at default arguments: the composed ask script runs test_leg_run detached, a timed-out leg records zero
tests, and land_run refuses that record."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import test_leg_run
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose as ask_compose_script
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import (  # noqa: F401 -- fixture
    _author,
    _declare,
    _git,
    _land,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_default_ask_script_verify_tests_prompt_runs_test_leg_run_in_the_background_with_no_deadline():
    script = ask_compose_script()
    at = script.index("label: 'verify-tests'")
    call = script[script.rindex("_verifyTest = await agent(", 0, at) : at]
    assert "coordinator_core.ops.test_leg_run" in call
    assert "run_in_background: true" in call
    assert "Deadline:" not in call and "Bash tool timeout" not in call


def test_default_ask_script_writes_the_leg_record_where_the_landing_gate_reads_it():
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    script = ask_compose_script()
    at = script.index("label: 'verify-tests'")
    call = script[script.rindex("_verifyTest = await agent(", 0, at) : at]
    out = call.split("--out ", 1)[1].split(" ", 1)[0]
    assert f"{RECORD_DIR_ROOT}/" in out and out.endswith("/test-leg.json")


def _slow_leg_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(60)\n", encoding="utf-8")
    monkeypatch.chdir(proj)
    out = tmp_path / "run" / "test-leg.json"
    rc = test_leg_run.main(["--budget-s", "1", "--out", str(out), "--", "test_slow.py"])
    assert rc == test_leg_run.EXIT_TIMEOUT
    return json.loads(out.read_text(encoding="utf-8"))


def test_timed_out_leg_records_zero_tests_and_land_run_refuses_that_record(tmp_path, monkeypatch, repo):
    record = _slow_leg_record(tmp_path, monkeypatch)
    assert record["tests_run"] == 0 and record["zero_run_cause"] == "budget-exhausted"
    assert record["status"] == "error"

    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _land(repo, targeted_tests=record)

    assert out["committed"] is False, out
    assert "ran 0 tests" in json.dumps(out) and "budget-exhausted" in json.dumps(out)
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
