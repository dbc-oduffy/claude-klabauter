"""
coordinator_core.ops.tests.test_handoff_transition_unclaim — C3: the unclaim
backstop mirrors the cascade instead of diverging from it.

Purpose: proves `handoff_transition._find_implemented_governing_plan`'s
belt-and-braces refusal is now kind-aware the SAME way `deliverable_cascade`'s
leg (d) is (AC4) — a `kind: spinoff` handoff whose `deliverable_id` matches an
implemented plan is a legacy inherited id, and its unclaim proceeds rather
than being refused — and that the exclusion set is sourced from ONE shared
kind-membership constant, not a second hand-rolled literal (AC5).

Spec backlink: docs/plans/2026-08-18-a-spinoff-is-not-its-parents-deliverable.md § C3

Negative-spec: does NOT resolve the exemption through `origin_plan_id`/
`origin_handoff` (see the plan's Anti-scope and C3 body — 95% of spinoffs
carry neither, and a resolver keyed on an edge most spinoffs don't have is
not a fix). Does NOT touch `deliverable_carry.py` (spinoffs never reach it).
Does NOT re-test unclaim's pre-existing idempotency/park_note/reaped_from
behaviour beyond what's needed to prove the kind-aware exemption composes
correctly with the pre-existing governing-plan refusal — that refusal's own
non-spinoff behaviour is covered by the regression case below.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_handoff_transition_unclaim.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.handoff_transition as ht
from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: this file spawns a real `git` process because
# locked_rmw (the write path _unclaim routes through) resolves the git
# common dir via a real `git rev-parse` call — no fixture stands in for
# that. Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_handler = ht._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_TEST_SID = "22222222-2222-2222-2222-222222222222"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _seed_handoff(
    repo: Path,
    name: str,
    *,
    deliverable_id: str,
    kind: Optional[str] = None,
    deployment_state: str = "in_flight",
    status: str = "claimed",
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
        f"claimed_at: 2026-01-01T00:00:00Z\n"
        f'claimed_by: "{_TEST_SID}"\n'
        f"deliverable_id: {deliverable_id}\n"
    )
    if kind is not None:
        fm += f"kind: {kind}\n"
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _seed_implemented_plan(repo: Path, name: str, *, deliverable_id: str) -> Path:
    path = repo / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Governing Plan {name}"\n'
        "status: implemented\n"
        f"deliverable_id: {deliverable_id}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Plan\n\nBody.\n", encoding="utf-8")
    return path


def _fm_field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


def _unclaim_params(handoff_path: str) -> dict:
    return {"verb": "unclaim", "handoff_path": handoff_path}


# ---------------------------------------------------------------------------
# AC4 — a kind: spinoff whose deliverable_id matches an implemented plan is
# not refused; the unclaim proceeds.
# ---------------------------------------------------------------------------


def test_ac4_spinoff_with_inherited_id_unclaims_despite_implemented_plan(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_implemented_plan(repo, "parent-plan.md", deliverable_id="dlv-parent-plan-000000")
    handoff = _seed_handoff(
        repo,
        "20260101-spinoff.md",
        deliverable_id="dlv-parent-plan-000000",
        kind="spinoff",
    )

    result = _run(_unclaim_params(str(handoff)), repo_root=repo / ".git")

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert _fm_field(handoff, "status") == "open"
    assert _fm_field(handoff, "deployment_state") == "ready_to_fire"


def test_ac4_non_spinoff_with_same_shape_is_still_refused(tmp_path):
    """Regression companion to AC4: an otherwise-identical non-spinoff
    handoff whose deliverable_id matches an implemented plan must still be
    refused — the exemption is kind-scoped, never a blanket relaxation of
    the belt-and-braces check."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_implemented_plan(repo, "parent-plan.md", deliverable_id="dlv-parent-plan-000000")
    handoff = _seed_handoff(
        repo,
        "20260101-handoff.md",
        deliverable_id="dlv-parent-plan-000000",
        kind=None,
    )

    result = _run(_unclaim_params(str(handoff)), repo_root=repo / ".git")

    assert result["exit_code"] == 1, result
    assert "governing plan" in result["error"] or "implemented" in result["error"]
    # No write occurred — still claimed, unchanged deployment_state.
    assert _fm_field(handoff, "status") == "claimed"
    assert _fm_field(handoff, "deployment_state") == "in_flight"


def test_ac4_spinoff_with_own_distinct_id_is_unaffected(tmp_path):
    """A correctly-minted spinoff (own distinct id, no plan carries it) was
    never refused before this chunk and must not be refused after it either
    — the kind-aware exemption changes only the co-tenant case."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo,
        "20260101-spinoff-own-id.md",
        deliverable_id="dlv-spinoff-own-000000",
        kind="spinoff",
    )

    result = _run(_unclaim_params(str(handoff)), repo_root=repo / ".git")

    assert result["exit_code"] == 0, result
    assert result["applied"] is True


def test_ac4_non_spinoff_kind_with_no_matching_plan_is_unaffected(tmp_path):
    """A non-spinoff handoff whose deliverable_id matches no implemented
    plan unclaims exactly as before this chunk — the new check must never
    introduce a refusal where none existed."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo,
        "20260101-handoff-no-plan.md",
        deliverable_id="dlv-no-plan-000000",
        kind=None,
    )

    result = _run(_unclaim_params(str(handoff)), repo_root=repo / ".git")

    assert result["exit_code"] == 0, result
    assert result["applied"] is True


# ---------------------------------------------------------------------------
# AC5 — shared sourcing: the backstop's exclusion set agrees with the
# cascade's, both ultimately reading baton_class.py's owning table, never a
# second hand-rolled literal.
# ---------------------------------------------------------------------------


def test_ac5_backstop_exclusion_set_matches_cascade_exclusion_set():
    assert ht._SPINOFF_KINDS == cascade_mod._SPINOFF_KINDS


def test_ac5_backstop_exclusion_set_matches_baton_class_owning_table():
    assert ht._SPINOFF_KINDS == frozenset(kind_values_for_canonical("spinoff"))


def test_ac5_backstop_uses_canonical_kind_for_the_membership_test():
    """The membership test in `_unclaim` routes the on-disk `kind` value
    through `canonical_kind` (imported straight from `baton_class.py`)
    before comparing against `_SPINOFF_KINDS` — exact-equality-after-
    canonicalize, mirroring `deliverable_cascade`'s leg (d) mechanism
    exactly (never a slug-prefix match, never a second alias table)."""
    assert ht.canonical_kind("spinoff") in ht._SPINOFF_KINDS
    # A DIFFERENT kind family (roadmap, not spinoff) must not collide.
    assert ht.canonical_kind("roadmap-baton") not in ht._SPINOFF_KINDS
