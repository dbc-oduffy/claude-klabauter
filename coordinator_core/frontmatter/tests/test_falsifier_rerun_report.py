
from __future__ import annotations

from pathlib import Path

from coordinator_core.frontmatter.falsifier_rerun_report import scan_plans

_FM_HEADER = "---\n"
_FM_FOOTER = "\n---\n\nbody\n"


def _write_plan(tmp_path: Path, name: str, frontmatter_yaml: str) -> Path:
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / name
    path.write_text(_FM_HEADER + frontmatter_yaml + _FM_FOOTER, encoding="utf-8")
    return plans_dir


def test_terminal_plan_with_falsifier_and_no_rerun_appears_in_missing():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        plans_dir = _write_plan(
            Path(td),
            "no-rerun.md",
            (
                "status: implemented\n"
                "prime_exit_criterion:\n"
                "  statement: x\n"
                "  falsifier:\n"
                "    how: run the thing\n"
                "    baseline_output: red\n"
                "    baseline_ref: deadbeef\n"
                "    expected_when_true: green\n"
            ),
        )
        report = scan_plans(plans_dir)

    assert report.with_falsifier == 1
    assert len(report.terminal_with_falsifier) == 1
    missing_paths = [r.repo_rel_path for r in report.missing_rerun]
    assert any("no-rerun.md" in p for p in missing_paths)
    assert len(report.with_rerun) == 0


def test_terminal_plan_with_recorded_rerun_does_not_appear_in_missing():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        plans_dir = _write_plan(
            Path(td),
            "has-rerun.md",
            (
                "status: implemented\n"
                "prime_exit_criterion:\n"
                "  statement: x\n"
                "  falsifier:\n"
                "    how: run the thing\n"
                "    baseline_output: red\n"
                "    baseline_ref: deadbeef\n"
                "    expected_when_true: green\n"
                "exit_criterion_met:\n"
                "  asserted: true\n"
                "  falsifier_output: green (re-run at HEAD)\n"
                "  falsifier_verdict: pass\n"
            ),
        )
        report = scan_plans(plans_dir)

    assert report.with_falsifier == 1
    assert len(report.terminal_with_falsifier) == 1
    missing_paths = [r.repo_rel_path for r in report.missing_rerun]
    assert not any("has-rerun.md" in p for p in missing_paths)
    with_rerun_paths = [r.repo_rel_path for r in report.with_rerun]
    assert any("has-rerun.md" in p for p in with_rerun_paths)


def test_non_terminal_plan_with_falsifier_is_excluded():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        plans_dir = _write_plan(
            Path(td),
            "draft-plan.md",
            (
                "status: draft\n"
                "prime_exit_criterion:\n"
                "  statement: x\n"
                "  falsifier:\n"
                "    how: run the thing\n"
                "    baseline_output: red\n"
                "    baseline_ref: deadbeef\n"
                "    expected_when_true: green\n"
            ),
        )
        report = scan_plans(plans_dir)

    assert report.with_falsifier == 1
    assert len(report.terminal_with_falsifier) == 0


def test_terminal_plan_with_no_falsifier_at_all_is_excluded():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        plans_dir = _write_plan(
            Path(td),
            "no-falsifier.md",
            (
                "status: implemented\n"
                "prime_exit_criterion:\n"
                "  statement: x\n"
                "  derived_from: state/sizings/x.yaml\n"
            ),
        )
        report = scan_plans(plans_dir)

    assert report.with_falsifier == 0
    assert len(report.terminal_with_falsifier) == 0


def test_close_out_adhoc_keys_derived_not_hand_listed_and_status_independent():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        plans_dir = _write_plan(
            Path(td),
            "adhoc.md",
            (
                "status: executing\n"
                "prime_exit_criterion:\n"
                "  statement: x\n"
                "  falsifier:\n"
                "    how: run the thing\n"
                "    baseline_output: red\n"
                "    baseline_ref: deadbeef\n"
                "    expected_when_true: green\n"
                "    a_brand_new_never_before_seen_spelling: some_value\n"
            ),
        )
        report = scan_plans(plans_dir)

    assert len(report.terminal_with_falsifier) == 0
    keys = {a.key: a for a in report.close_out_key_attempts}
    assert "a_brand_new_never_before_seen_spelling" in keys
    attempt = keys["a_brand_new_never_before_seen_spelling"]
    assert attempt.count == 1
    assert any("adhoc.md" in p for p in attempt.repo_rel_paths)


def test_close_out_adhoc_key_outside_close_out_area_is_not_reported():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        plans_dir = _write_plan(
            Path(td),
            "unrelated-adhoc.md",
            (
                "status: draft\n"
                "grouping_approvals:\n"
                "  some_unrelated_grouping_the_schema_rejects:\n"
                "    status: pending\n"
            ),
        )
        report = scan_plans(plans_dir)

    keys = {a.key for a in report.close_out_key_attempts}
    assert "some_unrelated_grouping_the_schema_rejects" not in keys


def test_render_does_not_claim_a_green_count_when_something_is_missing():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        plans_dir = _write_plan(
            Path(td),
            "no-rerun.md",
            (
                "status: implemented\n"
                "prime_exit_criterion:\n"
                "  statement: x\n"
                "  falsifier:\n"
                "    how: run the thing\n"
                "    baseline_output: red\n"
                "    baseline_ref: deadbeef\n"
                "    expected_when_true: green\n"
            ),
        )
        report = scan_plans(plans_dir)

    rendered = report.render()
    assert "No re-run recorded:" in rendered
    assert "no-rerun.md" in rendered


def test_live_corpus_runs_clean_and_within_budget():
    report = scan_plans()

    assert report.scanned_files > 0
    assert report.with_falsifier >= 0
    assert report.process_time_seconds < 0.5, (
        f"process_time_seconds={report.process_time_seconds} exceeds the "
        "500ms brightline"
    )
