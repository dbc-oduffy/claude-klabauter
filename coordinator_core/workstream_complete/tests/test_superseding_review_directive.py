"""C10: /workstream-complete offers and records a superseding review for a stranded plan."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from coordinator_core.workstream_complete import CONSUMES_MANIFEST, apply as ws_apply
from coordinator_core.workstream_complete import directives_review as dr

_JP = dr.STRANDED_RUN_JUDGMENT_POINT_ID
_RECORD_ID = dr.SUPERSEDING_RECORD_DIRECTIVE_ID
_STAMP_ID = dr.SUPERSEDING_STAMP_DIRECTIVE_ID

_SLICE = {
    "commit_range": {"base": "a" * 40, "head": "b" * 40},
    "wave_sidecar_paths": ["state/wave-1.md"],
    "prep_sidecar": "state/prep.md",
    "stage_returns": {"stage": 1},
}


def _plan(tmp_path: Path, frontmatter: str, *, emitted: bool = True) -> Path:
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(f"---\n{frontmatter}\n---\n\n# Plan\n", encoding="utf-8")
    if emitted:
        (plan.parent / "p.workflow.mjs.emitted.json").write_text("{}", encoding="utf-8")
    return plan


def _wire(plan: Path, decisions: dict) -> tuple[list, list]:
    directives: list = []
    points: list = []
    dr.wire_stranded_run_superseding_review(
        directives,
        points,
        plan_path=plan,
        plan_rel="docs/plans/p.md",
        sid="sid-1",
        repo_root=plan.parents[2],
        decisions=decisions,
    )
    return directives, points


def test_stranded_plan_yields_the_judgment_point_only(tmp_path: Path) -> None:
    directives, points = _wire(_plan(tmp_path, "status: approved"), {})
    assert [p["id"] for p in points] == [_JP]
    assert directives == []
    assert points[0].get("recommendation") is None
    record = next(d for d in points[0]["dispositions"] if d["value"] == "record")
    assert record["resolves"] == []


def test_record_decision_yields_both_directives_in_order_with_token(tmp_path: Path) -> None:
    directives, points = _wire(_plan(tmp_path, "status: approved"), {"superseding_review": _SLICE})
    assert [d["id"] for d in directives] == [_RECORD_ID, _STAMP_ID]
    assert [d["cli"] for d in directives] == ["record-superseding-review", "review-stamp"]
    assert all(c in CONSUMES_MANIFEST for c in ("record-superseding-review", "review-stamp"))
    assert f"{{{_RECORD_ID}.entry_path}}" in directives[1]["args"]
    record = next(d for d in points[0]["dispositions"] if d["value"] == "record")
    assert record["resolves"] == [_RECORD_ID, _STAMP_ID]
    assert all(d["depends_on"] == _JP for d in directives)


@pytest.mark.parametrize(
    "frontmatter, emitted",
    [
        ("status: approved\nreview_stamp:\n  integration_sidecar: x", True),
        ("status: implemented", True),
        ("status: approved", False),
    ],
)
def test_non_stranded_plan_yields_neither(tmp_path: Path, frontmatter: str, emitted: bool) -> None:
    directives, points = _wire(
        _plan(tmp_path, frontmatter, emitted=emitted), {"superseding_review": _SLICE}
    )
    assert directives == [] and points == []


def test_malformed_slice_is_refused() -> None:
    with pytest.raises(ValueError):
        dr.build_superseding_review_directives(
            plan_rel="p.md", sid="s", repo_root=Path("."), decisions={"superseding_review": {}}
        )


def test_apply_threads_the_record_path_into_the_stamp_directive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directives, points = _wire(_plan(tmp_path, "status: approved"), {"superseding_review": _SLICE})
    seen: dict[str, list[str]] = {}

    def _module(name: str, line: str) -> ModuleType:
        mod = ModuleType(name)

        def main(argv: list[str]) -> int:
            seen[name] = list(argv)
            print(line)
            return 0

        mod.main = main
        return mod

    modules = {
        "record-superseding-review": _module("record", "state/superseding-reviews/2026-10/r.md"),
        "review-stamp": _module("stamp", "minted"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli: modules[cli])
    monkeypatch.setattr(ws_apply, "assert_dispatchable", lambda *a, **k: None)
    decisions = {_JP: {"disposition": "record"}}
    _code, report = ws_apply._execute_directives(directives, points, decisions, repo_root=tmp_path)
    assert report["landed"] == [_RECORD_ID, _STAMP_ID]
    assert report["failed"] == []
    assert "state/superseding-reviews/2026-10/r.md" in seen["stamp"]


def test_skip_disposition_dispatches_nothing(tmp_path: Path) -> None:
    directives, points = _wire(_plan(tmp_path, "status: approved"), {"superseding_review": _SLICE})
    _code, report = ws_apply._execute_directives(
        directives, points, {_JP: {"disposition": "skip"}}, repo_root=tmp_path
    )
    assert report["landed"] == [] and report["blocked"] == [_RECORD_ID, _STAMP_ID]
