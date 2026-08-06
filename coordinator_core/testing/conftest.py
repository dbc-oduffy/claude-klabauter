"""
coordinator_core.testing.conftest — local-conftest re-export of the shared
fixture factory.

The `fixture_tree` factory lives in the plain module
`coordinator_core.testing._fixtures` (see that module for why). This conftest
re-exports it so tests under `coordinator_core/testing/` pick it up via the
normal local-conftest mechanism, with no `pytest_plugins` boilerplate.
"""

from __future__ import annotations

from coordinator_core.testing._fixtures import (  # noqa: F401 — re-exported for local-conftest fixture discovery
    FixtureTree,
    fixture_tree,
)
