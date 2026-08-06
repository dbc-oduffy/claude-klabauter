"""
coordinator_core.ops.tests.test_handoff_close_origin_stub

Tests for the "handoff.close_origin_stub" op — Port of:
close-origin-stub-on-ship.sh (example-doctrine-repo 394c8b64, 2026-07-19) + roadmap-baton join-fix.

Import guard: ``import coordinator_core.ops`` MUST precede all test functions so
that ALL op registrations fire (not just the single op under test). This
satisfies the universal-registry-completeness-tests-ov lesson: asserting a
non-empty registry BEFORE per-op assertions prevents a silent false-positive
over an empty registry.

Coverage (mirrors the port proposal's § Test plan C1-C11, plus the
docs/plans/2026-08-04-terminal-state-propagation-join-keys.md chunk-C1
deliverable_id fallback leg):
  (a) registry-completeness — registry is non-empty; op is registered
  C1  — direct-frontmatter join (regression guard), join_source: "direct"
  C1 (join-keys plan) — deliverable_id fallback leg: resolves via handoff,
       via plan, stays additive (does not shadow an already-resolved direct
       pair), and behaves like the other two legs on a genuine no-match.
  C1b (join-keys plan) — closes_stubs merged-plan-authorship leg: resolves
       N origins from one merged plan, absent-by-default parity with
       pre-C1b behaviour, dedup against an already-resolved pair, a
       malformed entry skipped without discarding its sibling, and
       plan-only (a handoff carrying the field contributes nothing).
  C2  — baton-walk join via `predecessor`, join_source: "baton_walk" (headline)
  C3  — baton-walk join via `origin_handoff`
  C4  — multi-hop baton-walk chain (handoff -> predecessor -> predecessor -> baton)
  C5  — plan (direct) + handoff (baton-walk) resolve the SAME pair -> dedup to one
  C6  — ambiguous refuse: two non-terminal stubs match the same pair
  C7  — non-terminal-only filter: an already-shipped matching stub is excluded
  C8  — guard-declined: an UNRELATED live handoff still references the stub
  C8b — a live `kind: spinoff` fork child (predecessor: none, forked_from:
        <baton>) does NOT guard-decline — narrowed edge_kinds regression
  C9  — missing baton / no lineage edge at all -> pairs_resolved: 0, every
        supplied artifact resolves cleanly with no linkage -> QUIET
        (exit_code 0, no_candidates: true), per the 2026-08-04 AC14
        per-closer scoring correction (see this closer's own docstring and
        state/audits/2026-08-04-terminal-state-closer-exit-code-caller-
        audit.md's corrected section).
  C10 — cycle in lineage -> no pair resolved from the cyclic branch, no hang,
        every supplied artifact still resolves cleanly -> QUIET (exit_code 0,
        pairs_resolved: 0, no_candidates: true)
  C11 — missing-both-params usage error -> exit_code: 1
  C2 (join-keys plan) — pairs_resolved=0 discriminator, corrected 2026-08-04:
       EVERY join leg (direct/baton_walk/deliverable_id/closes_stubs) coming
       up empty on artifact(s) that all resolved cleanly, with no partial/
       contradictory linkage, is a genuine zero-candidates negative — QUIET,
       exit_code 0, no_candidates: true (AC14). A named plan_path/
       handoff_path that does NOT resolve to a readable file at all, or an
       artifact carrying partial/contradictory linkage (one of roadmap_id/
       stub_id present without the other, or every closes_stubs entry
       malformed), stays LOUD, exit_code 1, message names the artifact +
       all keys looked for. pairs_resolved>0 with nothing matched (skipped)
       stays a separate quiet exit_code 0, unaffected by this correction.

Fixture approach: HandoffRepo (conftest.py) for state/handoffs/*.md fixtures
(schema-compliant via seed_handoff's baseline fields + `extra` for the
roadmap-graph fields kind=spinoff-roadmap requires); plain Path.write_text for
docs/plans/*.md fixtures (plans are read-only join sources here, never
schema-validated).

Spec backlink: cross-repo/archive/2026-07-14-claude-klabauter-em-wsc-close-origin-stub-join-and-session-shape-pickup-immutability.md
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects, including
# "handoff.close_origin_stub". MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops import handoff_close_origin_stub as _mod
from coordinator_core.ops.handoff_close_origin_stub import _handler
from coordinator_core.ops.fleet._common import handoff_claim_dir
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)

_OP_NAME = "handoff.close_origin_stub"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_close_origin_stub @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Execute a coroutine synchronously (test helper)."""
    return asyncio.run(coro)


