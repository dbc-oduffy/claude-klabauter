"""A ledger-check halt lands review-passed chunk product when the targeted tests pass, and only then."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import run_artifacts

from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _landed,
    _marker_script,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _dirty_done_chunk(repo):
    rec = _write_record(repo, RUN)
    # The run staged against an absent src.py/test_src.py, so every byte in them is the run's own.
    (rec / "manifest.json").write_text(
        json.dumps({"rows": [], "write_baseline": {"src.py": None, "test_src.py": None}}), encoding="utf-8"
    )
    (repo / "src.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "test_src.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["src.py", "test_src.py"])], {"C1": "DONE: C1\n"})


def test_ledger_check_halt_with_passing_tests_lands_product_and_test(repo):
    _dirty_done_chunk(repo)

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert "src.py" not in out.get("withheld", {}) and "test_src.py" not in out.get("withheld", {})
    names = _landed(repo, out, "--name-only")
    assert "src.py" in names and "test_src.py" in names
    assert run_artifacts.outcome_rel(RUN) in names


def test_ledger_check_halt_without_a_passing_verdict_withholds_product(repo):
    _dirty_done_chunk(repo)

    out = _call(repo, LEDGER)

    assert out["committed"] is True, out
    assert out["withheld"]["src.py"] == "halt-record-only"
    assert out["withheld"]["test_src.py"] == "halt-record-only"
    names = _landed(repo, out, "--name-only")
    assert "src.py" not in names and "test_src.py" not in names


def test_ledger_check_halt_with_failing_verdict_withholds_product(repo):
    _dirty_done_chunk(repo)

    out = _call(repo, LEDGER, targeted_tests={"status": "fail", "tests_failed": 1, "baseline": None})

    assert out["withheld"]["src.py"] == "halt-record-only"
    assert "src.py" not in _landed(repo, out, "--name-only")


def test_refusal_halt_withholds_product_even_with_passing_verdict(repo):
    _dirty_done_chunk(repo)

    out = _call(repo, None, **PASSING)

    assert out["withheld"]["src.py"] == "halt-record-only"
    assert "src.py" not in _landed(repo, out, "--name-only")
