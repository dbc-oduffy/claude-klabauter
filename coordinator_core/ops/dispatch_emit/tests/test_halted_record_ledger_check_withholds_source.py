"""A ledger-check halt with a dirty source file in the run's write set lands artifacts only."""

from __future__ import annotations

import pytest

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


def test_ledger_check_halt_withholds_dirty_source_and_test(repo):
    _write_record(repo, RUN)
    (repo / "src.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "test_src.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["src.py", "test_src.py"])], {"C1": "DONE: C1\n"})

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["withheld"]["src.py"] == "halt-record-only"
    assert out["withheld"]["test_src.py"] == "halt-record-only"
    names = _landed(repo, out, "--name-only")
    assert "src.py" not in names and "test_src.py" not in names
    assert any(n.startswith(f"state/warp/{RUN}/") for n in names)
