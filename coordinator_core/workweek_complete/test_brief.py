"""coordinator_core/workweek_complete/test_brief.py — C4a pin: Step 2's
directive resolves through the validate gate CLI, not the standalone
resolver.

Spec backlink: docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md
(C4, Design decision 2 / Problem 3). The repoint itself and CONSUMES_MANIFEST
membership were already landed by a prior session on this branch (verified on
disk: `d_step2_resolve_validation_cmd` already carries
`cli="validate-fast-and-packageability"`, `args=["fast"]`, and
`coordinator-resolve-validation-cmd` is not, and has never needed to be, a
CONSUMES_MANIFEST member -- no other directive ever named it). This file adds
the row's own declared test surface (`coordinator_core/workweek_complete/
test_brief.py`) as a focused, file-local pin, alongside the pre-existing
broader pin in `test_workweek_complete_contract.py::
test_step2_directive_names_the_validate_gate_cli_fast_subcommand`.
"""

from __future__ import annotations

import argparse
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from coordinator_core.workweek_complete import brief as wwc_brief


def test_step2_directive_repointed_at_validate_gate_fast_subcommand() -> None:
    directive = next(
        d
        for d in wwc_brief._build_directives()
        if d["id"] == "d_step2_resolve_validation_cmd"
    )
    assert directive["cli"] == "validate-fast-and-packageability"
    assert directive["args"] == ["fast"]


def test_standalone_resolver_cli_is_not_a_manifest_member() -> None:
    assert "coordinator-resolve-validation-cmd" not in wwc_brief.CONSUMES_MANIFEST


def test_validate_gate_cli_is_a_manifest_member() -> None:
    assert "validate-fast-and-packageability" in wwc_brief.CONSUMES_MANIFEST


