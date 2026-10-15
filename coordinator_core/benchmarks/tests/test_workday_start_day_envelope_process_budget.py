"""Standing gate: the workday-start day envelope under the 500ms bar, in-process, zero spawns.

The repo-bound readers run over a fixture matching this tree's census (883 plans, 636 handoffs,
the origin-stub join exercised); the machine-bound readers (addon health, hook and git-perf
currency, registry) read the live box. Process time and spawn count only, never wall clock; a
reader error fails the gate so a failed reader can never pass as a fast one.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.workday_start.day_envelope import day_envelope

SAMPLES = 5
BUDGET_MS = 500.0
PLANS = 883
HANDOFFS = 636
STUBS = 40


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("day_envelope_gate")
    plans = root / "docs" / "plans"
    handoffs = root / "state" / "handoffs"
    plans.mkdir(parents=True)
    handoffs.mkdir(parents=True)
    body = "body line\n" * 400
    for i in range(PLANS):
        pair = f"roadmap_id: r\nstub_id: s{i}\n" if i < STUBS else ""
        (plans / f"2026-09-{i:04d}-plan.md").write_text(
            f"---\ntitle: p{i}\nstatus: implemented\n{pair}---\n\n{body}", encoding="utf-8"
        )
    for i in range(HANDOFFS):
        stub = f"kind: spinoff-roadmap\ndeployment_state: ready_to_fire\nroadmap_id: r\nstub_id: s{i}\n" if i < STUBS else "kind: session\n"
        (handoffs / f"2026-09-{i:04d}-h.md").write_text(f"---\ntitle: h{i}\n{stub}---\n\n{body}", encoding="utf-8")
    return root


def test_day_envelope_process_budget(corpus, monkeypatch):
    spawns: list[object] = []

    def _no_spawn(*a, **k):
        spawns.append(a)
        raise AssertionError("day_envelope spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    monkeypatch.setattr(os, "system", _no_spawn)

    code, text = day_envelope(corpus)
    envelope = json.loads(text)
    assert code == 0 and "reader_errors" not in envelope, envelope.get("reader_errors")
    assert "j-stale-origin-stubs" in {p["id"] for p in envelope["judgment_points"]}, "stub join not exercised"

    samples = [in_process_time_ms(lambda: day_envelope(corpus))["process_time_ms"] for _ in range(SAMPLES)]
    median = statistics.median(samples)
    print(
        f"budget: workday-start day-envelope median {median:.1f}ms of {BUDGET_MS:.0f}ms "
        f"(samples {[round(s, 1) for s in samples]}), spawns {len(spawns)}, plans {PLANS}, handoffs {HANDOFFS}"
    )
    assert not spawns
    assert median < BUDGET_MS
