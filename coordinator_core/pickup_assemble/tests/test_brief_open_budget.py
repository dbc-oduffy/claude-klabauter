
from __future__ import annotations

import builtins
import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pa

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

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
