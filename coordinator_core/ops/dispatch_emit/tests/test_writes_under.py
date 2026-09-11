"""
Tests for ``writes_under:`` -- run-time-named write prefixes, end to end
across the dispatch-emit pipeline (spine_read -> wave_map -> pathspec ->
emit).

Source: state/improvement-queue/2026-09-11-dispatch-emit-takes-a-writes-under-prefi-309100e2b36b.yaml.
"""

from __future__ import annotations

import logging
import re

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.pathspec import (
    DirectoryShapedWriteError,
    NoWritesDeclaredError,
    commit_pathspec,
    commit_prefixes,
    terminal_test_scope,
)
from coordinator_core.ops.dispatch_emit.spine_read import (
    UNDECLARED,
    EmitterRow,
    FileShapedPrefixError,
    InvalidFieldTypeError,
    read_spine,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow, build_waves

_HEADER = "# fixture plan\n\n## Tasks\n\n"
_AUDITS = "state/audits/"


def _write_plan(tmp_path, body: str):
    path = tmp_path / "plan.md"
    path.write_text(_HEADER + "```yaml plan-tasks\n" + body + "\n```\n", encoding="utf-8")
    return path


def _row(id_, writes, writes_under=(), reads=None):
    return EmitterRow(
        id=id_,
        title=f"title-{id_}",
        surface="test",
        writes=writes,
        reads=reads or [],
        depends_on=[],
        writes_under=tuple(writes_under),
    )


def _wave_row(id_, writes, writes_under=()):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface="dispatch_emit",
        writes=writes,
        reads=[],
        depends_on=[],
        writes_under=tuple(writes_under),
    )


# ---------------------------------------------------------------------------
# spine_read
# ---------------------------------------------------------------------------


def test_a_prefix_only_row_reads_as_declared_empty_writes(tmp_path):
    body = """\
- id: C1
  title: dated audit
  surface: state/audits
  writes_under:
    - state/audits/
"""
    (row,) = read_spine(_write_plan(tmp_path, body))
    assert row.writes == []
    assert row.writes_under == (_AUDITS,)


def test_a_row_without_the_key_keeps_undeclared_writes_and_no_prefix(tmp_path):
    body = """\
- id: C1
  title: nothing declared
  surface: some/surface
"""
    (row,) = read_spine(_write_plan(tmp_path, body))
    assert row.writes is UNDECLARED
    assert row.writes_under == ()


def test_a_backslash_terminated_prefix_is_accepted(tmp_path):
    body = """\
- id: C1
  title: windows-authored prefix
  surface: state/audits
  writes_under:
    - "state\\\\audits\\\\"
"""
    (row,) = read_spine(_write_plan(tmp_path, body))
    assert row.writes_under == ("state\\audits\\",)


def test_a_file_shaped_prefix_is_refused_naming_row_and_entry(tmp_path):
    body = """\
- id: C1
  title: file in the prefix field
  surface: state/audits
  writes_under:
    - state/audits/report.md
"""
    with pytest.raises(FileShapedPrefixError) as excinfo:
        read_spine(_write_plan(tmp_path, body))
    assert "C1" in str(excinfo.value)
    assert "state/audits/report.md" in str(excinfo.value)


def test_a_scalar_prefix_is_refused(tmp_path):
    body = """\
- id: C1
  title: scalar prefix
  surface: state/audits
  writes_under: state/audits/
"""
    with pytest.raises(InvalidFieldTypeError):
        read_spine(_write_plan(tmp_path, body))


def test_an_epistemic_premise_gated_prefix_row_is_not_held_out(tmp_path):
    # The holdout keys on UNDECLARED writes. A prefix row has declared where
    # it writes, so it must be scheduled after its gate, not held.
    body = """\
- id: C1
  title: decides
  surface: a.py
  writes:
    - a.py
- id: C2
  title: records the verdict in a dated audit
  surface: state/audits
  writes_under:
    - state/audits/
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
"""
    waves = build_waves(read_spine(_write_plan(tmp_path, body)))
    assert [[row.id for row in wave] for wave in waves] == [["C1"], ["C2"]]
    assert waves[1][0].writes_under == (_AUDITS,)


# ---------------------------------------------------------------------------
# wave_map
# ---------------------------------------------------------------------------


def test_two_rows_under_one_prefix_cannot_share_a_wave():
    waves = build_waves([_row("C1", [], [_AUDITS]), _row("C2", [], [_AUDITS])])
    assert len(waves) == 2


