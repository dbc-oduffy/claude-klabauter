"""A ledger-check halt withholds the chunks a carried run-caused test failure implicates."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    RUN,
    _call,
    _git,
    _landed,
    _marker_script,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DONE = {"C1": "DONE: C1\n", "C2": "DONE: C2\n"}
TESTS = "coordinator_core/ops/fleet/tests/test_memo_draft.py"


def _setup(root) -> None:
    _write_record(root, RUN)
    (root / "coordinator_core/ops/fleet/tests").mkdir(parents=True)
    (root / TESTS).write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (root / "c1.py").write_text("a = 1\n", encoding="utf-8")
    (root / "good.py").write_text("g = 1\n", encoding="utf-8")
    _marker_script(root, [("C1", ["c1.py", TESTS]), ("C2", ["good.py"])], DONE)


def _verdict(status="fail", baseline=None, **extra):
    return {"targeted_tests": {"status": status, "tests_failed": 10, "baseline": baseline}, **extra}


def test_caused_failure_in_chunk_test_file_withholds_that_chunk_only(repo):
    _setup(repo)
    caused = [f"{TESTS}::test_a", f"{TESTS}::test_b"]

    out = _call(repo, LEDGER, **_verdict(baseline={"caused": caused, "pre_existing": [], "unverified": []}))

    assert out["committed"] is True, out
    assert out["withheld"] == {"c1.py": "targeted-tests-failed", TESTS: "targeted-tests-failed"}
    assert out["landed_chunks"] == ["C2"]
    assert out["targeted_tests"]["check"] == "failed"
    assert out["targeted_tests"]["findings"][0]["path"] == TESTS
    names = _landed(repo, out, "--name-only")
    assert "good.py" in names and "c1.py" not in names and TESTS not in names


def test_unattributed_failure_makes_product_record_only(repo):
    _setup(repo)

    out = _call(repo, LEDGER, **_verdict(baseline=None))

    assert out["committed"] is True, out
    assert set(out["withheld"]) == {"c1.py", TESTS, "good.py"}
    assert set(out["withheld"].values()) == {"targeted-tests-failed"}
    assert "landed_chunks" not in out
    names = _landed(repo, out, "--name-only")
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names
    assert "good.py" not in names and "c1.py" not in names


def test_failure_in_unowned_file_withholds_every_chunk(repo):
    _setup(repo)

    out = _call(
        repo, LEDGER,
        **_verdict(baseline={"caused": ["elsewhere/test_x.py::test_y"], "pre_existing": [], "unverified": []}),
    )

    assert set(out["withheld"]) == {"c1.py", TESTS, "good.py"}
    assert "landed_chunks" not in out


def test_pre_existing_only_lands_chunks(repo):
    _setup(repo)

    out = _call(repo, LEDGER, **_verdict(baseline={"caused": [], "pre_existing": [f"{TESTS}::test_a"], "unverified": []}))

    assert out["landed_chunks"] == ["C1", "C2"]
    assert "withheld" not in out
    assert out["targeted_tests"] == {"check": "pre-existing-only", "findings": []}


def test_no_verdict_withholds_chunks_as_record_only_and_says_so(repo):
    _setup(repo)

    out = _call(repo, LEDGER)

    assert out["committed"] is True, out
    assert set(out["withheld"]) == {"c1.py", TESTS, "good.py"}
    assert set(out["withheld"].values()) == {"halt-record-only"}
    assert "landed_chunks" not in out
    assert out["targeted_tests"] == {"check": "no-verdict", "findings": []}
    names = _landed(repo, out, "--name-only")
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names
    assert "good.py" not in names and "c1.py" not in names


def test_non_ledger_check_halt_is_unchanged(repo):
    _setup(repo)

    out = _call(repo, {"halted": "refusal", "reason": "r"}, **_verdict(baseline=None))

    assert set(out["withheld"].values()) == {"halt-record-only"}
    assert "targeted_tests" not in out
    assert "Lands-Chunks" not in _git(["log", "-1", "--format=%B"], repo)
