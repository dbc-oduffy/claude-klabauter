"""
Tests for coordinator_core.ops.check_harvest_debt.

Golden-parity fixtures mirror the DoE bash-original oracle case-for-case — this
file is the pytest analog of that bats-style suite, not a fresh test design.

Port of: test-harvest-debt-nudge.sh (DoE 3a561713, 2026-07-22)
"""

from __future__ import annotations

import io
import contextlib
from pathlib import Path

import pytest

from coordinator_core.distill.harvest_debt import compute_harvest_debt
from coordinator_core.ops.check_harvest_debt import main


def _make_fixture(
    root: Path,
    plan_count: int,
    logged_count: int,
    subdir: str = "2026-06",
) -> None:
    specs_dir = root / "archive" / "specs" / subdir
    log_dir = root / "state"
    specs_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    for i in range(1, plan_count + 1):
        (specs_dir / f"2026-06-0{i}-plan-fake-{i}.md").write_text(
            f"# Plan {i}\nstatus: terminal\n"
        )

    lines = ["# Distillation Log\n", "# Columns: run | path | disposition | fate\n", "\n"]
    lines.append("## Run 2026-06-01-10h00 (fixture)\n\n")
    for i in range(1, logged_count + 1):
        lines.append(
            f"- archive/specs/2026-06-0{i}-plan-fake-{i}.md -> DISTILLED, retained "
            f"(fixture) (run: 2026-06-01-10h00)\n"
        )
    (log_dir / "distillation-log.md").write_text("".join(lines))


def _run(root: Path, *extra_args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = main(["--root", str(root), *extra_args])
    return exit_code, out.getvalue().rstrip("\n"), err.getvalue()


def test_six_plans_zero_logged_nudges(tmp_path: Path) -> None:
    _make_fixture(tmp_path, 6, 0)
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == "6 un-harvested archived plans — run /distill"


def test_eight_plans_zero_logged_nudges_with_n8(tmp_path: Path) -> None:
    _make_fixture(tmp_path, 8, 0)
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == "8 un-harvested archived plans — run /distill"


def test_all_logged_silent(tmp_path: Path) -> None:
    _make_fixture(tmp_path, 6, 6)
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_threshold_boundary_five_is_silent(tmp_path: Path) -> None:
    _make_fixture(tmp_path, 8, 3)  # 5 unharvested, threshold is >5
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_six_unharvested_nudges(tmp_path: Path) -> None:
    _make_fixture(tmp_path, 9, 3)  # 6 unharvested
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == "6 un-harvested archived plans — run /distill"


def test_sidecar_files_excluded_from_count(tmp_path: Path) -> None:
    _make_fixture(tmp_path, 6, 0)
    specs_dir = tmp_path / "archive" / "specs" / "2026-06"
    (specs_dir / "2026-06-01-plan-fake-1.review.md").write_text("# review sidecar\n")
    (specs_dir / "2026-06-01-plan-fake-1.v3-divergence-check.md").write_text("# check sidecar\n")
    (specs_dir / "2026-06-01-plan-fake-1.the Director of Engineering-review.md").write_text("# the Director of Engineering review\n")
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == "6 un-harvested archived plans — run /distill"


def test_missing_archive_specs_dir_silent_noop(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "distillation-log.md").write_text(
        "# Distillation Log\n# Columns: run | path | disposition | fate\n"
    )
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_nested_yyyy_mm_subdirs_aggregate_count(tmp_path: Path) -> None:
    for i in range(1, 4):
        d = tmp_path / "archive" / "specs" / "2026-06"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"2026-06-0{i}-plan-{i}.md").write_text("# Plan\n")
    for i in range(1, 5):
        d = tmp_path / "archive" / "specs" / "2026-05"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"2026-05-0{i}-plan-{i}.md").write_text("# Plan\n")
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "distillation-log.md").write_text(
        "# Distillation Log\n# Columns: run | path | disposition | fate\n\n"
    )
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == "7 un-harvested archived plans — run /distill"


