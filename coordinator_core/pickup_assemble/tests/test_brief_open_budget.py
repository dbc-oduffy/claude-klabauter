"""
coordinator_core.pickup_assemble.tests.test_brief_open_budget

Purpose: pins AC4 (docs/plans/2026-08-21-rebuild-the-three-ceremony-
assemblers.md) — no op in scope may read a whole corpus to answer a
question about one artifact. Open count per `brief()` call must stay
UNDER 250, versus the 9,818 measured before C2/C3 deleted
`compute_competing_claim`/`compute_commit_reality`/`compute_successor_handoffs`
(the three unread, corpus-walking computations).

This is the regression guard for the whole class, not just chunk C3 — a
future re-introduction of a per-artifact corpus scan anywhere in
`pickup_assemble` trips this budget, not just the three deleted call
sites.

Counts via a `builtins.open` wrapper (the interface named in C3's own
dispatch brief), installed for the duration of one `brief()` call only.
"""

from __future__ import annotations

import builtins
import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa

# Real-git spawn in the fixture (git init/add/commit) — `brief()`'s
# classifiers read actual git-tracked repo state. Declares the spawn per
# the ratchet's Rule 2(b) marker escape (coordinator_core/tests/
# test_no_new_spawning_tests.py) rather than an allowlist entry.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

# AC4: "open count per op is under 250" — ratified flat, unchanged, over
# the dispute that it may be arithmetically tight for a brief that reads
# stamps (docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md
# line 111).
_AC4_OPEN_BUDGET = 250


from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)


def _seed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


class TestBriefOpenBudget:
    def test_brief_stays_under_ac4_open_budget(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        real_open = builtins.open
        open_count = 0

        def _counting_open(*args, **kwargs):
            nonlocal open_count
            open_count += 1
            return real_open(*args, **kwargs)

        builtins.open = _counting_open
        try:
            result = pa.brief("state/handoffs/h1.md", repo_root=repo)
        finally:
            builtins.open = real_open

        assert result.exit_code == pa.EXIT_OK
        assert open_count < _AC4_OPEN_BUDGET, (
            f"brief() opened {open_count} files — AC4 budget is "
            f"under {_AC4_OPEN_BUDGET}"
        )
