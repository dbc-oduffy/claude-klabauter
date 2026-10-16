"""cap_waves splits over-wide waves; ask_stage applies sibling_split.runtime_cap."""

from __future__ import annotations

import subprocess

from coordinator_core.ops.dispatch_emit import sibling_split
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow, cap_waves


def _row(i: int) -> WaveRow:
    return WaveRow(id=f"R{i}", title="t", surface="s", writes=[f"p{i}.py"], reads=[], depends_on=[])


def test_wave_of_cap_plus_one_splits_in_order():
    wave = [_row(i) for i in range(4)]
    out = cap_waves([wave], 3)
    assert [[r.id for r in w] for w in out] == [["R0", "R1", "R2"], ["R3"]]


def test_waves_at_or_under_cap_unchanged():
    waves = [[_row(0), _row(1)], [_row(2)]]
    assert cap_waves(waves, 2) == waves


def test_cap_waves_spawns_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert len(cap_waves([[_row(0), _row(1), _row(2)]], 1)) == 3


def test_ask_stage_splits_wide_plan(tmp_path, monkeypatch):
    rows = "".join(
        f"- id: C{i}\n  title: t{i}\n  change_kind: code-edit\n  surface: pkg/f{i}.py\n"
        f"  writes:\n    - pkg/f{i}.py\n"
        for i in range(3)
    )
    plan = (
        "---\ndeliverable_id: dlv-fixture-abc123\n---\n# Fixture\n\n## Problem\n\nWide.\n\n"
        f"## Tasks\n\n```yaml plan-tasks\n{rows}```\n"
    )
    rel = "docs/plans/2026-10-01-wide.md"
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / rel).write_text(plan, encoding="utf-8", newline="\n")
    monkeypatch.setattr(sibling_split, "runtime_cap", lambda cpus=None: 2)
    m = stage(tmp_path, run_id="rwide", plan_rel=rel)
    counts: dict[int, int] = {}
    for r in m.rows:
        counts[r.wave] = counts.get(r.wave, 0) + 1
    assert sorted(counts.values()) == [1, 2]
