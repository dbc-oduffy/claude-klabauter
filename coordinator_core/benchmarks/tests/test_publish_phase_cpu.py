"""Tests for the publish per-phase driver-CPU comparator."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.benchmarks import publish_phase_cpu as ppc

SUFFIX = "(driver only, excludes subprocesses)"


def _line(row: str, label: str, cpu: float) -> str:
    return f"  [timing]   {row}: {label}: {cpu + 1:.3f}s wall / {cpu:.3f}s cpu {SUFFIX}"


def _log(rows: list[str], scale: float = 1.0, rkl: float = 0.0) -> str:
    out = []
    for row in rows:
        out.append(_line(row, "run_pre_sync_gates", 4.0 * scale))
        out.append(_line(row, "dispatch_preswap_payload_parity_gate", 3.0 * scale))
        out.append(_line(row, "dispatch_percolate_post_rsync", 2.0 * scale))
        out.append(_line(row, "dispatch_percolate_pre_ci", 1.0 * scale))
        out.append(_line(row, "dispatch_mirror_like", 0.5 * scale))
        out.append(_line(row, "assert_dest_on_declared_ref", 9.0))
    out.append(_line("<round>", "dispatch_end_of_run_repo_key_literal_gate", rkl))
    return "\n".join(out) + "\n"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


ROWS = ["a", "b"]


def test_parse_sums_phases_over_rows_and_excludes_round_pseudo_row() -> None:
    totals, rows = ppc.parse_log(_log(ROWS, rkl=5.0))
    assert rows == {"a", "b"}
    assert totals["pre_sync_gates"] == 8.0
    assert totals["payload_parity"] == 6.0
    assert totals["repo_key_literal"] == 5.0


def test_self_comparison_passes(tmp_path: Path) -> None:
    p = _write(tmp_path, "base.txt", _log(ROWS))
    code, _ = ppc.run(p, p)
    assert code == ppc.EXIT_OK


def test_ten_x_regression_names_every_phase(tmp_path: Path) -> None:
    base = _write(tmp_path, "base.txt", _log(ROWS))
    after = _write(tmp_path, "after.txt", _log(ROWS, scale=10.0, rkl=60.0))
    code, report = ppc.run(after, base)
    assert code == ppc.EXIT_REGRESSION
    for phase in ppc.PHASE_LABELS.keys() - ppc.INFORMATIONAL_PHASES:
        assert phase in report.splitlines()[-1]


def test_just_under_threshold_passes_and_just_over_fails(tmp_path: Path) -> None:
    base = _write(tmp_path, "base.txt", _log(ROWS))
    under = _write(tmp_path, "under.txt", _log(ROWS, scale=1.49))
    over = _write(tmp_path, "over.txt", _log(ROWS, scale=1.51))
    assert ppc.run(under, base)[0] == ppc.EXIT_OK
    assert ppc.run(over, base)[0] == ppc.EXIT_REGRESSION


def test_repo_key_literal_absolute_bound(tmp_path: Path) -> None:
    base = _write(tmp_path, "base.txt", _log(ROWS))
    ok = _write(tmp_path, "ok.txt", _log(ROWS, rkl=9.9))
    bad = _write(tmp_path, "bad.txt", _log(ROWS, rkl=10.5))
    assert ppc.run(ok, base)[0] == ppc.EXIT_OK
    code, report = ppc.run(bad, base)
    assert code == ppc.EXIT_REGRESSION
    assert "repo_key_literal" in report.splitlines()[-1]


def test_row_set_mismatch_is_distinct_and_never_a_ratio(tmp_path: Path) -> None:
    base = _write(tmp_path, "base.txt", _log(ROWS))
    after = _write(tmp_path, "after.txt", _log(["a"], scale=100.0))
    code, report = ppc.run(after, base)
    assert code == ppc.EXIT_ROWSET_DIFFERS
    assert "row sets differ" in report
    assert "REGRESSED" not in report


def test_log_without_timing_lines_is_not_a_pass(tmp_path: Path) -> None:
    base = _write(tmp_path, "base.txt", _log(ROWS))
    empty = _write(tmp_path, "empty.txt", "nothing here\n")
    assert ppc.run(empty, base)[0] == ppc.EXIT_ROWSET_DIFFERS


SWEEP_LINE = (
    "  [timing] a: content_transform_sweep: 12.500s wall / 7.250s cpu "
    f"{SUFFIX}; files=9836"
)


def test_sweep_timing_line_is_reported_as_a_phase() -> None:
    totals, rows = ppc.parse_log(SWEEP_LINE + "\n")
    assert rows == {"a"}
    assert totals["content_transform_sweep"] == 7.25


def test_sweep_phase_is_reported_but_never_gates_against_a_log_that_predates_it(tmp_path: Path) -> None:
    base = _write(tmp_path, "base.txt", _log(ROWS))
    after = _write(tmp_path, "after.txt", _log(ROWS) + SWEEP_LINE.replace(" a:", " b:") + "\n")
    code, report = ppc.run(after, base)
    assert code == ppc.EXIT_OK
    sweep = next(ln for ln in report.splitlines() if ln.startswith("content_transform_sweep"))
    assert "7.2s" in sweep and "informational" in sweep and "REGRESSED" not in sweep


def test_gated_phase_still_regresses_alongside_the_sweep_phase(tmp_path: Path) -> None:
    base = _write(tmp_path, "base.txt", _log(ROWS))
    after = _write(tmp_path, "after.txt", _log(ROWS, scale=10.0) + SWEEP_LINE + "\n")
    code, report = ppc.run(after, base)
    assert code == ppc.EXIT_REGRESSION
    assert "content_transform_sweep" not in report.splitlines()[-1]


def test_main_returns_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _write(tmp_path, "base.txt", _log(ROWS))
    after = _write(tmp_path, "after.txt", _log(ROWS, scale=10.0))
    assert ppc.main([str(after), "--baseline", str(base)]) == ppc.EXIT_REGRESSION
    assert "pre_sync_gates" in capsys.readouterr().out
