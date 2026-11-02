"""Process-time and spawn budget for grind_split.split_row (500ms process time, zero subprocess spawns)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble.grind_split import MAX_SPLIT_ITEMS, split_row

BUDGET_MS = 500.0
PARENT_ID = "2026-11-01-many-failures-abc123def456"


@pytest.fixture(autouse=True)
def _no_output_override(monkeypatch):
    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)


@pytest.fixture
def popen_calls(monkeypatch):
    calls: list[tuple] = []
    real = subprocess.Popen

    class _Counting(real):
        def __init__(self, *args, **kwargs):
            calls.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _Counting)
    return calls


def _seed_parent(root: Path) -> None:
    d = root / "state" / "bug-backlog"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{PARENT_ID}.yaml").write_text(
        "created: 2026-11-01\n"
        "title: many unrelated failures\n"
        "body: |-\n  lists several\n"
        "status: open\n"
        "surface: a/test_x.py\n"
        "severity: P2\n",
        encoding="utf-8",
    )


def _items(n: int):
    return [
        {"title": f"item {i}", "evidence": f"evidence {i}", "declared_files": [f"pkg/f{i}.py"]}
        for i in range(n)
    ]


@pytest.mark.parametrize("n", [2, MAX_SPLIT_ITEMS])
def test_split_row_within_process_time_budget_and_spawns_nothing(tmp_path, popen_calls, n):
    _seed_parent(tmp_path)
    start = time.process_time()
    ids = split_row(tmp_path, "bug", PARENT_ID, _items(n))
    elapsed_ms = (time.process_time() - start) * 1000.0
    print(f"split_row process_time {n} items: {elapsed_ms:.1f} ms (budget {BUDGET_MS:.0f} ms)")
    assert len(ids) == n, "split_row did not mint every item; the timing is not admissible"
    assert elapsed_ms < BUDGET_MS
    assert popen_calls == []
