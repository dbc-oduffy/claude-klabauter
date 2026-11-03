"""Liveness of 50 held runs with 100MiB+ owner transcripts is decided without a transcript read, inside the bar."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import liveness_record_scan_budget as bench


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    base = tmp_path_factory.mktemp("liveness_budget")
    return bench.measure_all(base)


def test_fixture_holds_fifty_runs_with_hundred_mib_transcripts(measured):
    fixtures, _ = measured
    fixture = fixtures[bench.SURFACES[0]]
    assert len(fixture.minted) == bench.MINTED_RUNS and len(fixture.halted) == bench.HALTED_RUNS
    assert len(fixture.transcripts) == bench.HELD_RUNS
    assert all(p.stat().st_size >= 100 * 1024 * 1024 for p in fixture.transcripts)


def test_the_open_counter_sees_a_transcript_open(measured):
    fixture = measured[0][bench.SURFACES[0]]
    with bench.counting_opens(fixture.transcripts) as opens:
        open(fixture.transcripts[0], "rb").close()
        open(fixture.transcripts[1], "rb").close()
        (fixture.root / ".git" / "unwatched").write_text("x", encoding="utf-8")
    assert len(opens) == 2


@pytest.mark.parametrize("surface", bench.SURFACES)
def test_surface_opens_no_transcript(measured, surface):
    _, rows = measured
    assert rows[surface].transcript_opens == 0, f"{surface} opened transcripts {rows[surface].transcript_opens}x"


@pytest.mark.parametrize("surface", bench.SURFACES)
def test_surface_is_under_the_bar(measured, surface):
    _, rows = measured
    assert rows[surface].process_ms < bench.BAR_MS, f"{surface} took {rows[surface].process_ms}ms process time"
