"""candidate_paths lists the plan file the planner wrote, whether the record names it doubled or not."""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import run_artifacts

RUN = "warp-2026-10-29-x-aaaaaa-20261029T000000-bbbbbb"
SIZING = "state/sizings/2026-10-29-x-aaaaaa.yaml"
SINGLE = "docs/plans/2026-10-29-x-aaaaaa.md"
DOUBLED = "docs/plans/2026-10-29-2026-10-29-x-aaaaaa.md"


def _write(root: Path, rel: str, text: str = "x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _plans(tmp_path: Path, **kw) -> list[str]:
    arts = run_artifacts.candidate_paths(tmp_path, RUN, sizing_rel=SIZING, **kw)
    return list(arts.by_class.get("plan", ()))


def test_doubled_plan_rel_resolves_to_the_single_date_file_the_planner_wrote(tmp_path):
    _write(tmp_path, SIZING)
    _write(tmp_path, SINGLE)
    assert _plans(tmp_path, plan_rel=DOUBLED) == [SINGLE]


def test_doubled_plan_edge_resolves_to_the_single_date_file(tmp_path):
    _write(tmp_path, SIZING, f"plan: {DOUBLED}\n")
    _write(tmp_path, SINGLE)
    assert _plans(tmp_path) == [SINGLE]


def test_unnamed_plan_falls_back_to_the_sizing_resolver(tmp_path):
    _write(tmp_path, SIZING)
    _write(tmp_path, SINGLE)
    assert _plans(tmp_path) == [SINGLE]


def test_existing_doubled_file_still_resolves_as_named(tmp_path):
    _write(tmp_path, SIZING)
    _write(tmp_path, DOUBLED)
    assert _plans(tmp_path, plan_rel=DOUBLED) == [DOUBLED]


def test_named_plan_absent_everywhere_lists_nothing(tmp_path):
    _write(tmp_path, SIZING)
    assert _plans(tmp_path, plan_rel=DOUBLED) == []
