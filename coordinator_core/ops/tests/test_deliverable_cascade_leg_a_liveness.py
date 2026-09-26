"""Pins DR-263 leg (a) — live claimant refuses, dead (stale) claimant clears.

Calls `deliverable_cascade._predicate_refusal` directly, the same way
`test_deliverable_cascade.py::test_ac2_predicate_refusal_directly_returns_named_spinoff_reason`
does — no git repo, no `spawns_process` marker. Leg (a)'s two reads
(`_claimant`, `resolve_live_session_ids`) are monkeypatched on the module.
Leg (b) is isolated too: `has_live_children_from_metas` is monkeypatched to
return `{"referenced": False, "children": [], "exit_code": 1}` (exit_code 1 =
"no live children"; see `reap_in_flight_claims.survey`'s `exit_code != 1`
skip) — leg (b) fails closed on an empty/unreadable live set otherwise, and a
real await against the un-mocked resolver would need a live corpus this test
never builds. A non-None `corpus_metas` is passed so `_predicate_refusal`
routes leg (b) through the metas-indexed path this monkeypatch targets.

Spec: docs/plans/2026-09-26-plan-implemented-does-not-close-its-handoff.md (PIDNC-C1)
Test surface: python -m pytest coordinator_core/ops/tests/test_deliverable_cascade_leg_a_liveness.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.handoff_children as handoff_children_mod

_LIVE_SID = "22222222-2222-2222-2222-222222222222"

_FM = {
    "deployment_state": "in_flight",
    "deliverable_id": "dlv-leg-a-000000",
    "kind": "handoff",
}


def _write_handoff(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "handoffs" / "20260101-leg-a.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        'title: "Test Handoff"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: in_flight\n"
        "deliverable_id: dlv-leg-a-000000\n"
        "kind: handoff\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    return path


async def _no_live_children(*args, **kwargs):
    return {"referenced": False, "children": [], "exit_code": 1}


def test_leg_a_refuses_a_claimant_in_the_live_set(tmp_path, monkeypatch):
    handoff = _write_handoff(tmp_path)
    monkeypatch.setattr(cascade_mod, "_claimant", lambda *_a, **_k: _LIVE_SID)
    monkeypatch.setattr(cascade_mod, "resolve_live_session_ids", lambda: {_LIVE_SID})
    monkeypatch.setattr(handoff_children_mod, "has_live_children_from_metas", _no_live_children)

    reason = asyncio.run(
        cascade_mod._predicate_refusal(
            handoff, dict(_FM), tmp_path / ".git", corpus_metas={}
        )
    )

    assert reason is not None
    assert "claimed by live session" in reason


def test_leg_a_clears_a_stale_claimant_not_in_the_live_set(tmp_path, monkeypatch):
    handoff = _write_handoff(tmp_path)
    monkeypatch.setattr(cascade_mod, "_claimant", lambda *_a, **_k: _LIVE_SID)
    monkeypatch.setattr(cascade_mod, "resolve_live_session_ids", lambda: set())
    monkeypatch.setattr(handoff_children_mod, "has_live_children_from_metas", _no_live_children)

    reason = asyncio.run(
        cascade_mod._predicate_refusal(
            handoff, dict(_FM), tmp_path / ".git", corpus_metas={}
        )
    )

    assert reason is None


def test_leg_a_passes_with_no_claimant_and_never_checks_liveness(tmp_path, monkeypatch):
    handoff = _write_handoff(tmp_path)
    monkeypatch.setattr(cascade_mod, "_claimant", lambda *_a, **_k: None)

    calls = []

    def _tracking_live_session_ids():
        calls.append(True)
        return set()

    monkeypatch.setattr(cascade_mod, "resolve_live_session_ids", _tracking_live_session_ids)
    monkeypatch.setattr(handoff_children_mod, "has_live_children_from_metas", _no_live_children)

    reason = asyncio.run(
        cascade_mod._predicate_refusal(
            handoff, dict(_FM), tmp_path / ".git", corpus_metas={}
        )
    )

    assert reason is None
    assert calls == []
