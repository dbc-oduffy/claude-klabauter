"""Tests for cs_stamp_plan_implemented's open-AC notice.

Spec backlink: state/audits/2026-08-13-implemented-plans-keep-ac-tables-at-open.md
— 17/40 `implemented` plans carry an AC table with rows still `open` and
nothing in the close ceremony ever surfaces that. This is an OFFER, not a
block: the stamp still succeeds (exit code unchanged in every case below);
a stderr notice fires only when there is an open row to report.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path

from coordinator_core import archive_stamp as arstamp


def _seed_plan(tmp_path: Path, table_body: str, status: str = "draft") -> Path:
    path = tmp_path / "plan.md"
    path.write_text(
        f"---\nstatus: {status}\n---\n\nBody.\n\n{table_body}\n",
        encoding="utf-8",
    )
    return path


class TestStampPlanImplementedAcNotice:
    def test_all_open_acs_fires_notice_and_counts_them(self, tmp_path):
        plan = _seed_plan(
            tmp_path,
            "| ID | Criterion | Status |\n"
            "|---|---|---|\n"
            "| AC1 | does a thing | open |\n"
            "| AC2 | does another thing | open |\n",
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_stamp_plan_implemented(str(plan))
        assert rc == 0
        assert "2 AC row" in buf.getvalue()
        assert str(plan) in buf.getvalue()

    def test_all_dispositioned_acs_is_silent(self, tmp_path):
        plan = _seed_plan(
            tmp_path,
            "| ID | Criterion | Status |\n"
            "|---|---|---|\n"
            "| AC1 | does a thing | met |\n"
            "| AC2 | does another thing | shipped |\n",
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_stamp_plan_implemented(str(plan))
        assert rc == 0
        assert "AC row" not in buf.getvalue()

    def test_no_ac_table_is_silent(self, tmp_path):
        plan = _seed_plan(tmp_path, "Just prose, no table here.")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_stamp_plan_implemented(str(plan))
        assert rc == 0
        assert "AC row" not in buf.getvalue()

    def test_mixed_table_counts_only_open_rows(self, tmp_path):
        plan = _seed_plan(
            tmp_path,
            "| ID | Criterion | Status |\n"
            "|---|---|---|\n"
            "| AC1 | does a thing | met |\n"
            "| AC2 | does another thing | open |\n"
            "| AC3 | a third thing | open |\n",
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_stamp_plan_implemented(str(plan))
        assert rc == 0
        assert "2 AC row" in buf.getvalue()

    def test_unparseable_table_is_silent_not_a_false_count(self, tmp_path):
        # A row with a stray `|` inside backticks in the criterion column,
        # but otherwise no line matching the strict `| ACn | ... | status |`
        # shape end-to-end (the closing pipe is missing).
        plan = _seed_plan(
            tmp_path,
            "| ID | Criterion | Status |\n"
            "|---|---|---|\n"
            "| AC1 | uses `a | b` inside backticks, malformed row\n",
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_stamp_plan_implemented(str(plan))
        assert rc == 0
        assert "AC row" not in buf.getvalue()

    def test_pipe_inside_backticks_does_not_break_a_valid_row(self, tmp_path):
        plan = _seed_plan(
            tmp_path,
            "| ID | Criterion | Status |\n"
            "|---|---|---|\n"
            "| AC1 | uses `a | b` inside backticks | open |\n",
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_stamp_plan_implemented(str(plan))
        assert rc == 0
        assert "1 AC row" in buf.getvalue()

    def test_nonexistent_plan_path_still_returns_one_and_is_silent(self, tmp_path):
        missing = tmp_path / "does-not-exist.md"
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_stamp_plan_implemented(str(missing))
        assert rc == 1
        assert "AC row" not in buf.getvalue()
