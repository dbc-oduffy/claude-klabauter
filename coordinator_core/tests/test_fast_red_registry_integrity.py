"""The committed fast-tier standing-red registry has no integrity problems."""
from __future__ import annotations

from datetime import date

from coordinator_core.ops.fast_red_registry import REPO_ROOT, load_registry, registry_problems


def test_committed_registry_is_sound():
    assert registry_problems(load_registry(), today=date.today(), repo_root=REPO_ROOT) == []
