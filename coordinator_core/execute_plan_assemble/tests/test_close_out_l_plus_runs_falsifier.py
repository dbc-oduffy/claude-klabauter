"""L+ close-out runs the plan's own prime-exit-criterion falsifier (the XL r34 replay).

A recorded `falsifier_verdict: pass` over a failing command must not close
`implemented` at L, XL or XXL; below L the recorded-verdict path is unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
from coordinator_core.execute_plan_assemble.tests.test_close_out_goal_refusal import (
    _asserted_pass_block,
    _asserted_true_verdict_fail_block,
    _init_repo,
    _land_the_shipping_chunk,
    _prime_exit_criterion_block,
    _run_close_out,
    _run_git,
    _seed_goal_plan,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SIZING_REL = "state/sizings/2026-10-25-fixture-l-sizing.yaml"
_FAILING = "Run `python -c \"import sys; sys.exit(1)\"`"
_PASSING = "Run `python -c \"import sys; sys.exit(0)\"`"
_UNPARSEABLE = "Run the thing by hand and look"


def _seed(
    root: Path,
    *,
    how: str,
    exit_criterion_met: str,
    tshirt: Optional[str] = "L",
    sizing_text: Optional[str] = None,
    sizing_ref: Optional[str] = _SIZING_REL,
):
    _init_repo(root)
    shipping_sha = _land_the_shipping_chunk(root)
    if sizing_ref is not None and (tshirt is not None or sizing_text is not None):
        sizing = root / _SIZING_REL
        sizing.parent.mkdir(parents=True, exist_ok=True)
        sizing.write_text(
            sizing_text if sizing_text is not None else f"estimate:\n  tshirt: {tshirt}\n",
            encoding="utf-8",
        )
        _run_git(["add", _SIZING_REL], root)
    block = _prime_exit_criterion_block(
        baseline_ref=shipping_sha, exit_criterion_met=exit_criterion_met
    ).replace("    how: read the fixture surface", f"    how: '{how}'")
    goal_fm = block + (f"sizing_object: {sizing_ref}\n" if sizing_ref is not None else "")
    _seed_goal_plan(root, goal_frontmatter=goal_fm, shipping_sha=shipping_sha)
    return root


class TestLPlusRunsTheFalsifier:
    def test_failing_command_with_recorded_pass_does_not_close_implemented(
        self, tmp_path, monkeypatch
    ):
        _seed(tmp_path, how=_FAILING, exit_criterion_met=_asserted_pass_block())

        code, result = _run_close_out(monkeypatch, tmp_path, "plan.md")

        assert code == coas.EXIT_OK, result
        assert result["status_target"] != "implemented"
        assert result["status_target"] is None
        assert result["stamped"] is False
        assert result["falsifier_gate"]["passed"] is False
        assert result["falsifier_gate"]["exit_code"] == 1
        assert result["partial_evaluation_stamped"] is True
        plan_text = (tmp_path / "plan.md").read_text(encoding="utf-8")
        assert "close_out_last_partial:" in plan_text
        assert "falsifier did not pass" in plan_text
        assert "status: executing" in plan_text

    @pytest.mark.parametrize("tshirt", ["L", "XL", "XXL"])
    def test_passing_command_with_recorded_pass_closes_implemented(
        self, tmp_path, monkeypatch, tshirt
    ):
        _seed(tmp_path, how=_PASSING, exit_criterion_met=_asserted_pass_block(), tshirt=tshirt)

        code, result = _run_close_out(monkeypatch, tmp_path, "plan.md")

        assert code == coas.EXIT_OK, result
        assert result["status_target"] == "implemented"
        assert result["stamped"] is True
        assert result["falsifier_gate"]["passed"] is True

    def test_recorded_fail_is_still_refused(self, tmp_path, monkeypatch):
        _seed(
            tmp_path,
            how=_PASSING,
            exit_criterion_met=_asserted_true_verdict_fail_block(),
        )

        code, result = _run_close_out(monkeypatch, tmp_path, "plan.md", dry_run=True)

        assert code == coas.EXIT_OK, result
        assert result["status_target"] is None
        assert "falsifier_gate" not in result

    def test_m_plan_keeps_the_recorded_verdict_path(self, tmp_path, monkeypatch):
        _seed(tmp_path, how=_FAILING, exit_criterion_met=_asserted_pass_block(), tshirt="M")

        code, result = _run_close_out(monkeypatch, tmp_path, "plan.md")

        assert code == coas.EXIT_OK, result
        assert result["status_target"] == "implemented"
        assert "falsifier_gate" not in result

    def test_plan_without_sizing_object_keeps_the_recorded_verdict_path(
        self, tmp_path, monkeypatch
    ):
        _seed(
            tmp_path,
            how=_FAILING,
            exit_criterion_met=_asserted_pass_block(),
            tshirt=None,
            sizing_ref=None,
        )

        code, result = _run_close_out(monkeypatch, tmp_path, "plan.md", dry_run=True)

        assert result["status_target"] == "implemented"
        assert "falsifier_gate" not in result

    def test_unparseable_how_is_indeterminate_not_implemented(self, tmp_path, monkeypatch):
        _seed(tmp_path, how=_UNPARSEABLE, exit_criterion_met=_asserted_pass_block())

        code, result = _run_close_out(monkeypatch, tmp_path, "plan.md", dry_run=True)

        assert code == coas.EXIT_OK, result
        assert result["status_target"] is None
        assert result["falsifier_gate"]["ran"] is False
        assert result["falsifier_gate"]["passed"] is False

    @pytest.mark.parametrize(
        "kind", ["missing-file", "malformed-yaml", "no-tshirt"]
    )
    def test_unreadable_sizing_still_runs_the_falsifier(self, tmp_path, monkeypatch, kind):
        if kind == "missing-file":
            _seed(
                tmp_path,
                how=_FAILING,
                exit_criterion_met=_asserted_pass_block(),
                tshirt=None,
            )
        else:
            text = "estimate: [unclosed\n" if kind == "malformed-yaml" else "estimate:\n  note: x\n"
            _seed(
                tmp_path,
                how=_FAILING,
                exit_criterion_met=_asserted_pass_block(),
                tshirt=None,
                sizing_text=text,
            )

        code, result = _run_close_out(monkeypatch, tmp_path, "plan.md", dry_run=True)

        assert code == coas.EXIT_OK, result
        assert result["status_target"] is None
        assert result["falsifier_gate"]["passed"] is False

    def test_dry_run_gives_the_same_status_target_as_the_live_path(self, tmp_path, monkeypatch):
        _seed(tmp_path, how=_FAILING, exit_criterion_met=_asserted_pass_block())
        _, dry = _run_close_out(monkeypatch, tmp_path, "plan.md", dry_run=True)
        plan_after_dry = (tmp_path / "plan.md").read_text(encoding="utf-8")
        _, live = _run_close_out(monkeypatch, tmp_path, "plan.md")

        assert dry["status_target"] == live["status_target"] is None
        assert dry["falsifier_gate"]["passed"] is live["falsifier_gate"]["passed"] is False
        assert "close_out_last_partial:" not in plan_after_dry


class TestSizeReader:
    @pytest.mark.parametrize(
        ("tshirt", "expected"),
        [("XS", False), ("S", False), ("M", False), ("L", True), ("xl", True), ("XXL", True)],
    )
    def test_tshirt_floor_is_l(self, tmp_path, tshirt, expected):
        sizing = tmp_path / "s.yaml"
        sizing.write_text(f"estimate:\n  tshirt: {tshirt}\n", encoding="utf-8")
        assert coas._plan_is_l_plus_fail_closed({"sizing_object": "s.yaml"}, tmp_path) is expected

    def test_absent_or_null_sizing_object_is_below_l(self, tmp_path):
        assert coas._plan_is_l_plus_fail_closed({}, tmp_path) is False
        assert coas._plan_is_l_plus_fail_closed({"sizing_object": None}, tmp_path) is False
