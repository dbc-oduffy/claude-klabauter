"""
coordinator_core.ops.fleet.tests.test_plan_handoffs — Tests for
fleet.handoffs_for_plan op.

Coverage:
  (a) N handoffs (live) sharing a plan id are ALL returned.
  (b) Live AND archived handoffs for the same plan id are both returned,
      each tagged live=True/False respectively.
  (c) A plan id with zero matching handoffs → empty candidates[], exit_code:0
      (not an error).
  (d) Handoffs missing origin_plan_id entirely are excluded, not a crash.
  (e) Required/extra candidate fields (status, deployment_state, shipped_in,
      claimed_by, live) round-trip from frontmatter.
  (f) dry_run:false / missing plan_id / missing repo_root → setup-error
      envelope (exit_code:1), mirroring archive_plans's setup-error shape.

In-process dispatch pattern mirrors test_archive_plans.py: direct handler
call via asyncio.run(), no pytest-asyncio, no _RegistryScope stub needed for
fleet ops.

Import guard (lesson state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml):
  coordinator_core.ops.fleet is imported at module top so @register_op fires
  before any registry assertion.

Spec backlink: cross-repo memo from claude-central-em, 2026-07-26 — the
  B-series/G-series repair gap this op exists to prevent a recurrence of.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# Import guard: must fire @register_op for fleet ops before any registry assertion.
import coordinator_core.ops.fleet  # noqa: F401 — side-effect: registers fleet.* ops
import coordinator_core.ops.fleet.plan_handoffs  # noqa: F401 — ensure the handler is registered

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.plan_handoffs import _handoffs_for_plan


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_params(plan_id: str | None = "pln-example-c231e9", dry_run: bool = True) -> dict:
    params: dict = {"dry_run": dry_run}
    if plan_id is not None:
        params["plan_id"] = plan_id
    return params


def _seed_archived_handoff(fleet_repo, month: str, name: str, extra_frontmatter: str = "") -> Path:
    """Write and commit a handoff directly under archive/handoffs/<month>/.

    Mirrors test_archive_handoffs.py's test_retention_archived_child_does_not_block_archive
    inline pattern (no seed_archived_handoff helper on FleetRepo itself).
    """
    archive_dir = fleet_repo.root / "archive" / "handoffs" / month
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / name
    lines = ['---', 'title: "Archived Handoff"', "status: claimed", "created: 2026-01-01"]
    if extra_frontmatter:
        lines.extend(extra_frontmatter.splitlines())
    lines.append("---")
    path.write_text("\n".join(lines) + "\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", f"add archived handoff {name}")
    return path


# ---------------------------------------------------------------------------
# Import-guard / floor assertion
# ---------------------------------------------------------------------------

def test_op_registered():
    """Import guard: fleet.handoffs_for_plan must be registered before any dispatch test."""
    assert "fleet.handoffs_for_plan" in _REGISTRY, (
        "fleet.handoffs_for_plan must be registered — is "
        "coordinator_core.ops.fleet.plan_handoffs imported at module top?"
    )


# ---------------------------------------------------------------------------
# (a) N live handoffs sharing a plan id are all returned
# ---------------------------------------------------------------------------

def test_all_live_handoffs_for_plan_returned(fleet_repo):
    plan_id = "pln-computed-skills-frontage-roadmap-c231e9"
    fleet_repo.seed_handoff(
        "2026-07-01-baton-g1.md", "open",
        extra_frontmatter=f"origin_plan_id: {plan_id}",
    )
    fleet_repo.seed_handoff(
        "2026-07-02-baton-g2.md", "claimed",
        extra_frontmatter=f"origin_plan_id: {plan_id}",
    )
    # A handoff from a DIFFERENT plan must not be swept in.
    fleet_repo.seed_handoff(
        "2026-07-03-other-plan.md", "open",
        extra_frontmatter="origin_plan_id: pln-unrelated-000000",
    )

    result = _run(_handoffs_for_plan(_make_params(plan_id), repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 0
    ids = {c["id"] for c in result["candidates"]}
    assert ids == {
        "state/handoffs/2026-07-01-baton-g1.md",
        "state/handoffs/2026-07-02-baton-g2.md",
    }
    assert all(c["origin_plan_id"] == plan_id for c in result["candidates"])
    assert all(c["live"] is True for c in result["candidates"])


# ---------------------------------------------------------------------------
# (b) live AND archived handoffs for the same plan id both returned
# ---------------------------------------------------------------------------

def test_live_and_archived_both_returned(fleet_repo):
    plan_id = "pln-baton-relation-abc123"
    fleet_repo.seed_handoff(
        "2026-07-10-b-live.md", "open",
        extra_frontmatter=f"origin_plan_id: {plan_id}",
    )
    _seed_archived_handoff(
        fleet_repo, "2026-06", "2026-06-01-b-archived.md",
        extra_frontmatter=f"origin_plan_id: {plan_id}\ndeployment_state: shipped\nshipped_in: abc1234",
    )

    result = _run(_handoffs_for_plan(_make_params(plan_id), repo_root=fleet_repo.common_dir))

    by_id = {c["id"]: c for c in result["candidates"]}
    assert set(by_id) == {
        "state/handoffs/2026-07-10-b-live.md",
        "archive/handoffs/2026-06/2026-06-01-b-archived.md",
    }
    assert by_id["state/handoffs/2026-07-10-b-live.md"]["live"] is True
    archived = by_id["archive/handoffs/2026-06/2026-06-01-b-archived.md"]
    assert archived["live"] is False
    assert archived["deployment_state"] == "shipped"
    assert archived["shipped_in"] == "abc1234"


# ---------------------------------------------------------------------------
# (c) zero matching handoffs → empty candidates[], not an error
# ---------------------------------------------------------------------------

def test_zero_matches_returns_empty_not_error(fleet_repo):
    fleet_repo.seed_handoff(
        "2026-07-01-unrelated.md", "open",
        extra_frontmatter="origin_plan_id: pln-something-else-000000",
    )

    result = _run(
        _handoffs_for_plan(_make_params("pln-no-such-plan-999999"), repo_root=fleet_repo.common_dir)
    )

    assert result["exit_code"] == 0
    assert result["candidates"] == []


# ---------------------------------------------------------------------------
# (d) handoffs missing origin_plan_id entirely are excluded, not a crash
# ---------------------------------------------------------------------------

def test_handoffs_missing_origin_plan_id_excluded(fleet_repo):
    plan_id = "pln-target-plan-111111"
    fleet_repo.seed_handoff(
        "2026-07-01-no-origin.md", "open",  # no origin_plan_id at all
    )
    fleet_repo.seed_handoff(
        "2026-07-02-has-origin.md", "open",
        extra_frontmatter=f"origin_plan_id: {plan_id}",
    )

    result = _run(_handoffs_for_plan(_make_params(plan_id), repo_root=fleet_repo.common_dir))

    ids = {c["id"] for c in result["candidates"]}
    assert ids == {"state/handoffs/2026-07-02-has-origin.md"}


# ---------------------------------------------------------------------------
# (e) required + extra candidate fields round-trip from frontmatter
# ---------------------------------------------------------------------------

def test_candidate_fields_round_trip(fleet_repo):
    plan_id = "pln-fields-check-222222"
    fleet_repo.seed_handoff(
        "2026-07-01-fields.md", "claimed",
        claimed_by="session-abc123",
        extra_frontmatter=(
            f"origin_plan_id: {plan_id}\n"
            "deployment_state: awaiting_gate\n"
            "gate_dependency: some-subsystem"
        ),
    )

    result = _run(_handoffs_for_plan(_make_params(plan_id), repo_root=fleet_repo.common_dir))

    assert len(result["candidates"]) == 1
    c = result["candidates"][0]
    # Frozen fleet.* candidate keys (contract §2.1).
    for key in ("id", "title", "status", "family", "terminal_since", "note"):
        assert key in c
    assert c["family"] == "handoff"
    assert c["status"] == "claimed"
    # Op-specific detail this op exists to surface.
    assert c["deployment_state"] == "awaiting_gate"
    assert c["claimed_by"] == "session-abc123"
    assert c["origin_plan_id"] == plan_id
    assert c["live"] is True


# ---------------------------------------------------------------------------
# (g) DR-084 regression: legacy consumed_by vocabulary still resolves claimed_by
# ---------------------------------------------------------------------------

def test_claimed_by_resolves_legacy_consumed_by_vocabulary(fleet_repo):
    """query_records() does not normalize claimed_by/consumed_by (it only
    normalizes status/claimed_at), so a handoff recorded under the LEGACY
    consumed_by field must still surface via the DR-084 single accessor
    (claim_holder) rather than silently returning claimed_by: None."""
    plan_id = "pln-legacy-vocab-333333"
    fleet_repo.seed_handoff(
        "2026-07-01-legacy.md", "consumed",
        extra_frontmatter=(
            f"origin_plan_id: {plan_id}\n"
            "consumed_by: session-legacy-xyz"
        ),
    )

    result = _run(_handoffs_for_plan(_make_params(plan_id), repo_root=fleet_repo.common_dir))

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["claimed_by"] == "session-legacy-xyz"


# ---------------------------------------------------------------------------
# (f) setup-error envelope on bad params
# ---------------------------------------------------------------------------

def test_dry_run_false_is_setup_error(fleet_repo):
    result = _run(
        _handoffs_for_plan(_make_params(dry_run=False), repo_root=fleet_repo.common_dir)
    )
    assert result["exit_code"] == 1
    assert result["candidates"] == []


def test_missing_plan_id_is_setup_error(fleet_repo):
    result = _run(_handoffs_for_plan(_make_params(plan_id=None), repo_root=fleet_repo.common_dir))
    assert result["exit_code"] == 1
    assert result["candidates"] == []


def test_blank_plan_id_is_setup_error(fleet_repo):
    result = _run(_handoffs_for_plan(_make_params(plan_id="   "), repo_root=fleet_repo.common_dir))
    assert result["exit_code"] == 1
    assert result["candidates"] == []


def test_missing_repo_root_is_setup_error():
    result = _run(_handoffs_for_plan(_make_params(), repo_root=None))
    assert result["exit_code"] == 1
    assert result["candidates"] == []
