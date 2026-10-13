"""Pins leg (a): the caller's own session claim clears; a different live claimant still refuses.

Test surface: python -m pytest coordinator_core/ops/tests/test_deliverable_cascade_self_claim.py -q
"""

from __future__ import annotations

import asyncio

import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.handoff_children as handoff_children_mod

_CALLER = "11111111-1111-1111-1111-111111111111"
_OTHER = "22222222-2222-2222-2222-222222222222"
_FM = {"deployment_state": "in_flight", "deliverable_id": "dlv-self-000000", "kind": "handoff"}


async def _no_live_children(*args, **kwargs):
    return {"referenced": False, "children": [], "exit_code": 1}


def _run(tmp_path, monkeypatch, claimant, caller):
    path = tmp_path / "state" / "handoffs" / "h.md"
    monkeypatch.setattr(cascade_mod, "_claimant", lambda *_a, **_k: claimant)
    monkeypatch.setattr(cascade_mod, "resolve_live_session_ids", lambda: {_CALLER, _OTHER})
    monkeypatch.setattr(handoff_children_mod, "has_live_children_from_metas", _no_live_children)
    return asyncio.run(
        cascade_mod._predicate_refusal(
            path, dict(_FM), tmp_path / ".git", corpus_metas={}, caller_session_id=caller
        )
    )


def test_callers_own_live_claim_clears(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, _CALLER, _CALLER) is None


def test_different_live_claimant_still_refuses(tmp_path, monkeypatch):
    reason = _run(tmp_path, monkeypatch, _OTHER, _CALLER)
    assert reason is not None and "claimed by live session" in reason


def test_no_caller_session_keeps_refusing_live_claimant(tmp_path, monkeypatch):
    reason = _run(tmp_path, monkeypatch, _CALLER, "")
    assert reason is not None and "claimed by live session" in reason


def test_plan_cascade_entry_passes_attributable_session_as_caller(tmp_path, monkeypatch):
    """`plan_status_transition._run_cascade` -> the real handler threads the invoking session."""
    import subprocess

    import coordinator_core.session.core as session_core
    from coordinator_core.ops import plan_status_transition as pst
    from coordinator_core.win_portability import no_console_creationflags

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_creationflags())
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    plan = tmp_path / "docs" / "plans" / "2026-10-13-p.md"
    plan.write_text("---\ntitle: P\nstatus: implemented\n---\nBody\n", encoding="utf-8")
    (tmp_path / "state" / "handoffs" / "h.md").write_text(
        "---\ntitle: H\nkind: handoff\ndeployment_state: in_flight\n"
        "deliverable_id: dlv-self-000000\n---\nBody\n", encoding="utf-8",
    )
    seen = []

    async def spy(*args, **kwargs):
        seen.append(kwargs.get("caller_session_id"))
        return "stop here"

    monkeypatch.setattr(session_core, "attributable_session_id", lambda: _CALLER)
    monkeypatch.setattr(cascade_mod, "_predicate_refusal", spy)
    pst._run_cascade(str(plan), "dlv-self-000000")
    assert seen and set(seen) == {_CALLER}
