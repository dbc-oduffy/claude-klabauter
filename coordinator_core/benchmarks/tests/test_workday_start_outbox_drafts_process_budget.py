"""Standing gate: the workday-start stale outbox drafts reader under the 500ms bar, in-process.

The fixture is 200 drafts across both draft roots, 150 aged past the stale window and `status:
draft`. Process time and spawn count only, never wall clock; the reader spawns nothing, asserted
by patching the spawn entry points.
"""
from __future__ import annotations

import os
import statistics
import subprocess

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.session.machinery_paths import LEGACY_MEMO_OUTBOX_RELDIR, MEMO_OUTBOX_RELDIR
from coordinator_core.workday_start.outbox_drafts import stale_outbox_drafts

SAMPLES = 5
BUDGET_MS = 500.0
DRAFTS = 200
STALE = 150
NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("outbox_drafts_gate")
    for rel in (MEMO_OUTBOX_RELDIR, LEGACY_MEMO_OUTBOX_RELDIR):
        root.joinpath(*rel.split("/")).mkdir(parents=True)
    for i in range(DRAFTS):
        rel = MEMO_OUTBOX_RELDIR if i % 2 else LEGACY_MEMO_OUTBOX_RELDIR
        p = root.joinpath(*rel.split("/")) / f"memo-{i:04d}.md"
        p.write_text(f"---\nstatus: draft\nto: doe\ntitle: t{i}\n---\n\n" + "body\n" * 200, encoding="utf-8")
        age = 72 if i < STALE else 1
        t = NOW - age * 3600
        os.utime(p, (t, t))
    return root


def test_outbox_drafts_reader_process_budget(corpus, monkeypatch):
    spawns: list[object] = []

    def _no_spawn(*a, **k):
        spawns.append(a)
        raise AssertionError("outbox_drafts spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    monkeypatch.setattr(os, "system", _no_spawn)

    code, text = stale_outbox_drafts(corpus, now=NOW)
    assert code == 0
    assert len(text.split("\n")) == STALE + 1, "fixture must exercise the stale path"

    samples = [
        in_process_time_ms(lambda: stale_outbox_drafts(corpus, now=NOW))["process_time_ms"]
        for _ in range(SAMPLES)
    ]
    median = statistics.median(samples)
    print(f"budget: workday-start outbox-drafts median {median:.1f}ms of {BUDGET_MS:.0f}ms, spawns {len(spawns)}, drafts {DRAFTS}")
    assert not spawns
    assert median < BUDGET_MS
