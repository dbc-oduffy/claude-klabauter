"""landing.guard_selection returns the land gate's owed node set for a path list."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import landing_guard_selection as op
from coordinator_core.ops.dispatch_emit import landing_integrity as li

GUARD_FILE = "tests/test_guard.py"
GUARD = f"{GUARD_FILE}::test_no_literal"
MISSING = "tests/test_gone.py::test_x"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / GUARD_FILE).write_text("def test_no_literal():\n    pass\n", encoding="utf-8")
    (root / "coordinator.local.md").write_text(
        f'---\nalways_run_guards: ["{GUARD}", "{MISSING}"]\n---\n', encoding="utf-8",
    )
    return root


def test_unrelated_paths_still_select_every_declared_guard(repo):
    out = op._handler({"repo_root": str(repo), "paths": ["unrelated.txt"]})
    assert out["guards"] == [GUARD]
    assert GUARD in out["nodes"]
    assert out["nodes"] == li.landing_invariant_nodes(repo, ["unrelated.txt"], None)
    assert f"--tests {GUARD}" in out["retest_command"]


def test_no_declared_guards_returns_empty_guards(tmp_path):
    out = op._handler({"repo_root": str(tmp_path), "paths": ["a.py"]})
    assert out["guards"] == []
    assert GUARD not in out["nodes"]


def test_repo_root_falls_back_to_the_dispatched_root(repo):
    out = op._handler({"paths": ["unrelated.txt"]}, repo)
    assert out["guards"] == [GUARD]


@pytest.mark.parametrize("params", [{}, {"paths": "a.py"}, {"paths": [1]}])
def test_bad_paths_refused(repo, params):
    with pytest.raises(CallerFacingValidationError):
        op._handler({"repo_root": str(repo), **params})


def test_no_root_refused():
    with pytest.raises(CallerFacingValidationError):
        op._handler({"paths": []})


def test_scope_is_worktree_keyed():
    assert OP_KEY_SCOPE[op.OP_KEY] == "show_top"
