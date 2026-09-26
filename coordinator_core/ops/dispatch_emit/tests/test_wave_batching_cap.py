"""Tests for item 4: the wave composer batches write-capable executors at
<=5 per ``await parallel([...])`` group, ahead of the wave's own single
commit barrier. Read-only rows never count toward the cap.

Spec: docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md (IBMDT-C4).
Source ask: cross-repo memo archive/2026-09-11-doe-claude-em-mise-
concurrency-cap-unemittable.md -- "batch a wave into sequential
`parallel()` calls of at most five" without inventing a `depends_on` edge.
"""

import re

from coordinator_core.ops.dispatch_emit.emit import (
    _split_wave_for_parallel_cap,
    _wave_agent_calls,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_META_PHASE_LINE_RE = re.compile(r"phases\s*:\s*\[([^\]]*)\]")
_JS_STRING_LITERAL_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def _extract_phase_titles(script: str) -> list[str]:
    m = _META_PHASE_LINE_RE.search(script)
    assert m is not None
    return [
        literal.replace("\\'", "'").replace("\\\\", "\\")
        for literal in _JS_STRING_LITERAL_RE.findall(m.group(1))
    ]


def _write_row(id_: str) -> WaveRow:
    return WaveRow(
        id=id_,
        title=f"Title for {id_}",
        surface="dispatch_emit",
        writes=[f"pkg/{id_}.py"],
        reads=[],
        depends_on=[],
    )


def _readonly_row(id_: str) -> WaveRow:
    return WaveRow(
        id=id_,
        title=f"Title for {id_}",
        surface="dispatch_emit",
        writes=[],
        reads=[],
        depends_on=[],
    )


def test_five_write_rows_stay_in_one_group():
    wave = [_write_row(f"C{i}") for i in range(1, 6)]
    assert _split_wave_for_parallel_cap(wave) == [wave]


def test_seven_write_rows_split_into_five_and_two():
    wave = [_write_row(f"C{i}") for i in range(1, 8)]
    groups = _split_wave_for_parallel_cap(wave)
    assert [row.id for row in groups[0]] == ["C1", "C2", "C3", "C4", "C5"]
    assert [row.id for row in groups[1]] == ["C6", "C7"]


def test_read_only_rows_never_count_toward_the_cap():
    wave = [_write_row(f"C{i}") for i in range(1, 6)] + [
        _readonly_row("R1"),
        _readonly_row("R2"),
    ]
    groups = _split_wave_for_parallel_cap(wave)
    assert len(groups) == 1
    assert [row.id for row in groups[0]] == [
        "C1",
        "C2",
        "C3",
        "C4",
        "C5",
        "R1",
        "R2",
    ]


def test_read_only_rows_do_not_reorder_around_a_split():
    wave = (
        [_write_row(f"C{i}") for i in range(1, 6)]
        + [_readonly_row("R1")]
        + [_write_row(f"C{i}") for i in range(6, 8)]
    )
    groups = _split_wave_for_parallel_cap(wave)
    assert [row.id for row in groups[0]] == ["C1", "C2", "C3", "C4", "C5", "R1"]
    assert [row.id for row in groups[1]] == ["C6", "C7"]


def test_wave_agent_calls_at_cap_emits_exactly_one_parallel_group():
    wave = [_write_row(f"C{i}") for i in range(1, 6)]
    call = _wave_agent_calls(wave, "Wave 1: C1..C5", None, "wave1Results")
    assert call.count("await parallel([") == 1


def test_wave_agent_calls_emits_two_parallel_groups_for_seven_write_rows():
    wave = [_write_row(f"C{i}") for i in range(1, 8)]
    call = _wave_agent_calls(wave, "Wave 1: C1..C7", None, "wave1Results")
    assert call.count("await parallel([") == 2
    assert (
        "const wave1Results = [...wave1ResultsGroup1, ...wave1ResultsGroup2];"
        in call
    )


def test_seven_row_write_wave_commits_once_through_compose_script():
    wave = [_write_row(f"C{i}") for i in range(1, 8)]
    script = compose_script([wave], name="wf", description="seven write rows")

    phase_titles = _extract_phase_titles(script)
    commit_titles = [t for t in phase_titles if t.startswith("Commit wave")]
    assert commit_titles == ["Commit wave 1"]
    assert script.count("await parallel([") == 2