def test_a_prefix_and_a_file_beneath_it_cannot_share_a_wave():
    waves = build_waves(
        [_row("C1", [], [_AUDITS]), _row("C2", ["state/audits/2026-09-11-x.md"])]
    )
    assert len(waves) == 2


def test_disjoint_prefixes_share_a_wave():
    waves = build_waves([_row("C1", [], [_AUDITS]), _row("C2", [], ["state/lessons/"])])
    assert len(waves) == 1


def test_a_reader_under_a_prefix_lands_after_its_writer():
    rows = [
        _row("C2", ["b.py"], reads=["state/audits/2026-09-11-x.md"]),
        _row("C1", [], [_AUDITS]),
    ]
    waves = build_waves(rows)
    assert [[row.id for row in wave] for wave in waves] == [["C1"], ["C2"]]


# ---------------------------------------------------------------------------
# pathspec
# ---------------------------------------------------------------------------


def test_a_prefix_only_wave_returns_an_empty_static_pathspec_without_refusing(caplog):
    wave = [_wave_row("C1", [], [_AUDITS])]
    with caplog.at_level(logging.WARNING):
        assert commit_pathspec(wave) == []
    assert "writes: [] declared" not in caplog.text
    assert commit_prefixes(wave) == [("C1", (_AUDITS,))]


def test_prefixes_stay_attributed_to_their_own_row():
    wave = [
        _wave_row("C1", ["a.py"]),
        _wave_row("C2", [], [_AUDITS]),
        _wave_row("C3", [], ["state/lessons/"]),
    ]
    assert commit_pathspec(wave) == ["a.py"]
    assert commit_prefixes(wave) == [("C2", (_AUDITS,)), ("C3", ("state/lessons/",))]


def test_an_all_empty_wave_without_a_prefix_still_refuses():
    with pytest.raises(NoWritesDeclaredError):
        commit_pathspec([_wave_row("C1", []), _wave_row("C2", [])])


def test_the_directory_shaped_refusal_names_the_prefix_spelling():
    with pytest.raises(DirectoryShapedWriteError) as excinfo:
        commit_pathspec([_wave_row("C1", [_AUDITS])])
    assert "writes_under:" in str(excinfo.value)


def test_a_prefix_only_spine_has_a_legitimately_empty_test_scope():
    assert terminal_test_scope([[_wave_row("C1", [], [_AUDITS])]]) == []


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------


def test_a_prefix_only_wave_keeps_its_commit_phase_with_the_widening_rule():
    script = compose_script(
        [[_wave_row("C1", [], [_AUDITS])]], name="wf", description="prefix wave"
    )
    assert "phase('Commit wave 1')" in script
    assert "RUN-TIME-NAMED WRITES" in script
    assert "C1: \\`state/audits/\\`" in script
    assert "commit phase omitted" not in script


def test_a_wave_without_prefixes_carries_no_widening_rule():
    script = compose_script(
        [[_wave_row("C1", ["a.py"])]], name="wf", description="plain wave"
    )
    assert "RUN-TIME-NAMED WRITES" not in script
    assert "write PREFIXES" not in script


def test_the_preflight_checks_prefixes_for_ignore_rules_not_claims():
    script = compose_script(
        [[_wave_row("C1", ["a.py"], [_AUDITS])]], name="wf", description="mixed"
    )
    preflight = script.split("phase('Preflight: commit claimability')", 1)[1]
    preflight = preflight.split("phase(", 1)[0]
    assert "every path in [a.py]" in preflight
    assert "write PREFIXES" in preflight
    assert _AUDITS in preflight


def test_the_executor_names_its_own_prefix_files_and_never_runs_porcelain_over_the_prefix():
    contract = emit._row_return_contract(
        _wave_row("C1", ["a.py"], [_AUDITS]), "docs/plans/2026-09-11-x.md"
    )
    assert "created-under-prefix:" in contract
    footprint = re.search(r"outside this footprint: (.*?)\. If", contract).group(1)
    assert _AUDITS in footprint
    # The self-verify step carries a `<footprint paths>` placeholder; the
    # DONE-summary clause is the one rendered with this row's real paths.
    (porcelain,) = [
        paths
        for paths in re.findall(r"git status --porcelain -- (.*?) \| cut", contract)
        if paths != "<footprint paths>"
    ]
    assert "a.py" in porcelain
    assert "state/audits" not in porcelain


def test_a_row_without_prefixes_gets_no_prefix_claim_field():
    contract = emit._row_return_contract(
        _wave_row("C1", ["a.py"]), "docs/plans/2026-09-11-x.md"
    )
    assert "created-under-prefix:" not in contract