def _seed_baton(
    repo,
    name: str,
    *,
    roadmap_id: str,
    stub_id: str,
    deployment_state: str = "ready_to_fire",
    predecessor: str = "none",
    wave: int = 1,
    deliverable_id: Optional[str] = None,
) -> Path:
    """Seed a `kind: spinoff-roadmap` origin-stub baton (schema-compliant).

    kind=spinoff-roadmap requires roadmap_id/stub_id/wave/blocks/blocked_by
    (cross-field rule _cf_spinoff_roadmap_requires_graph) — blocks/blocked_by
    are empty lists here (no other roadmap graph needed for these tests).
    ``deliverable_id`` is an additional optional fixture field (C1): the
    deliverable_id fallback leg reads it off a matched stub, not off
    roadmap_id/stub_id, which the stub always carries regardless.
    created defaults to seed_handoff's 2026-01-01 (pre shipped_in-required
    cutoff — 2026-05-29 — so a bare `ship` with no sha param validates clean).
    """
    lines = [
        "kind: spinoff-roadmap",
        f"roadmap_id: {roadmap_id}",
        f"stub_id: {stub_id}",
    ]
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    lines.append(f"wave: {wave}")
    lines.append("blocks: []")
    lines.append("blocked_by: []")
    extra = "\n".join(lines)
    return repo.seed_handoff(
        name,
        "open",
        deployment_state=deployment_state,
        predecessor=predecessor,
        extra=extra,
    )


def _seed_execution_handoff(
    repo,
    name: str,
    *,
    predecessor: Optional[str] = None,
    origin_handoff: Optional[str] = None,
    roadmap_id: Optional[str] = None,
    stub_id: Optional[str] = None,
    deliverable_id: Optional[str] = None,
) -> Path:
    """Seed a plain execution handoff (kind unset -> session-handoff default).

    predecessor / origin_handoff simulate the two lineage edges a stub-routed
    execution chain may reach the baton through (§2 of the join-fix). When
    roadmap_id/stub_id are supplied they are written directly (direct-join leg).
    deliverable_id (C1), when supplied, is the fallback join leg's own key.
    """
    extra_lines = []
    if origin_handoff is not None:
        extra_lines.append(f"origin_handoff: {origin_handoff}")
    if roadmap_id is not None:
        # roadmap_id is schema-permitted only on kind=spinoff-roadmap — but
        # this op never schema-validates a READ-only join source (only the
        # matched STUB is mutated/validated), so a plain execution handoff
        # carrying roadmap_id/stub_id directly is a legitimate join-source
        # fixture even though it would fail validate_frontmatter if it were
        # ever re-written through handoff.transition.
        extra_lines.append(f"roadmap_id: {roadmap_id}")
    if stub_id is not None:
        extra_lines.append(f"stub_id: {stub_id}")
    if deliverable_id is not None:
        extra_lines.append(f"deliverable_id: {deliverable_id}")
    extra = "\n".join(extra_lines)
    return repo.seed_handoff(
        name,
        "open",
        predecessor=predecessor if predecessor is not None else "none",
        extra=extra,
    )


def _seed_plan(
    repo,
    rel_path: str,
    *,
    roadmap_id: Optional[str] = None,
    stub_id: Optional[str] = None,
    deliverable_id: Optional[str] = None,
    closes_stubs: Optional[list] = None,
) -> Path:
    """Write a minimal docs/plans/*.md fixture carrying join-key frontmatter.

    Plans are read-only join sources (never schema-validated, never mutated)
    and are NOT DAG nodes — walk_forward only knows state/handoffs/ +
    archive/handoffs/, so a plan can only ever contribute a "direct",
    "deliverable_id", or "closes_stubs" pair, never "baton_walk".

    ``closes_stubs`` (C1b), when supplied, is a list of (roadmap_id, stub_id)
    tuples written as the merged-plan-authorship ``closes_stubs:
    [{roadmap_id, stub_id}, ...]`` field.
    """
    path = repo.root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if roadmap_id is not None:
        lines.append(f"roadmap_id: {roadmap_id}")
    if stub_id is not None:
        lines.append(f"stub_id: {stub_id}")
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    if closes_stubs is not None:
        lines.append("closes_stubs:")
        for rid, sid in closes_stubs:
            lines.append(f"  - roadmap_id: {rid}")
            lines.append(f"    stub_id: {sid}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n\n# Plan\n", encoding="utf-8")
    return path


def _deployment_state(repo, name: str) -> str:
    text = repo.read_text(name)
    split = split_frontmatter(text)
    assert split is not None
    return read_fm_field(split.fm_text, "deployment_state")


