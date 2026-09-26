
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture(autouse=True)
def _reset_foreign_repo_probe_memo():
    from coordinator_core.git_scope import reset_foreign_repo_probe_memo

    reset_foreign_repo_probe_memo()
    yield
    reset_foreign_repo_probe_memo()
