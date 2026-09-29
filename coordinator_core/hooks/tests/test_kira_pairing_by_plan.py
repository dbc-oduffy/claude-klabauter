"""coordinator_core/hooks/tests/test_kira_pairing_by_plan.py — P143-T64.

`_kira_unstamped_integrators` previously scoped session-wide: any
integrator sidecar carrying a spawn-time `integrator_receipt` with no
`integrated_from` was counted against EVERY Kira verdict in the session,
regardless of which plan either belonged to. Pairing now keys on the
reviewer (Kira) sidecar's own `plan:` field — an integrator spawned for a
different plan must not be counted against this verdict.
"""

from __future__ import annotations

from coordinator_core.hooks.guard_kira_verdict_routed import (
    _guard_kira_verdict_routed,
    _kira_has_verified_ledger,
    _kira_unstamped_integrators,
)


def test_unstamped_integrators_scoped_to_matching_plan():
    in_scope = [
        ("kira.md", {"agent_type": "coordinator:overengineering-reviewer", "plan": "plan-a"}),
        ("integrator-a.md", {"integrator_receipt": "1", "plan": "plan-a"}),
        ("integrator-b.md", {"integrator_receipt": "1", "plan": "plan-b"}),
    ]

    same_plan = _kira_unstamped_integrators(in_scope, plan="plan-a")
    assert same_plan == ["integrator-a.md"]

    other_plan = _kira_unstamped_integrators(in_scope, plan="plan-b")
    assert other_plan == ["integrator-b.md"]

    unrelated_plan = _kira_unstamped_integrators(in_scope, plan="plan-c")
    assert unrelated_plan == []


def test_unstamped_integrators_excludes_already_stamped():
    in_scope = [
        ("integrator-a.md", {"integrator_receipt": "1", "plan": "plan-a", "integrated_from": "kira"}),
    ]
    assert _kira_unstamped_integrators(in_scope, plan="plan-a") == []


def test_unstamped_integrators_falls_back_to_session_wide_when_plan_unreadable():
    in_scope = [
        ("integrator-a.md", {"integrator_receipt": "1", "plan": "plan-a"}),
        ("integrator-b.md", {"integrator_receipt": "1", "plan": "plan-b"}),
    ]
    assert sorted(_kira_unstamped_integrators(in_scope, plan=None)) == [
        "integrator-a.md",
        "integrator-b.md",
    ]
    assert sorted(_kira_unstamped_integrators(in_scope)) == [
        "integrator-a.md",
        "integrator-b.md",
    ]


def test_guard_passes_when_kira_carries_her_own_verified_ledger(tmp_path):
    """End-to-end twin of DoE's
    test_pass_when_kira_carries_her_own_verified_ledger: reviewers apply
    their own findings in place ("Apply, Then Ledger, Then Verify" --
    coordinator/agents/overengineering-reviewer.md), so a verified
    `findings_ledger` on Kira's own sidecar routes the verdict with no
    sibling code-reviewer sidecar required."""
    (tmp_path / ".git").mkdir()
    session_id = "sess-rri-m4"
    share_dir = tmp_path / ".coordinator-local" / "subagent-share" / session_id
    share_dir.mkdir(parents=True)
    (share_dir / "coordinatoroverengineering-reviewer.a1.md").write_text(
        "---\n"
        "agent_type: coordinator:overengineering-reviewer\n"
        "spawned_at: 2026-09-27T00:00:00Z\n"
        "findings_count: 2\n"
        "rebuild_recommended: false\n"
        "findings_ledger: {rows: 2, applied: 2, em_rejected: 0, suspended: 0}\n"
        "---\n"
        "body\n"
    )

    out = _guard_kira_verdict_routed({"cwd": str(tmp_path), "session_id": session_id})
    assert out == {}


def test_guard_fires_when_integrated_from_names_kira_but_no_rebuild_was_recommended(tmp_path):
    """2026-09-28 PM order: rebuild_recommended is the ONLY route to the EM.
    A sibling sidecar naming Kira's stem in `integrated_from` must NOT
    satisfy routing on its own -- only a verified ledger on Kira's own
    sidecar, or (when her verdict recommended a rebuild) that same
    integrated_from route, ever clears the guard."""
    (tmp_path / ".git").mkdir()
    session_id = "sess-rri-m4-no-rebuild"
    share_dir = tmp_path / ".coordinator-local" / "subagent-share" / session_id
    share_dir.mkdir(parents=True)
    (share_dir / "coordinatoroverengineering-reviewer.a1.md").write_text(
        "---\n"
        "agent_type: coordinator:overengineering-reviewer\n"
        "spawned_at: 2026-09-27T00:00:00Z\n"
        "findings_count: 2\n"
        "rebuild_recommended: false\n"
        "---\n"
        "body\n"
    )
    (share_dir / "executor.b2.md").write_text(
        "---\n"
        "agent_type: coordinator:executor\n"
        "spawned_at: 2026-09-27T00:00:01Z\n"
        "integrated_from: [coordinatoroverengineering-reviewer.a1]\n"
        "---\n"
        "body\n"
    )

    out = _guard_kira_verdict_routed({"cwd": str(tmp_path), "session_id": session_id})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_clears_via_integrated_from_only_when_kira_recommended_rebuild(tmp_path):
    (tmp_path / ".git").mkdir()
    session_id = "sess-rri-m4-rebuild"
    share_dir = tmp_path / ".coordinator-local" / "subagent-share" / session_id
    share_dir.mkdir(parents=True)
    (share_dir / "coordinatoroverengineering-reviewer.a1.md").write_text(
        "---\n"
        "agent_type: coordinator:overengineering-reviewer\n"
        "spawned_at: 2026-09-27T00:00:00Z\n"
        "findings_count: 2\n"
        "rebuild_recommended: true\n"
        "---\n"
        "body\n"
    )
    (share_dir / "executor.b2.md").write_text(
        "---\n"
        "agent_type: coordinator:executor\n"
        "spawned_at: 2026-09-27T00:00:01Z\n"
        "integrated_from: [coordinatoroverengineering-reviewer.a1]\n"
        "---\n"
        "body\n"
    )

    out = _guard_kira_verdict_routed({"cwd": str(tmp_path), "session_id": session_id})
    assert out == {}


def test_verified_ledger_stamp_on_kira_sidecar_satisfies_routing():
    """RRI-M4: a verified `findings_ledger` on Kira's OWN sidecar routes the
    verdict directly, with no separate integrator sidecar required."""
    assert _kira_has_verified_ledger(
        {"findings_ledger": "{rows: 3, applied: 3, em_rejected: 0, suspended: 0, verified_at: 2026-09-26T00:00:00Z}"}
    )
    assert not _kira_has_verified_ledger({})
    assert not _kira_has_verified_ledger({"findings_ledger": ""})
    assert not _kira_has_verified_ledger({"findings_ledger": []})
