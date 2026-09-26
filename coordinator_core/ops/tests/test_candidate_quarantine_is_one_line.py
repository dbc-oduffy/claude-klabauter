
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from coordinator_core.ops.goals_match import _collect_goals
from coordinator_core.ops.handoff_match import _collect_handoffs
from coordinator_core.ops.plan_match import _collect_plans


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def at(self, level: int) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == level]


@pytest.fixture
def capture(monkeypatch):
    handler = _Capture()

    def _attach(logger_name: str):
        log = logging.getLogger(logger_name)
        log.addHandler(handler)
        monkeypatch.setattr(log, "level", logging.DEBUG, raising=False)
        log.setLevel(logging.DEBUG)
        monkeypatch.setattr(log, "propagate", False, raising=False)
        return handler

    handler.attach = _attach  # type: ignore[attr-defined]
    yield handler
    for name in (
        "coordinator_core.ops.plan_match",
        "coordinator_core.ops.goals_match",
        "coordinator_core.ops.handoff_match",
    ):
        logging.getLogger(name).removeHandler(handler)


def _sidecars(plans_dir: Path, count: int) -> None:
    plans_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (plans_dir / f"2026-08-25-plan-{i}.review.md").write_text(
            "---\nreviewer: the Staff Engineer\n---\n\nA review sidecar, not a plan.\n",
            encoding="utf-8",
        )


class TestPlanQuarantineIsOneWarning:
    def test_many_skipped_files_produce_exactly_one_warning(self, tmp_path, capture):
        capture.attach("coordinator_core.ops.plan_match")
        plans = tmp_path / "docs" / "plans"
        _sidecars(plans, 40)
        (plans / "2026-08-25-a-real-plan.md").write_text(
            '---\nplan_id: PID-REAL\ntitle: "A real plan"\n---\n\nBody.\n',
            encoding="utf-8",
        )

        items = _collect_plans(plans)

        assert [i["id"] for i in items] == ["PID-REAL"]
        warnings = capture.at(logging.WARNING)
        assert len(warnings) == 1, warnings
        assert "skipped 40 of 41" in warnings[0]
        assert "missing required field: title (40)" in warnings[0]

    def test_per_file_detail_survives_at_debug(self, tmp_path, capture):
        capture.attach("coordinator_core.ops.plan_match")
        plans = tmp_path / "docs" / "plans"
        _sidecars(plans, 3)

        _collect_plans(plans)

        debug = capture.at(logging.DEBUG)
        assert len(debug) == 3
        assert any("2026-08-25-plan-0.review.md" in line for line in debug)

    def test_the_exception_text_reaches_the_debug_line(self, tmp_path, capture):
        capture.attach("coordinator_core.ops.plan_match")
        plans = tmp_path / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "2026-08-25-broken-yaml.md").write_text(
            "---\ntitle: [unclosed\n---\n\nBody.\n", encoding="utf-8"
        )

        _collect_plans(plans)

        debug = capture.at(logging.DEBUG)
        assert len(debug) == 1
        assert "parse error" in debug[0]
        assert "2026-08-25-broken-yaml.md" in debug[0]
        assert debug[0].rstrip().split("parse error: ", 1)[1].strip() != ""
        assert "parse error (1)" in capture.at(logging.WARNING)[0]

    def test_a_clean_corpus_says_nothing(self, tmp_path, capture):
        capture.attach("coordinator_core.ops.plan_match")
        plans = tmp_path / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "2026-08-25-only-plan.md").write_text(
            '---\nplan_id: PID\ntitle: "Only plan"\n---\n\nBody.\n', encoding="utf-8"
        )

        _collect_plans(plans)

        assert capture.at(logging.WARNING) == []

    def test_reasons_are_bucketed_not_interpolated(self, tmp_path, capture):
        capture.attach("coordinator_core.ops.plan_match")
        plans = tmp_path / "docs" / "plans"
        _sidecars(plans, 2)
        (plans / "INDEX.md").write_text("# Index\n\nNo frontmatter.\n", encoding="utf-8")

        _collect_plans(plans)

        warning = capture.at(logging.WARNING)[0]
        assert "missing required field: title (2)" in warning
        assert "no YAML frontmatter block (1)" in warning


class TestGoalQuarantineIsOneWarning:
    def test_many_skipped_goals_produce_exactly_one_warning(self, tmp_path, capture):
        capture.attach("coordinator_core.ops.goals_match")
        goals = tmp_path / "state" / "goals"
        goals.mkdir(parents=True)
        for i in range(12):
            (goals / f"broken-{i}.yaml").write_text(
                "title: no id here\nstatus: active\n", encoding="utf-8"
            )
        (goals / "good.yaml").write_text(
            "id: goal-good\ntitle: A good goal\nstatus: active\n", encoding="utf-8"
        )

        items = _collect_goals(goals)

        assert [i["id"] for i in items] == ["goal-good"]
        warnings = capture.at(logging.WARNING)
        assert len(warnings) == 1, warnings
        assert "skipped 12 of 13" in warnings[0]
        assert "missing required field: id (12)" in warnings[0]


class TestHandoffQuarantineIsOneWarning:
    def test_many_skipped_handoffs_produce_exactly_one_warning(self, tmp_path, capture):
        capture.attach("coordinator_core.ops.handoff_match")
        handoffs = tmp_path / "state" / "handoffs"
        handoffs.mkdir(parents=True)
        for i in range(7):
            (handoffs / f"2026-08-25-untitled-{i}.md").write_text(
                "---\nstatus: open\n---\n\nBody.\n", encoding="utf-8"
            )
        (handoffs / "2026-08-25-real.md").write_text(
            '---\ntitle: "A real baton"\n---\n\nBody.\n', encoding="utf-8"
        )

        items = _collect_handoffs(handoffs)

        assert [i["title"] for i in items] == ["A real baton"]
        warnings = capture.at(logging.WARNING)
        assert len(warnings) == 1, warnings
        assert "skipped 7 of 8" in warnings[0]
