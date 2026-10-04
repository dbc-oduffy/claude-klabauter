"""wsc brief surfaces a claimed, non-terminal spec-dispatch plan that no governing plan reached."""

from __future__ import annotations

import pytest

import coordinator_core.workstream_complete as wsc
from coordinator_core.ops.ceremony import wsc_disposition
from coordinator_core.session import claimed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_JP = "jp-spec-dispatch-plan-unreconciled"
_REL = "docs/plans/2026-10-01-spec-dispatch-fixture.md"


def _gate() -> wsc.SessionShapeGate:
    return wsc.SessionShapeGate(
        sid="testsid123",
        disposition=wsc_disposition.SINGLE_SESSION,
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
        detection={},
    )


def _plan(tmp_path, *, scope_mode="spec-dispatch", status="draft"):
    path = tmp_path / _REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: fixture\nstatus: {status}\nscope_mode: {scope_mode}\n---\n\n# fixture\n",
        encoding="utf-8",
    )
    return path


def _setup(monkeypatch, tmp_path, **plan_kwargs):
    _plan(tmp_path, **plan_kwargs)
    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: _gate())
    monkeypatch.setattr(
        claimed_plan, "list_held_plan_claims", lambda cwd=None: [(_REL, "2026-10-01T00:00:00Z")]
    )


def _jp_ids(tmp_path, decisions=None):
    return [
        jp["id"]
        for jp in wsc.brief(decisions=decisions or {}, repo_root=tmp_path)["judgment_points"]
    ]


def test_fires_for_a_claimed_non_terminal_spec_dispatch_plan(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    obj = wsc.brief(decisions={}, repo_root=tmp_path)
    points = {jp["id"]: jp for jp in obj["judgment_points"]}
    assert _JP in points
    assert _REL in points[_JP]["question"]
    assert f"archive-stamp-cli stamp-plan-implemented {_REL}" in points[_JP]["reason"]
    assert points[_JP].get("recommendation") is None


def test_silent_when_a_governing_plan_resolves(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    ids = _jp_ids(tmp_path, {"governing_plan_path": _REL})
    assert _JP not in ids


def test_silent_for_a_terminal_plan(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, status="implemented")
    assert _JP not in _jp_ids(tmp_path)


def test_silent_for_another_scope_mode(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, scope_mode="plan")
    assert _JP not in _jp_ids(tmp_path)


def test_silent_without_a_claim(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(claimed_plan, "list_held_plan_claims", lambda cwd=None: [])
    assert _JP not in _jp_ids(tmp_path)
