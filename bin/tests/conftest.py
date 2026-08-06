"""
bin/tests conftest — pytest configuration for bin/ script tests.

Ensures the project root is importable when pytest is invoked from bin/tests/
or any subdirectory, matching the pattern used in coordinator_core/tests/conftest.py.

Spec backlink: docs/plans/2026-07-05-claude-klabauter-cockpit-contract-revendor-script.md § D4
"""

from __future__ import annotations

import sys
from pathlib import Path

# Project root = three levels up from this file:
#   bin/tests/conftest.py → bin/tests/ → bin/ → <root>
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