# ---------------------------------------------------------------------------
# (a) registration
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY, (
        f"{_OP_NAME!r} must be registered; present ops: {sorted(_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# C11 — missing-both-params usage error
# ---------------------------------------------------------------------------


def test_missing_both_params_usage_error(handoff_repo):
    result = _run(_handler({}, repo_root=handoff_repo.common_dir))
    assert result["exit_code"] == 1
    assert result["closed"] == []
    assert result["skipped"] == []
    assert result["pairs_resolved"] == 0
    assert "error" in result


def test_missing_repo_root_usage_error():
    result = _run(_handler({"handoff_path": "state/handoffs/x.md"}, repo_root=None))
    assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# C1 — direct-frontmatter join (regression guard)
# ---------------------------------------------------------------------------


def test_direct_join_from_handoff(handoff_repo):
    _seed_baton(
        handoff_repo,
        "2026-07-10_000000_roadmap-qsub-03.md",
        roadmap_id="qsub-03",
        stub_id="c1",
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_exec.md",
        roadmap_id="qsub-03",
        stub_id="c1",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1
    assert len(result["closed"]) == 1
    entry = result["closed"][0]
    assert entry["join_source"] == "direct"
    assert entry["roadmap_id"] == "qsub-03"
    assert entry["stub_id"] == "c1"
    assert _deployment_state(
        handoff_repo, "2026-07-10_000000_roadmap-qsub-03.md"
    ) == "shipped"


def test_direct_join_from_plan(handoff_repo):
    """Plan-only call resolves exactly as the bash did — no baton-walk leg."""
    _seed_baton(
        handoff_repo,
        "2026-07-10_000000_roadmap-qsub-04.md",
        roadmap_id="qsub-04",
        stub_id="c2",
    )
    plan = _seed_plan(
        handoff_repo, "docs/plans/2026-07-11-some-plan.md",
        roadmap_id="qsub-04", stub_id="c2",
    )

    result = _run(
        _handler({"plan_path": str(plan)}, repo_root=handoff_repo.common_dir)
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1
    assert len(result["closed"]) == 1
    assert result["closed"][0]["join_source"] == "direct"


# ---------------------------------------------------------------------------
# C1 (this chunk) — deliverable_id fallback join leg
# ---------------------------------------------------------------------------


def test_deliverable_id_join_from_handoff(handoff_repo):
    """A handoff carrying ONLY deliverable_id (no roadmap_id/stub_id, no
    lineage edge at all) still resolves — via the origin stub sharing that
    deliverable_id — reporting join_source: "deliverable_id"."""
    baton_name = "2026-07-10_000000_roadmap-qsub-c1a.md"
    _seed_baton(
        handoff_repo,
        baton_name,
        roadmap_id="qsub-c1a",
        stub_id="c1a",
        deliverable_id="dlv-c1a",
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_c1a-exec.md",
        deliverable_id="dlv-c1a",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1
    assert len(result["closed"]) == 1, result
    entry = result["closed"][0]
    assert entry["join_source"] == "deliverable_id"
    assert entry["roadmap_id"] == "qsub-c1a"
    assert entry["stub_id"] == "c1a"
    assert _deployment_state(handoff_repo, baton_name) == "shipped"


def test_deliverable_id_join_from_plan(handoff_repo):
    """A plan carrying ONLY deliverable_id (no roadmap_id/stub_id) resolves
    via the same fallback leg — plans are not DAG nodes, so this proves the
    leg is not baton-walk-only."""
    baton_name = "2026-07-10_000000_roadmap-qsub-c1b.md"
    _seed_baton(
        handoff_repo,
        baton_name,
        roadmap_id="qsub-c1b",
        stub_id="c1b",
        deliverable_id="dlv-c1b",
    )
    plan = _seed_plan(
        handoff_repo,
        "docs/plans/2026-07-11-c1b-plan.md",
        deliverable_id="dlv-c1b",
    )

    result = _run(
        _handler({"plan_path": str(plan)}, repo_root=handoff_repo.common_dir)
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1
    assert len(result["closed"]) == 1, result
    assert result["closed"][0]["join_source"] == "deliverable_id"
    assert _deployment_state(handoff_repo, baton_name) == "shipped"


def test_deliverable_id_leg_does_not_shadow_existing_direct_pair(handoff_repo):
    """When the (roadmap_id, stub_id) pair already resolves via the direct
    leg, the deliverable_id leg contributes nothing new — dedup on pair
    value keeps pairs_resolved at 1 and the primary leg's join_source wins,
    proving the new leg is purely additive fallback, never a replacement."""
    baton_name = "2026-07-10_000000_roadmap-qsub-c1c.md"
    _seed_baton(
        handoff_repo,
        baton_name,
        roadmap_id="qsub-c1c",
        stub_id="c1c",
        deliverable_id="dlv-c1c",
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_c1c-exec.md",
        roadmap_id="qsub-c1c",
        stub_id="c1c",
        deliverable_id="dlv-c1c",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1, result
    assert len(result["closed"]) == 1, result
    assert result["closed"][0]["join_source"] == "direct"


def test_deliverable_id_leg_no_matching_stub_reports_no_match(handoff_repo):
    """A deliverable_id present on the input but matching no origin stub
    resolves the leg to nothing — same trust-boundary posture as the other
    two legs. The supplied handoff resolved cleanly and carries no roadmap-
    origin linkage of any kind (no direct pair, no deliverable_id-joined
    stub in the tree, no closes_stubs) — a genuine zero-candidates negative
    (AC14 correction, 2026-08-04), QUIET exit_code 0, not the loud
    unjoinable-inputs case."""
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_c1d-exec.md",
        deliverable_id="dlv-c1d-unmatched",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["closed"] == []
    assert result["skipped"] == []
    assert result["pairs_resolved"] == 0
    assert result["no_candidates"] is True
    assert "deliverable_id" in result["message"]
    assert "closes_stubs" in result["message"]


# ---------------------------------------------------------------------------
# C1b (this chunk) — closes_stubs merged-plan-authorship join leg
# ---------------------------------------------------------------------------


def test_closes_stubs_resolves_multiple_origins_from_one_merged_plan(handoff_repo):
    """A merged plan naming two pre-existing origin stubs via closes_stubs
    closes BOTH, even though they share no deliverable_id with the plan or
    each other — the case deliverable_id's one-plan-to-N-handoffs leg cannot
    reach (Addendum Q2)."""
    baton_a = "2026-07-10_000000_roadmap-qsub-c1b-a.md"
    baton_b = "2026-07-10_000000_roadmap-qsub-c1b-b.md"
    _seed_baton(handoff_repo, baton_a, roadmap_id="qsub-c1b-a", stub_id="c1b-a")
    _seed_baton(handoff_repo, baton_b, roadmap_id="qsub-c1b-b", stub_id="c1b-b")
    plan = _seed_plan(
        handoff_repo,
        "docs/plans/2026-08-04-c1b-merged-plan.md",
        deliverable_id="dlv-c1b-merged",
        closes_stubs=[("qsub-c1b-a", "c1b-a"), ("qsub-c1b-b", "c1b-b")],
    )

    result = _run(
        _handler({"plan_path": str(plan)}, repo_root=handoff_repo.common_dir)
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 2, result
    assert len(result["closed"]) == 2, result
    join_sources = {entry["join_source"] for entry in result["closed"]}
    assert join_sources == {"closes_stubs"}
    assert _deployment_state(handoff_repo, baton_a) == "shipped"
    assert _deployment_state(handoff_repo, baton_b) == "shipped"


def test_closes_stubs_absent_is_todays_behaviour_exactly(handoff_repo):
    """A plan with no closes_stubs field behaves exactly as before this
    chunk — absence is the documented default, proven here against a plan
    that also carries a directly-resolving pair."""
    baton_name = "2026-07-10_000000_roadmap-qsub-c1b-absent.md"
    _seed_baton(
        handoff_repo, baton_name, roadmap_id="qsub-c1b-absent", stub_id="c1b-absent"
    )
    plan = _seed_plan(
        handoff_repo,
        "docs/plans/2026-08-04-c1b-no-closes-stubs-plan.md",
        roadmap_id="qsub-c1b-absent",
        stub_id="c1b-absent",
    )

    result = _run(
        _handler({"plan_path": str(plan)}, repo_root=handoff_repo.common_dir)
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1, result
    assert result["closed"][0]["join_source"] == "direct"


def test_closes_stubs_dedupes_against_directly_resolved_pair(handoff_repo):
    """A closes_stubs entry that names the SAME pair a direct/deliverable_id
    leg already resolved contributes nothing new — dedup on pair value,
    proving this leg is purely additive fallback, never a second source of
    truth for a pair already resolved."""
    baton_name = "2026-07-10_000000_roadmap-qsub-c1b-dedup.md"
    _seed_baton(
        handoff_repo, baton_name, roadmap_id="qsub-c1b-dedup", stub_id="c1b-dedup"
    )
    plan = _seed_plan(
        handoff_repo,
        "docs/plans/2026-08-04-c1b-dedup-plan.md",
        roadmap_id="qsub-c1b-dedup",
        stub_id="c1b-dedup",
        closes_stubs=[("qsub-c1b-dedup", "c1b-dedup")],
    )

    result = _run(
        _handler({"plan_path": str(plan)}, repo_root=handoff_repo.common_dir)
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1, result
    assert len(result["closed"]) == 1, result
    assert result["closed"][0]["join_source"] == "direct"


def test_closes_stubs_malformed_entry_is_skipped_not_fatal(handoff_repo):
    """A malformed closes_stubs entry (missing stub_id) is skipped rather
    than discarding the whole list — the well-formed sibling entry still
    resolves and closes."""
    baton_name = "2026-07-10_000000_roadmap-qsub-c1b-malformed.md"
    _seed_baton(
        handoff_repo, baton_name, roadmap_id="qsub-c1b-malformed", stub_id="c1b-good"
    )
    path = handoff_repo.root / "docs/plans/2026-08-04-c1b-malformed-plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "closes_stubs:\n"
        "  - roadmap_id: qsub-c1b-malformed\n"
        "    stub_id: c1b-good\n"
        "  - roadmap_id: qsub-c1b-malformed-orphan\n"
        "---\n\n# Plan\n",
        encoding="utf-8",
    )

    result = _run(
        _handler({"plan_path": str(path)}, repo_root=handoff_repo.common_dir)
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1, result
    assert result["closed"][0]["join_source"] == "closes_stubs"
    assert _deployment_state(handoff_repo, baton_name) == "shipped"


def test_closes_stubs_handoff_path_only_contributes_nothing(handoff_repo):
    """closes_stubs is a PLAN-only field (per module docstring): a call with
    only handoff_path never reads it, even if the handoff carries the key,
    since a handoff is not the artifact the field is authored on. The
    supplied handoff carries no linkage at all here, so the outcome is the
    quiet zero-candidates negative (AC14 correction, 2026-08-04), not loud."""
    baton_name = "2026-07-10_000000_roadmap-qsub-c1b-handoff-only.md"
    _seed_baton(
        handoff_repo,
        baton_name,
        roadmap_id="qsub-c1b-handoff-only",
        stub_id="c1b-handoff-only",
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo, "2026-07-11_000000_c1b-handoff-only-exec.md"
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["closed"] == []
    assert result["pairs_resolved"] == 0
    assert result["no_candidates"] is True


# ---------------------------------------------------------------------------
# C2 — baton-walk join via `predecessor` (headline test)
# ---------------------------------------------------------------------------


def test_baton_walk_join_via_predecessor(handoff_repo):
    baton_name = "2026-07-10_000000_roadmap-qsub-05.md"
    _seed_baton(
        handoff_repo, baton_name, roadmap_id="qsub-05", stub_id="c3"
    )
    # Execution handoff has NO direct roadmap_id/stub_id — only a predecessor
    # edge back at the baton (simulates a plain /pickup of the baton).
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_exec.md",
        predecessor=baton_name,
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1
    assert len(result["closed"]) == 1, result
    entry = result["closed"][0]
    assert entry["join_source"] == "baton_walk"
    assert entry["roadmap_id"] == "qsub-05"
    assert entry["stub_id"] == "c3"
    assert _deployment_state(handoff_repo, baton_name) == "shipped"


# ---------------------------------------------------------------------------
# C3 — baton-walk join via `origin_handoff`
# ---------------------------------------------------------------------------


def test_baton_walk_join_via_origin_handoff(handoff_repo):
    baton_name = "2026-07-10_000000_roadmap-qsub-06.md"
    _seed_baton(
        handoff_repo, baton_name, roadmap_id="qsub-06", stub_id="c4"
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_fork-exec.md",
        origin_handoff=baton_name,
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert len(result["closed"]) == 1, result
    assert result["closed"][0]["join_source"] == "baton_walk"
    assert _deployment_state(handoff_repo, baton_name) == "shipped"


# ---------------------------------------------------------------------------
# C4 — multi-hop baton-walk chain
# ---------------------------------------------------------------------------


def test_baton_walk_multi_hop_chain(handoff_repo):
    baton_name = "2026-07-10_000000_roadmap-qsub-07.md"
    _seed_baton(
        handoff_repo, baton_name, roadmap_id="qsub-07", stub_id="c5"
    )
    mid_name = "2026-07-11_000000_mid.md"
    handoff_repo.seed_handoff(mid_name, "consumed", predecessor=baton_name)
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-12_000000_exec.md",
        predecessor=mid_name,
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert len(result["closed"]) == 1, result
    assert result["closed"][0]["join_source"] == "baton_walk"
    assert _deployment_state(handoff_repo, baton_name) == "shipped"


# ---------------------------------------------------------------------------
# C5 — plan (direct) + handoff (baton-walk) resolve the SAME pair -> dedup
# ---------------------------------------------------------------------------


def test_dedup_direct_and_baton_walk_same_pair(handoff_repo):
    baton_name = "2026-07-10_000000_roadmap-qsub-08.md"
    _seed_baton(
        handoff_repo, baton_name, roadmap_id="qsub-08", stub_id="c6"
    )
    plan = _seed_plan(
        handoff_repo, "docs/plans/2026-07-11-dedup-plan.md",
        roadmap_id="qsub-08", stub_id="c6",
    )
    # handoff carries NO direct ids — only reaches the same pair via baton-walk.
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_dedup-exec.md",
        predecessor=baton_name,
    )

    result = _run(
        _handler(
            {"plan_path": str(plan), "handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1, result
    assert len(result["closed"]) == 1, result


# ---------------------------------------------------------------------------
# C6 — ambiguous refuse
# ---------------------------------------------------------------------------


def test_ambiguous_match_refuses_to_stamp(handoff_repo):
    _seed_baton(
        handoff_repo,
        "2026-07-10_000000_roadmap-qsub-09-a.md",
        roadmap_id="qsub-09",
        stub_id="c7",
    )
    _seed_baton(
        handoff_repo,
        "2026-07-10_000001_roadmap-qsub-09-b.md",
        roadmap_id="qsub-09",
        stub_id="c7",
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_ambig-exec.md",
        roadmap_id="qsub-09",
        stub_id="c7",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["closed"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "ambiguous"
    # Neither stub was stamped.
    assert _deployment_state(
        handoff_repo, "2026-07-10_000000_roadmap-qsub-09-a.md"
    ) == "ready_to_fire"
    assert _deployment_state(
        handoff_repo, "2026-07-10_000001_roadmap-qsub-09-b.md"
    ) == "ready_to_fire"


# ---------------------------------------------------------------------------
# C7 — non-terminal-only filter
# ---------------------------------------------------------------------------


def test_already_shipped_stub_excluded_not_zero_match(handoff_repo):
    """A matching stub already deployment_state:shipped is genuinely filtered
    out — not treated as an ambiguous/second candidate.

    (M1, Leg B) UPDATED from the pre-M1 assertion: a stub that matched
    kind+pair but was excluded by the deployment_state gate is no longer
    collapsed into the same 'no-match' reason as a true zero-candidate join
    — it now reports 'no-match-filtered-deployment-state' (with the excluded
    stub's path + deployment_state), per the M1 spec's Leg B. See AC5 below
    for the still-unchanged true zero-match case.
    """
    baton_path = _seed_baton(
        handoff_repo,
        "2026-07-10_000000_roadmap-qsub-10.md",
        roadmap_id="qsub-10",
        stub_id="c8",
        deployment_state="shipped",
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_c7-exec.md",
        roadmap_id="qsub-10",
        stub_id="c8",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["closed"] == []
    assert len(result["skipped"]) == 1
    entry = result["skipped"][0]
    assert entry["reason"] == "no-match-filtered-deployment-state"
    assert len(entry["excluded"]) == 1
    assert entry["excluded"][0]["deployment_state"] == "shipped"
    assert entry["excluded"][0]["stub_path"].endswith(baton_path.name)
    assert entry["excluded"][0]["exclusion_reason"] == "state-not-eligible"


# ---------------------------------------------------------------------------
# C8 — guard-declined (unrelated live handoff still references the stub)
# ---------------------------------------------------------------------------


def test_guard_declined_unrelated_live_child(handoff_repo):
    baton_name = "2026-07-10_000000_roadmap-qsub-11.md"
    _seed_baton(
        handoff_repo, baton_name, roadmap_id="qsub-11", stub_id="c9"
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_c8-exec.md",
        roadmap_id="qsub-11",
        stub_id="c9",
    )
    # An UNRELATED third handoff (not the join source) still points at the
    # baton as ITS predecessor — a genuine live child, must guard-decline.
    handoff_repo.seed_handoff(
        "2026-07-11_500000_unrelated-child.md",
        "open",
        predecessor=baton_name,
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["closed"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "guard-declined"
    assert _deployment_state(handoff_repo, baton_name) == "ready_to_fire"


# ---------------------------------------------------------------------------
# C8b — a live `kind: spinoff` fork child of the stub does NOT guard-decline
# ---------------------------------------------------------------------------


def test_live_forked_from_child_does_not_guard_decline(handoff_repo):
    """Regression for the narrowed edge_kinds (CONCLUSION_EDGE_KINDS): a live
    `kind: spinoff` child that carries `predecessor: none` +
    `forked_from: <baton>` is schema-incapable of being the stub's real
    continuation (`_cf_forked_from_spinoff_only` + `_cf_spinoff_predecessor_
    none`), so it must NOT retain the stub open. Contrast with C8, where an
    UNRELATED live handoff reaches the baton via `predecessor` and correctly
    still guard-declines.
    """
    baton_name = "2026-07-10_000000_roadmap-qsub-11b.md"
    _seed_baton(
        handoff_repo, baton_name, roadmap_id="qsub-11b", stub_id="c9b"
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_c8b-exec.md",
        roadmap_id="qsub-11b",
        stub_id="c9b",
    )
    # A live spinoff DEFLECTED from a session holding the baton — schema-legal
    # only with predecessor: none, and forked_from == origin_handoff (the
    # session's own originating handoff), pointing at the baton.
    handoff_repo.seed_handoff(
        "2026-07-11_500000_tangent-spinoff.md",
        "open",
        predecessor="none",
        extra=f"kind: spinoff\nforked_from: {baton_name}\norigin_handoff: {baton_name}",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["skipped"] == []
    assert len(result["closed"]) == 1
    assert result["closed"][0]["stub_path"].endswith(baton_name)
    assert _deployment_state(handoff_repo, baton_name) == "shipped"


# ---------------------------------------------------------------------------
# C9 — missing baton / no lineage edge at all
# ---------------------------------------------------------------------------


def test_no_pair_in_inputs_is_quiet_zero_candidates(handoff_repo):
    """AC14 correction (2026-08-04): a caller-supplied handoff that resolves
    cleanly but carries no roadmap-origin linkage of any kind — every join
    leg came up empty — is a genuine zero-candidates negative, not a caller
    handing inputs that could not be read. QUIET, exit_code 0,
    no_candidates: true, `message` still names what was inspected and
    looked for so the outcome stays legible."""
    exec_handoff = _seed_execution_handoff(
        handoff_repo, "2026-07-11_000000_lonely.md"
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["closed"] == []
    assert result["skipped"] == []
    assert result["pairs_resolved"] == 0
    assert result["no_candidates"] is True
    assert "lonely" in result["message"]
    assert "roadmap_id" in result["message"] or "(roadmap_id,stub_id)" in result["message"]
    assert "deliverable_id" in result["message"]
    assert "closes_stubs" in result["message"]


def test_unresolvable_handoff_path_is_loud(handoff_repo):
    """A named handoff_path that does NOT resolve to a readable file at all
    (path escapes the allowed roots / does not exist) is "I could not read
    your inputs" — always LOUD, exit_code 1, regardless of the
    zero-candidates correction above. The message names the unresolved
    input."""
    result = _run(
        _handler(
            {"handoff_path": "state/handoffs/does-not-exist.md"},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 1, result
    assert result["closed"] == []
    assert result["skipped"] == []
    assert result["pairs_resolved"] == 0
    assert "no_candidates" not in result
    assert "unresolvable" in result["message"]
    assert "does-not-exist.md" in result["message"]


def test_partial_pair_on_handoff_is_loud(handoff_repo):
    """A handoff carrying only ONE of roadmap_id/stub_id — authored linkage
    that failed to join, not an absence of linkage — stays LOUD, exit_code
    1, distinct from the quiet zero-candidates case."""
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_partial-pair.md",
        roadmap_id="qsub-partial-only",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 1, result
    assert result["closed"] == []
    assert result["skipped"] == []
    assert result["pairs_resolved"] == 0
    assert "no_candidates" not in result
    assert "partial/contradictory linkage" in result["message"]
    assert "partial-pair" in result["message"]


# ---------------------------------------------------------------------------
# C10 — cycle in lineage
# ---------------------------------------------------------------------------


def test_cycle_in_lineage_does_not_hang(handoff_repo):
    """A <-> B predecessor cycle: walk_forward's own cycle detection
    terminates cleanly; no pair resolved from the cyclic branch (neither A
    nor B is a spinoff-roadmap baton), no hang, no crash. The supplied
    handoff itself resolves cleanly and carries no linkage, so this is the
    quiet zero-candidates negative (AC14 correction, 2026-08-04)."""
    a_name = "2026-07-11_000000_cycle-a.md"
    b_name = "2026-07-11_000001_cycle-b.md"
    handoff_repo.seed_handoff(a_name, "open", predecessor=b_name)
    handoff_repo.seed_handoff(b_name, "open", predecessor=a_name)

    result = _run(
        _handler(
            {"handoff_path": handoff_repo.abs_path(a_name)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 0
    assert result["no_candidates"] is True
    assert result["closed"] == []
    assert result["skipped"] == []


# ---------------------------------------------------------------------------
# M1 — in_flight liveness-gated admission (Leg A) + legible skip (Leg B)
#
# Spec backlink: tasks/mise-specs/M1-in-flight-liveness-eligibility.md;
# cross-repo/inbox/2026-08-01-example-cockpit-repo-em-close-origin-stub-skips-in-flight.md
# ---------------------------------------------------------------------------


def test_in_flight_no_claim_dir_is_eligible_ac2(handoff_repo):
    """AC2: an in_flight stub with NO claim dir is eligible and gets closed
    (claim released when the session ends — the orphaned-after-ship case)."""
    baton_name = "2026-07-10_000000_roadmap-qsub-m1a.md"
    _seed_baton(
        handoff_repo,
        baton_name,
        roadmap_id="qsub-m1a",
        stub_id="m1a",
        deployment_state="in_flight",
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_m1a-exec.md",
        roadmap_id="qsub-m1a",
        stub_id="m1a",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["skipped"] == []
    assert len(result["closed"]) == 1, result
    assert _deployment_state(handoff_repo, baton_name) == "shipped"


def test_in_flight_live_holder_not_closed_ac3(handoff_repo):
    """AC3: an in_flight stub whose claim dir exists and whose holder is LIVE
    is NOT closed, and appears in skipped[] with reason
    'no-match-filtered-deployment-state' (not 'no-match')."""
    baton_name = "2026-07-10_000000_roadmap-qsub-m1b.md"
    baton_path = _seed_baton(
        handoff_repo,
        baton_name,
        roadmap_id="qsub-m1b",
        stub_id="m1b",
        deployment_state="in_flight",
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_m1b-exec.md",
        roadmap_id="qsub-m1b",
        stub_id="m1b",
    )
    claim_dir = handoff_claim_dir(handoff_repo.common_dir, baton_path)
    claim_dir.mkdir(parents=True)

    with patch.object(_mod, "cs_claim_holder_live", return_value=True) as mock_live:
        result = _run(
            _handler(
                {"handoff_path": str(exec_handoff)},
                repo_root=handoff_repo.common_dir,
            )
        )
    mock_live.assert_called_once_with(str(claim_dir))

    assert result["exit_code"] == 0, result
    assert result["closed"] == []
    assert len(result["skipped"]) == 1
    entry = result["skipped"][0]
    assert entry["reason"] == "no-match-filtered-deployment-state"
    assert len(entry["excluded"]) == 1
    assert entry["excluded"][0]["deployment_state"] == "in_flight"
    assert entry["excluded"][0]["stub_path"].endswith(baton_name)
    assert entry["excluded"][0]["exclusion_reason"] == "claim-live"
    assert _deployment_state(handoff_repo, baton_name) == "in_flight"


def test_in_flight_liveness_read_raises_fails_closed_ac4(handoff_repo, caplog):
    """AC4: an in_flight stub whose claim-liveness read RAISES is NOT closed
    (fail-closed), and a warning naming the claim dir is logged."""
    baton_name = "2026-07-10_000000_roadmap-qsub-m1c.md"
    baton_path = _seed_baton(
        handoff_repo,
        baton_name,
        roadmap_id="qsub-m1c",
        stub_id="m1c",
        deployment_state="in_flight",
    )
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_m1c-exec.md",
        roadmap_id="qsub-m1c",
        stub_id="m1c",
    )
    claim_dir = handoff_claim_dir(handoff_repo.common_dir, baton_path)
    claim_dir.mkdir(parents=True)

    with patch.object(
        _mod, "cs_claim_holder_live", side_effect=OSError("simulated indeterminate read")
    ):
        with caplog.at_level("WARNING"):
            result = _run(
                _handler(
                    {"handoff_path": str(exec_handoff)},
                    repo_root=handoff_repo.common_dir,
                )
            )

    assert result["exit_code"] == 0, result
    assert result["closed"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "no-match-filtered-deployment-state"
    assert (
        result["skipped"][0]["excluded"][0]["exclusion_reason"]
        == "liveness-read-failed"
    )
    assert _deployment_state(handoff_repo, baton_name) == "in_flight"
    assert any(
        "cs_claim_holder_live raised" in rec.message and str(claim_dir) in rec.message
        for rec in caplog.records
    ), [rec.message for rec in caplog.records]


def test_no_matching_stub_at_all_reports_no_match_ac5(handoff_repo):
    """AC5: a (roadmap_id, stub_id) pair with no matching stub at all still
    reports reason 'no-match' — unchanged (distinct from a matched-but-
    filtered candidate, AC3/C7 above). AC2/AC14: this is the OTHER half of
    the discriminator from C2 — a join key WAS resolved (pairs_resolved=1)
    but nothing matched, a legitimate quiet negative, exit_code stays 0.
    Contrast test_no_pair_in_inputs_is_loud, where no join leg resolves
    anything at all (pairs_resolved=0) and the result is loud."""
    exec_handoff = _seed_execution_handoff(
        handoff_repo,
        "2026-07-11_000000_m1d-exec.md",
        roadmap_id="qsub-m1d",
        stub_id="m1d",
    )

    result = _run(
        _handler(
            {"handoff_path": str(exec_handoff)},
            repo_root=handoff_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["pairs_resolved"] == 1, result
    assert result["closed"] == []
    assert len(result["skipped"]) == 1
    entry = result["skipped"][0]
    assert entry["reason"] == "no-match"
    assert "excluded" not in entry
