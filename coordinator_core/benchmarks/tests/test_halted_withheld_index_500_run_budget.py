"""The halted-withheld index read on ask_stage's and record_halted_run_landed's paths re-reads only changed run files, inside the process-time bar."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
from coordinator_core.ops.dispatch_emit.write_baseline import exclude_halted_withheld

RUNS = 500
BAR_MS = 500.0
SIZING = "state/sizings/subject.yaml"


@pytest.fixture
def index_500(tmp_path, monkeypatch):
    """A tmp repo whose index holds RUNS run files, built; `reads` collects every file `_parse_file` opens."""
    idx._STAT_CACHE.clear()
    for i in range(RUNS):
        idx.add_run(
            tmp_path, f"ask-20261113T000000-{i:06x}", [f"halted/other_{i}.txt"],
            sizing_rel=f"state/sizings/halted-{i}.yaml", prints={f"halted/other_{i}.txt": f"blob{i}"}, declared=[],
        )
    idx.atomic_write_bytes(idx._dir(tmp_path) / idx.SENTINEL, b"")
    reads: list = []
    real = idx._parse_file
    monkeypatch.setattr(idx, "_parse_file", lambda file: (reads.append(os.fspath(file)), real(file))[1])
    yield tmp_path, reads
    idx._STAT_CACHE.clear()


def _ask_stage_read(root):
    """The halted-withheld read ask_stage runs at stage (write_baseline.exclude_halted_withheld)."""
    captured = {f"halted/other_{i}.txt": f"blob{i}" for i in range(0, RUNS, 50)}
    return exclude_halted_withheld(root, captured, SIZING, "ask-20261113T000000-self")


def _timed(fn):
    start = time.process_time()
    result = fn()
    return result, (time.process_time() - start) * 1000.0


def test_cold_pass_reads_every_run_file_once(index_500):
    root, reads = index_500
    _ask_stage_read(root)
    assert len(reads) == RUNS


def test_warm_ask_stage_read_opens_no_run_file_and_stays_under_the_bar(index_500):
    root, reads = index_500
    cold = _ask_stage_read(root)
    reads.clear()
    warm, ms = _timed(lambda: _ask_stage_read(root))
    assert warm == cold
    assert reads == []
    assert ms < BAR_MS, f"warm exclude_halted_withheld took {ms:.1f}ms process time"


def test_one_rewritten_run_file_is_the_only_re_read(index_500):
    root, reads = index_500
    _ask_stage_read(root)
    reads.clear()
    target = idx._dir(root) / "ask-20261113T000000-000007.json"
    body = json.loads(target.read_text(encoding="utf-8"))
    body["paths"] = ["halted/other_7.txt", "halted/extra_7.txt"]
    target.write_text(json.dumps(body), encoding="utf-8")  # size changes without going through _store
    _ask_stage_read(root)
    assert reads == [os.fspath(target)]
    reads.clear()
    _ask_stage_read(root)
    assert reads == []


def test_add_run_through_the_store_re_reads_only_that_file(index_500):
    root, reads = index_500
    _ask_stage_read(root)
    reads.clear()
    idx.add_run(root, "ask-20261113T000000-000009", ["halted/changed_9.txt"], sizing_rel="state/sizings/halted-9.yaml", prints={}, declared=[])
    assert idx.entries(root)["ask-20261113T000000-000009"].paths == ("halted/changed_9.txt",)
    assert len(reads) == 1


def test_record_halted_run_landed_index_leg_drops_one_run_and_re_reads_nothing(index_500):
    """mark_landed's index effect is remove_run; the next fresh_entry/run_ids/entries read opens no file."""
    root, reads = index_500
    _ask_stage_read(root)
    reads.clear()
    idx.remove_run(root, "ask-20261113T000000-000003")

    def leg():
        return (
            idx.fresh_entry(root, "ask-20261113T000000-000004"),
            idx.run_ids(root),
            idx.entries(root),
        )

    (_, ids, entries), ms = _timed(leg)
    assert reads == []
    assert len(ids) == len(entries) == RUNS - 1
    assert "ask-20261113T000000-000003" not in entries
    assert ms < BAR_MS, f"warm landed-leg index read took {ms:.1f}ms process time"
