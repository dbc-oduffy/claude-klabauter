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


def test_verified_ledger_stamp_on_kira_sidecar_satisfies_routing():
    """RRI-M4: a verified `findings_ledger` on Kira's OWN sidecar routes the
    verdict directly, with no separate integrator sidecar required."""
    assert _kira_has_verified_ledger(
        {"findings_ledger": "{rows: 3, applied: 3, em_rejected: 0, suspended: 0, verified_at: 2026-09-26T00:00:00Z}"}
    )
    assert not _kira_has_verified_ledger({})
    assert not _kira_has_verified_ledger({"findings_ledger": ""})
    assert not _kira_has_verified_ledger({"findings_ledger": []})