def _load_trampoline_parser() -> argparse.ArgumentParser:
    path = (
        Path(__file__).resolve().parents[2]
        / "coordinator"
        / "bin"
        / "workweek-complete-doc-staleness.py"
    )
    spec = importlib.util.spec_from_file_location("wwc_doc_staleness_trampoline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._build_parser()


def test_no_directive_runs_the_doc_staleness_cli() -> None:
    directives = wwc_brief._build_directives(repo_root=None)
    assert not any(d["cli"] == "workweek-complete-doc-staleness" for d in directives)
    assert not any(
        d["id"].startswith("d_step4b_4k_doc_staleness") for d in directives
    )


def test_lint_frontmatter_is_not_a_weekly_directive() -> None:
    # PM turn 23: the whole-tree frontmatter sweep is update-docs Phase 11d's.
    assert "lint-frontmatter" not in wwc_brief.CONSUMES_MANIFEST
    ids = [d["id"] for d in wwc_brief._build_directives(repo_root=None)]
    assert "d_step2_5_lint_frontmatter" not in ids


def test_trampoline_parser_takes_doc_and_has_no_ack_flag() -> None:
    parser = _load_trampoline_parser()
    assert parser.parse_args(["--doc", "README.md"]).doc == "README.md"
    with pytest.raises(SystemExit):
        parser.parse_args(["--doc", "README.md", "--ack"])


_CONCLUDING = (
    "d_step13_archive_close",
    "d_step13_5_post_command_hook",
    "jp_step8_5_loe_high_water",
    "jp_step9_editorial_bucketing",
    "jp_step9_pm_release_notes_gate",
    "jp_step10_semver_judgment",
    "jp_step10_5_gh_release_publish",
)


def _brief_at(
    monkeypatch, tmp_path: Path, anchor: str | None, now: datetime, **flags
) -> tuple[int, dict]:
    from coordinator_core.workweek_complete.brief_inputs import BriefInputs

    if anchor is not None:
        header = tmp_path / "state" / "week-changelog" / "HEADER.md"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text(f"**Week starting:** {anchor}\n", encoding="utf-8")
    monkeypatch.setattr(
        wwc_brief,
        "compute_brief_inputs",
        lambda *a, **k: BriefInputs(repo_root=tmp_path.as_posix()),
    )
    monkeypatch.setattr(wwc_brief, "resolve_operator_config", lambda env=None: None)
    return wwc_brief.brief(now_local=now, **flags)


def _ids(envelope: dict) -> set[str]:
    return {d["id"] for d in envelope["directives"]} | {
        p["id"] for p in envelope["judgment_points"]
    }


def test_catch_up_archives_each_missed_week_and_omits_every_concluding_id(
    monkeypatch, tmp_path: Path
) -> None:
    code, env = _brief_at(monkeypatch, tmp_path, "2026-09-28", datetime(2026, 10, 14, 10, 0), catch_up=True)
    assert code == 0
    assert env["period_mode"]["mode"] == "catch_up"
    assert env["period_mode"]["missed"] == ["2026-09-28", "2026-10-05"]
    assert env["period_mode"]["in_progress"] == "2026-10-12"
    assert not _ids(env) & set(_CONCLUDING)
    archives = [d for d in env["directives"] if d["id"].startswith(wwc_brief.CATCH_UP_ARCHIVE_PREFIX)]
    assert [d["id"] for d in archives] == [
        "d_step13_catch_up_archive_2026-09-28",
        "d_step13_catch_up_archive_2026-10-05",
    ]
    assert archives[0]["cli"] == "workweek-complete-close"
    assert archives[0]["args"][:7] == [
        "archive", "--week-only", "--week-starting", "2026-09-28",
        "--week-ending", "2026-10-04", "--version",
    ]
    assert "--move-priorities" in archives[0]["args"]
    assert "--move-priorities" not in archives[1]["args"]
    assert all("--no-push" in d["args"] for d in archives)
    step1b = next(d for d in env["directives"] if d["id"] == "d_step1b_backfill_changelog_gaps")
    assert step1b["args"] == ["--through", "2026-10-11"]


def test_catch_up_performs_no_release_act(monkeypatch, tmp_path: Path) -> None:
    _, env = _brief_at(monkeypatch, tmp_path, "2026-09-28", datetime(2026, 10, 14, 10, 0), catch_up=True)
    for d in env["directives"]:
        assert d["cli"] != "gh"
        assert "release" not in d["id"] and "semver" not in d["id"]
        assert "--no-push" in d["args"] or d["cli"] != "workweek-complete-close"


def test_catch_up_narration_leads_and_names_conclude_when_clock_chose(
    monkeypatch, tmp_path: Path
) -> None:
    _, env = _brief_at(monkeypatch, tmp_path, "2026-09-28", datetime(2026, 10, 14, 10, 0))
    assert env["period_mode"]["source"] == "auto-clock"
    assert env["narration"].startswith(
        "Catch-up run: 2026-09-28, 2026-10-05 backfilled; 2026-10-12 left open."
    )
    assert "--conclude" in env["narration"]


def test_explicit_catch_up_narration_does_not_push_conclude(monkeypatch, tmp_path: Path) -> None:
    _, env = _brief_at(monkeypatch, tmp_path, "2026-09-28", datetime(2026, 10, 16, 10, 0), catch_up=True)
    assert env["period_mode"]["source"] == "explicit"
    assert env["narration"].startswith("Catch-up run: ")
    assert "--conclude" not in env["narration"]


def test_late_week_without_flags_concludes_with_the_full_set(monkeypatch, tmp_path: Path) -> None:
    _, env = _brief_at(monkeypatch, tmp_path, "2026-10-12", datetime(2026, 10, 16, 10, 0))
    assert env["period_mode"]["mode"] == "conclude"
    assert "d_step13_archive_close" in _ids(env)
    assert not any(i.startswith(wwc_brief.CATCH_UP_ARCHIVE_PREFIX) for i in _ids(env))
    step1b = next(d for d in env["directives"] if d["id"] == "d_step1b_backfill_changelog_gaps")
    assert step1b["args"] == []


def test_explicit_conclude_early_in_the_week_keeps_the_concluding_ids(
    monkeypatch, tmp_path: Path
) -> None:
    _, env = _brief_at(
        monkeypatch, tmp_path, "2026-10-12", datetime(2026, 10, 13, 9, 0), conclude=True
    )
    assert env["period_mode"]["mode"] == "conclude"
    assert "d_step13_archive_close" in _ids(env)


def test_already_archived_week_is_not_missed(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "archive" / "week-changelogs" / "2026-09-28").mkdir(parents=True)
    _, env = _brief_at(monkeypatch, tmp_path, "2026-09-28", datetime(2026, 10, 14, 10, 0), catch_up=True)
    assert env["period_mode"]["missed"] == ["2026-10-05"]
    step1b = next(d for d in env["directives"] if d["id"] == "d_step1b_backfill_changelog_gaps")
    assert step1b["args"] == ["--through", "2026-10-11"]


def test_header_without_a_dated_anchor_keeps_the_concluding_ceremony(
    monkeypatch, tmp_path: Path
) -> None:
    _, env = _brief_at(monkeypatch, tmp_path, None, datetime(2026, 10, 13, 9, 0))
    assert env["period_mode"]["mode"] == "conclude"
    assert "d_step13_archive_close" in _ids(env)


@pytest.fixture(autouse=True)
def _no_git_scan(monkeypatch) -> None:
    from coordinator_core.ops.workday_complete_backfill_scan import GapScan

    monkeypatch.setattr(wwc_brief.backfill_scan, "scan", lambda root, *a, **k: GapScan())


def _write_dailies(tmp_path: Path, *names: str) -> None:
    week = tmp_path / "state" / "week-changelog"
    week.mkdir(parents=True, exist_ok=True)
    for name in names:
        (week / name).write_text("x", encoding="utf-8")


def test_unset_anchor_catch_up_lists_every_unarchived_completed_week(
    monkeypatch, tmp_path: Path
) -> None:
    _write_dailies(tmp_path, "2026-09-29-h.md", "2026-10-07-h.md", "2026-10-14-h.md")
    (tmp_path / "state" / "week-changelog" / "HEADER.md").write_text(
        "**Week starting:** (not yet set)\n", encoding="utf-8"
    )
    code, env = _brief_at(monkeypatch, tmp_path, None, datetime(2026, 10, 15, 10, 0), catch_up=True)
    assert code == 0
    assert env["period_mode"]["mode"] == "catch_up"
    assert env["period_mode"]["missed"] == ["2026-09-28", "2026-10-05"]
    assert env["period_mode"]["in_progress"] == "2026-10-12"
    archives = [d["id"] for d in env["directives"] if d["id"].startswith(wwc_brief.CATCH_UP_ARCHIVE_PREFIX)]
    assert archives == [
        "d_step13_catch_up_archive_2026-09-28",
        "d_step13_catch_up_archive_2026-10-05",
    ]


def test_unset_anchor_without_flags_keeps_concluding(monkeypatch, tmp_path: Path) -> None:
    _write_dailies(tmp_path, "2026-09-29-h.md")
    _, env = _brief_at(monkeypatch, tmp_path, None, datetime(2026, 10, 15, 10, 0))
    assert env["period_mode"]["mode"] == "conclude"
    assert "d_step13_archive_close" in _ids(env)


def test_unset_anchor_catch_up_skips_archived_weeks(monkeypatch, tmp_path: Path) -> None:
    _write_dailies(tmp_path, "2026-09-29-h.md", "2026-10-07-h.md")
    (tmp_path / "archive" / "week-changelogs" / "2026-09-29").mkdir(parents=True)
    _, env = _brief_at(monkeypatch, tmp_path, None, datetime(2026, 10, 15, 10, 0), catch_up=True)
    assert env["period_mode"]["missed"] == ["2026-10-06"]


@pytest.mark.parametrize("mode", [{}, {"catch_up": True}, {"conclude": True}])
def test_every_directive_carries_a_nonempty_detail(monkeypatch, tmp_path: Path, mode) -> None:
    _, env = _brief_at(monkeypatch, tmp_path, "2026-09-28", datetime(2026, 10, 14, 10, 0), **mode)
    assert env["directives"]
    for d in env["directives"]:
        assert isinstance(d["detail"], str) and d["detail"], d["id"]


def test_catch_up_archive_detail_names_the_week_and_the_invocation_detail_names_the_cli() -> None:
    d = wwc_brief._catch_up_archive_directive("2026-09-28", move_priorities=False)
    assert "2026-09-28" in d["detail"]
    assert wwc_brief._directive("x", cli="list-week-changelog", args=["a"])["detail"] == (
        "list-week-changelog a"
    )


def test_catch_up_lead_reports_the_daily_gap_count(monkeypatch, tmp_path: Path) -> None:
    from coordinator_core.ops.workday_complete_backfill_scan import GapRow, GapScan

    rows = [GapRow("2026-10-01", 2, "a", "b"), GapRow("2026-10-02", 1, "c", "d")]
    monkeypatch.setattr(
        wwc_brief.backfill_scan, "scan", lambda root, *a, **k: GapScan(rows, ["2026-08-01"])
    )
    _, env = _brief_at(monkeypatch, tmp_path, "2026-09-28", datetime(2026, 10, 14, 10, 0), catch_up=True)
    assert "Daily backfill-scan gaps: 2 day(s) plus 1 older." in env["narration"]
    _, concluding = _brief_at(
        monkeypatch, tmp_path, "2026-10-12", datetime(2026, 10, 16, 10, 0)
    )
    assert "backfill-scan gaps" not in concluding["narration"]


def test_catch_up_step_cost_row_exists() -> None:
    import json

    costs = json.loads((Path(wwc_brief.__file__).parent / "step_costs.json").read_text(encoding="utf-8"))
    assert wwc_brief.CATCH_UP_ARCHIVE_PREFIX in costs["steps"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--catch-up", "--conclude"],
        ["--catch-up", "--only"],
        ["--conclude", "--only"],
        ["--catch-up", "--for-date", "2026-10-13"],
    ],
)
def test_brief_main_refuses_forbidden_flag_combinations(argv: list[str], capsys) -> None:
    assert wwc_brief.main(argv) != 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("argv", [["--catch-up", "--conclude"]])
def test_apply_main_refuses_forbidden_flag_combinations(argv: list[str]) -> None:
    from coordinator_core.workweek_complete import apply as wwc_apply

    assert wwc_apply.main(argv) != 0


def test_brief_emits_no_doc_staleness_id_and_never_enters_the_op(monkeypatch) -> None:
    import sys

    from coordinator_core.workweek_complete.brief_inputs import BriefInputs

    for name in [m for m in sys.modules if m.endswith("ops.doc_staleness")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(wwc_brief, "compute_brief_inputs", lambda *a, **k: BriefInputs())
    monkeypatch.setattr(wwc_brief, "resolve_operator_config", lambda env=None: None)
    exit_code, envelope = wwc_brief.brief()
    assert exit_code == 0
    ids = [d["id"] for d in envelope["directives"]] + [
        p["id"] for p in envelope["judgment_points"]
    ]
    assert not any("doc_staleness" in i for i in ids)
    assert not any(m.endswith("ops.doc_staleness") for m in sys.modules)
