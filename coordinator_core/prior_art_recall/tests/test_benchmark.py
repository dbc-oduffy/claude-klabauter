from __future__ import annotations

from pathlib import Path

from coordinator_core.prior_art_recall import benchmark as b
from coordinator_core.prior_art_recall.tests.test_search_log import make_log, render


def test_fixtures_cover_each_kind_once_and_plants_exist():
    assert b.self_check(b.load_cases()) == []


def test_plans_never_name_the_planted_file():
    for c in b.load_cases():
        assert Path(c.planted_file).stem not in c.plan_text


def test_workdir_has_base_plant_and_plan(tmp_path):
    c = b.load_cases({"lesson-backup-window"})[0]
    b.build_workdir(c, tmp_path / "w")
    assert (tmp_path / "w" / "state/lessons/2026-10-02-compaction-collided-with-snapshot.yaml").is_file()
    assert (tmp_path / "w" / "docs/wiki/naming-conventions.md").is_file()
    assert (tmp_path / "w" / "plan.md").is_file()


def _sidecar(section: str, cited: str) -> str:
    head = "### Conflicts" if section == "conflicts" else "### Compatible-but-relevant"
    return f"{head}\n- {cited}\n\n### Silent areas\nnone\n\n" + render(make_log(1))


def test_score_requires_conflict_section_citation():
    c = b.load_cases({"decision-flat-file-queue"})[0]
    cited = "docs/decisions/DR-007-queue-is-flat-files.md"
    assert b.score_sidecar(c, _sidecar("conflicts", cited))["caught"] is True
    assert b.score_sidecar(c, _sidecar("compatible", cited))["caught"] is False
    assert b.score_sidecar(c, "")["caught"] is False


def _perfect(model, work, sidecar, brief):
    plan = (work / "plan.md").read_text(encoding="utf-8")
    case = next(c for c in b.load_cases() if c.plan_text == plan)
    sidecar.write_text(_sidecar("conflicts", case.planted_file), encoding="utf-8")


def _blind(model, work, sidecar, brief):
    sidecar.write_text("### Conflicts\nNo conflicts found.\n", encoding="utf-8")


def test_runner_reports_rates_per_model(tmp_path):
    def by_model(model, work, sidecar, brief):
        (_perfect if model == "sonnet" else _blind)(model, work, sidecar, brief)

    cases = b.load_cases()
    report = b.run_benchmark(["sonnet", "haiku"], 2, by_model, tmp_path, cases)
    assert report["models"]["sonnet"]["catch_rate"] == 1.0
    assert report["models"]["sonnet"]["runs"] == len(cases) * 2
    assert report["models"]["haiku"]["catch_rate"] == 0.0
    assert set(report["models"]["sonnet"]["per_kind"]) == set(b.KINDS)
    assert (tmp_path / "report.json").is_file()
    text = b.format_report(report)
    assert "sonnet: catch_rate=1.0000" in text and "haiku minus sonnet: -1.0000" in text


def test_crashed_run_is_a_recorded_miss(tmp_path):
    def boom(model, work, sidecar, brief):
        raise RuntimeError("cli died")

    report = b.run_benchmark(["m"], 1, boom, tmp_path, b.load_cases({"wiki-retry-cap"}))
    row = report["models"]["m"]["rows"][0]
    assert row["caught"] is False and "cli died" in row["error"]


def test_log_valid_rate_tracks_validator(tmp_path):
    report = b.run_benchmark(["m"], 1, _blind, tmp_path, b.load_cases({"wiki-retry-cap"}))
    assert report["models"]["m"]["log_valid_rate"] == 0.0
