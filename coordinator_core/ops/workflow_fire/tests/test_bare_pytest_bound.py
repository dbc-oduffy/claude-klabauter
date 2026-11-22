"""A bare `pytest` command word resolves from PATH to an arbitrary interpreter (witness: `no module example_retrieval_repo_core`);
delivery binds it to the stamped interpreter. The wrapper bootstrap carries COORDINATOR_ENGINE_ROOT."""
from __future__ import annotations

import sys

import pytest

from coordinator_core.ops.workflow_fire import worker_preamble
from coordinator_core.ops.workflow_fire.worker_preamble import bind_engine_commands, needs_binding


@pytest.fixture
def stamped(monkeypatch):
    monkeypatch.setattr(worker_preamble, "_engine_command_runtime", lambda: ("/stamped/py", worker_preamble.Path("/eng")))
    monkeypatch.setattr(worker_preamble, "_quoted", lambda v: v)


@pytest.mark.parametrize("text", [
    "pytest -q tests/test_x.py",
    "Run `pytest tests/test_x.py -x` once.",
    "cd repo && pytest coordinator_core/ops/tests/test_y.py",
])
def test_bare_pytest_is_bound_to_the_interpreter(stamped, text):
    assert needs_binding(text)
    out = bind_engine_commands(text)
    assert "/stamped/py -m pytest" in out
    assert " pytest -" not in out.replace("-m pytest", "") and "&& pytest" not in out and "`pytest" not in out


def test_pytest_prose_stays_unbound(stamped):
    prose = "The pytest suite is slow; see the pytest docs."
    assert bind_engine_commands(prose) == prose


def test_python_m_pytest_is_not_double_bound(stamped):
    out = bind_engine_commands("/stamped/py -m pytest tests/test_x.py")
    assert out.count("-m pytest") == 1


def test_wrapper_bootstrap_sets_engine_root():
    assert "os.environ['COORDINATOR_ENGINE_ROOT']=root" in worker_preamble._BOOTSTRAP