def test_absent_canonical_log_fails_loud(tmp_path: Path) -> None:
    d = tmp_path / "archive" / "specs" / "2026-06"
    d.mkdir(parents=True)
    (d / "2026-06-01-plan-fake-1.md").write_text("# Plan\n")
    exit_code, _, err = _run(tmp_path)
    assert exit_code == 1
    assert "distillation-log.md" in err


def test_debt_vs_logged_sanity_warning_fires(tmp_path: Path) -> None:
    _make_fixture(tmp_path, 12, 1)  # 11 unharvested, 1 logged -> 11 > 5*1
    exit_code, out, err = _run(tmp_path)
    assert exit_code == 0
    assert out == "11 un-harvested archived plans — run /distill"
    assert "WARNING" in err
    assert "5x" in err


def test_ascii_arrow_rows_recognized_as_harvested(tmp_path: Path) -> None:
    _make_fixture(tmp_path, 6, 6)
    log_text = (tmp_path / "state" / "distillation-log.md").read_text()
    assert "->" in log_text
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_unicode_arrow_row_also_recognized(tmp_path: Path) -> None:
    specs_dir = tmp_path / "archive" / "specs" / "2026-06"
    specs_dir.mkdir(parents=True)
    (specs_dir / "2026-06-01-plan-fake-1.md").write_text("# Plan\n")
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "distillation-log.md").write_text(
        "# Distillation Log\n# Columns: run | path | disposition | fate\n\n"
        "- archive/specs/2026-06-01-plan-fake-1.md → DISTILLED, retained "
        "(fixture) (run: 2026-06-01-10h00)\n"
    )
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_probe_and_engine_agree_on_mixed_corpus_sidecar_exclusion(
    tmp_path: Path,
) -> None:
    # C5: probe (basename-keyed) and engine (specs_dir-relative-keyed) count
    # DIFFERENT things in general (see module docstring negative-spec), but on
    # a corpus with no basename collisions across subdirs AND no action-table
    # rows (the probe only recognizes canonical DISTILLED/PROMOTE arrow rows),
    # they must land on the SAME un-harvested count once both consume the
    # shared C1 sidecar vocabulary — this is the parity this task's brief
    # calls for, not a claim the two are interchangeable in general.
    specs_dir = tmp_path / "archive" / "specs" / "2026-06"
    specs_dir.mkdir(parents=True)
    harvested_names = [f"2026-06-0{i}-plan-fake-{i}.md" for i in range(1, 5)]  # 4
    unharvested_names = [f"2026-06-1{i}-plan-fake-{i}.md" for i in range(1, 8)]  # 7
    sidecar_names = [
        "2026-06-01-plan-fake-1.review.md",
        "2026-06-02-plan-fake-2.v3-divergence-check.md",
    ]
    for name in harvested_names + unharvested_names + sidecar_names:
        (specs_dir / name).write_text(f"# {name}\n")

    log_dir = tmp_path / "state"
    log_dir.mkdir(parents=True)
    lines = ["# Distillation Log\n", "# Columns: run | path | disposition | fate\n", "\n"]
    lines.append("## Run 2026-06-01-10h00\n\n")
    for name in harvested_names:
        lines.append(
            f"- archive/specs/2026-06/{name} -> DISTILLED, retained "
            f"fixture (run: 2026-06-01-10h00)\n"
        )
    log_path = log_dir / "distillation-log.md"
    log_path.write_text("".join(lines))

    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == "7 un-harvested archived plans — run /distill"
    probe_unharvested = 7

    result = compute_harvest_debt(tmp_path / "archive" / "specs", log_path)
    assert result.total_specs == 11  # 4 harvested + 7 unharvested, sidecars excluded
    assert len(result.harvest_debt) == probe_unharvested


def test_no_git_and_no_explicit_root_returns_zero_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.check_harvest_debt.which", lambda _name: None
    )
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = main([])
    assert exit_code == 0
    assert out.getvalue() == ""
