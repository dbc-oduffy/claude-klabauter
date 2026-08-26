"""Guard: `coordinator_core.git.tree_spine` and `coordinator_core.git.
argv_batch` never import `coordinator_core.ops` -- the `git/` -> `ops/`
edge C1 (docs/plans/2026-08-26-the-archival-commit-helper-computes-its-own-
tree.md) relocated these two modules out of `coordinator_core.ops.ceremony.
git_native` specifically to retire.

SCOPED TO THESE TWO MODULES ONLY, deliberately not repo-wide: a package-wide
"`coordinator_core/git/` never imports `coordinator_core/ops/`" guard is RED
the moment it is written -- `coordinator_core/git/commit_trailers.py` (lines
258 and 798, as of this chunk) imports from `ops/` today and that pre-
existing edge is explicitly out of this chunk's scope. Do not widen this
guard to cover `commit_trailers.py` or any other existing `git/` module
without a chunk that actually retires those edges too.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUARDED_MODULES = (
    "coordinator_core/git/tree_spine.py",
    "coordinator_core/git/argv_batch.py",
)


def _imported_top_level_packages(source: str) -> set:
    tree = ast.parse(source)
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
    return modules


@pytest.mark.parametrize("relpath", _GUARDED_MODULES)
def test_module_never_imports_ops(relpath):
    source = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
    imported = _imported_top_level_packages(source)
    offending = {m for m in imported if m == "coordinator_core.ops" or m.startswith("coordinator_core.ops.")}
    assert not offending, (
        f"{relpath} imports from coordinator_core.ops ({offending}) -- this "
        "is exactly the git/ -> ops/ edge C1 exists to retire for these two "
        "modules; see this test's own module docstring for why the guard "
        "does not extend to the rest of coordinator_core/git/."
    )
