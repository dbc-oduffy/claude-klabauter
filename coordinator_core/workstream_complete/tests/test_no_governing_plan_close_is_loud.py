"""A close whose governing-plan resolution is `none` surfaces the `no-governing-plan` judgment point.

Pins: the point is emitted only on source `none`, carries no recommendation, and gates no directive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc
from coordinator_core.ops.ceremony import wsc_disposition
from coordinator_core.workstream_complete import judgments

POINT_ID = "no-governing-plan"


def _gate() -> wsc.SessionShapeGate:
    return wsc.SessionShapeGate(
        sid="testsid-no-gov-plan",
        disposition=wsc_disposition.SINGLE_SESSION,
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
        detection={},
    )


def _brief(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, decisions: dict) -> dict:
    gate = _gate()
    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: gate)
    return wsc.brief(decisions=decisions, repo_root=tmp_path)


def _point(brief: dict) -> dict | None:
    return next((jp for jp in brief["judgment_points"] if jp["id"] == POINT_ID), None)


def test_none_source_emits_the_point(monkeypatch, tmp_path):
    brief = _brief(monkeypatch, tmp_path, {})
    assert brief["preflight"]["governing_plan_resolution"]["source"] == "none"
    assert _point(brief) is not None


def test_resolved_plan_does_not_emit_the_point(monkeypatch, tmp_path):
    slug = "a-real-governing-plan"
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{slug}.md").write_text(f"# {slug}\n", encoding="utf-8")
    brief = _brief(monkeypatch, tmp_path, {"governing_plan_slug": slug})
    assert brief["preflight"]["governing_plan_resolution"]["source"] != "none"
    assert _point(brief) is None


def test_no_directive_depends_on_the_point(monkeypatch, tmp_path):
    brief = _brief(monkeypatch, tmp_path, {})
    assert _point(brief) is not None
    for directive in brief["directives"]:
        raw = directive.get("depends_on") or []
        deps = [raw] if isinstance(raw, str) else list(raw)
        flat = [d if isinstance(d, str) else str(d) for d in deps]
        assert not any(POINT_ID in d for d in flat), directive.get("id")


def test_point_has_no_recommendation_and_two_empty_resolving_dispositions(monkeypatch, tmp_path):
    point = _point(_brief(monkeypatch, tmp_path, {}))
    assert point is not None
    assert point["recommendation"] is None
    dispositions = {d["value"]: d for d in point["dispositions"]}
    assert set(dispositions) == {"no-plan-governed", "plan-governed-rerun-with-plan"}
    assert all(d["resolves"] == [] for d in dispositions.values())


def test_builder_roster_length():
    assert len(judgments.JUDGMENT_POINT_BUILDERS) == 29
