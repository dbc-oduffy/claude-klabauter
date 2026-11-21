"""A repo guard that ends pytest before any test runs is "verify could not run" (environment_defect) carrying
the guard text; a real failing test under the same undeclared interpreter stays red."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401

pytestmark = [pytest.mark.complete]

_GUARD = (
    "INTERPRETER/DEPENDENCY GUARD: this is not a test failure.\n"
    "Interpreter in use: /opt/engine/python3.15\nPinned interpreter: /opt/repo/python3.13\n"
)


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def test_guard_exit_needs_a_nonzero_exit_with_no_test_outcome():
    assert cov.guard_exit(1, _GUARD)
    assert not cov.guard_exit(0, _GUARD)
    assert not cov.guard_exit(1, "FAILED t/a.py::test_x - assert 1 == 2\n1 failed in 0.1s\n")
    assert not cov.guard_exit(5, "no tests ran in 0.01s\n")
    assert not cov.guard_exit(2, "ERROR collecting t/a.py\n!!! Interrupted: 1 error during collection !!!\n")
