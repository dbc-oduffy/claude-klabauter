"""Standing gate: the workday-start stale executing plans reader under the 500ms bar, in-process.

The fixture matches this tree's plans census (883 plans, 34 executing, every mtime past the
3-day threshold so each status head is read). Process time and spawn count only, never wall
clock; the reader spawns nothing, asserted by patching the spawn entry points.
"""
from __future__ import annotations

import os
import statistics
import subprocess

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.workday_start.stale_plans import stale_executing_plans

SAMPLES = 5
BUDGET_MS = 500.0
PLANS = 883
EXECUTING = 34
NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("stale_plans_gate")
    d = root / "docs" / "plans"
    d.mkdir(parents=True)
    old = NOW - 20 * 86400
    body = "body line\n" * 400
    for i in range(PLANS):
        status = "executing" if i < EXECUTING else "done"
        p = d / f"2026-09-{i:04d}-plan.md"
        p.write_text(f"---\ntitle: p{i}\nstatus: {status}\ncreated: 2026-09-01\n---\n\n{body}", encoding="utf-8")
        os.utime(p, (old, old))
    return root


def test_stale_plans_reader_process_budget(corpus, monkeypatch):
    spawns: list[object] = []

    def _no_spawn(*a, **k):
        spawns.append(a)
        raise AssertionError("stale_plans spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    monkeypatch.setattr(os, "system", _no_spawn)

    code, text = stale_executing_plans(corpus, now=NOW)
    assert code == 0
    assert len(text.split("\n")) == EXECUTING, "fixture must exercise the executing path"

    samples = [
        in_process_time_ms(lambda: stale_executing_plans(corpus, now=NOW))["process_time_ms"]
        for _ in range(SAMPLES)
    ]
    median = statistics.median(samples)
    print(f"budget: workday-start stale-plans median {median:.1f}ms of {BUDGET_MS:.0f}ms, spawns {len(spawns)}, plans {PLANS}")
    assert not spawns
    assert median < BUDGET_MS
