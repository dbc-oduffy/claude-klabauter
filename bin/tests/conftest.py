"""
bin/tests conftest — pytest configuration for bin/ script tests.

Ensures the project root is importable when pytest is invoked from bin/tests/
or any subdirectory, matching the pattern used in coordinator_core/tests/conftest.py.

Spec backlink: pln-claude-klabauter-cockpit-contract-re-ven-9620fc § D4
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Project root = three levels up from this file:
#   bin/tests/conftest.py → bin/tests/ → bin/ → <root>
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture(autouse=True)
def _reset_foreign_repo_probe_memo():
    """Give every test an empty ``git_scope`` foreign-repo probe memo.

    Twin of the fixture in ``coordinator_core/conftest.py`` — a conftest covers
    only its own subtree, and the doctor-probe tests here build fixture DoE
    clones exactly the way that suite does. See that fixture for why the memo
    must not cross a test boundary.
    """
    from coordinator_core.git_scope import reset_foreign_repo_probe_memo

    reset_foreign_repo_probe_memo()
    yield
    reset_foreign_repo_probe_memo()
