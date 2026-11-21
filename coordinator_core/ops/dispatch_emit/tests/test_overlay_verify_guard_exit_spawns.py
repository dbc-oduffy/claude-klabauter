"""A repo guard that ends pytest before any test runs is "verify could not run" (environment_defect) carrying
the guard text; a real failing test under the same undeclared interpreter stays red."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _repo
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete, pytest.mark.cadence]

_GUARD = (
    "INTERPRETER/DEPENDENCY GUARD: this is not a test failure.\n"
    "Interpreter in use: /opt/engine/python3.15\nPinned interpreter: /opt/repo/python3.13\n"
)


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _commit(root, rel, src):
    _write(root, rel, src)
    _git(root, "add", rel)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    return _git(root, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("code", [1, 4])
def test_a_guard_exit_under_an_undeclared_pin_is_an_environment_defect_with_the_guard_text(tmp_path, code):
    root, _ = _repo(tmp_path)
    head = _commit(root, "conftest.py", f"import sys\nsys.stderr.write({_GUARD!r})\nraise SystemExit({code})\n")

    out = cov.verify(root, "run1", head, ["t/test_reader.py"])

    assert out.verdict == cov.ENV_DEFECT and not out.red, out
    assert "verify could not run" in out.detail and "Pinned interpreter: /opt/repo/python3.13" in out.detail, out


def test_a_real_failed_test_under_the_same_interpreter_stays_red(tmp_path):
    root, _ = _repo(tmp_path)
    head = _commit(root, "t/test_real.py", "def test_real():\n    assert 1 == 2\n")

    out = cov.verify(root, "run1", head, ["t/test_real.py"])

    assert out.red and out.verdict != cov.ENV_DEFECT, out
