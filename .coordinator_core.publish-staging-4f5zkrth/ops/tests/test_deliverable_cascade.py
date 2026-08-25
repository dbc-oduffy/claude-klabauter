"""
coordinator_core.ops.tests.test_deliverable_cascade — C2: the cascade's
handoff descriptor grows a fourth predicate leg (`kind` policy).

Purpose: proves `deliverable_cascade`'s new leg "d" — a `kind: spinoff`
candidate is never advanced through the `deliverable_id` join, even when its
id exact-matches the query — is wired through the EXISTING
`_predicate_refusal` channel (AC1, AC2), is a byte-identical no-op for the
sizing kind (AC3), and composes correctly with the rest of the cascade in a
three-candidate fixture (AC10: parent ships; a correctly-minted spinoff,
carrying its own distinct id, is untouched; a legacy inherited-id spinoff is
refused by the kind predicate).

Spec backlink: docs/plans/2026-08-18-a-spinoff-is-not-its-parents-deliverable.md § C2

Negative-spec: does NOT touch `deliverable_carry.py` (spinoffs never reach
it — see the plan's Anti-scope) and does NOT resolve anything through
`origin_plan_id`/`origin_handoff`. Does NOT re-test leg (a)/(b)/(c) beyond
what's needed to prove leg (d) composes with them — those are already
covered by `test_deliverable_cascade_claim_state.py` and
`test_deliverable_cascade_kinds.py`.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_deliverable_cascade.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops.cascade_backstop_sweep  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.cascade_retract  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.handoff_children  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.handoff_transition  # noqa: F401 — fires @register_op side effect
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

# Declared, not excused: this file spawns a real `git` process because the
# happy-path write leg (AC10's "parent ships") resolves shipped_in evidence
# against a real commit — no fixture stands in for that. Spawn ratchet:
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_handler = cascade_mod._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


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
    deployment_state: str = "ready_to_fire",
    deliverable_id: str = "dlv-test-000000",
    kind: Optional[str] = None,
    scope: Optional[list] = None,
    commit: bool = False,
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
        f"deliverable_id: {deliverable_id}\n"
    )
    if kind is not None:
        fm += f"kind: {kind}\n"
    if scope is not None:
        fm += "scope:\n" + "".join(f"  - {p}\n" for p in scope)
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    if commit:
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", f"add {name}")
    return path


def _fm_field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


# ---------------------------------------------------------------------------
# AC3 — leg "d" declared on both kind descriptors, sizing exempt
# ---------------------------------------------------------------------------


def test_ac3_handoff_kind_has_leg_d_applies():
    leg_d = cascade_mod._HANDOFF_KIND.predicate_legs["d"]
    assert leg_d.applies is True


def test_ac3_sizing_kind_leg_d_is_exempt_with_a_reason():
    leg_d = cascade_mod._SIZING_KIND.predicate_legs["d"]
    assert leg_d.applies is False
    assert leg_d.reason


def test_ac3_sizing_kind_end_to_end_unaffected_by_leg_d(tmp_path):
    """Byte-identical behaviour claim, exercised: a routed sizing-object with
    a matching deliverable_id still advances to shipped exactly as before
    leg (d) existed — the exempt leg must never turn into a silent refusal."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = repo / "state" / "sizings" / "20260101-a.yaml"
    sizing.parent.mkdir(parents=True, exist_ok=True)
    sizing.write_text(
        "schema: sizing-object\n"
        "intent: Test intent, verbatim.\n"
        "estimate:\n"
        "  tshirt: M\n"
        "  provisional: true\n"
        "route: plan\n"
        "detents: []\n"
        "fork: null\n"
        "xl_exit: null\n"
        "status: routed\n"
        "premise:\n"
        "  provenance: read\n"
        "  evidence: test fixture, no real premise verified\n"
        "deliverable_id: dlv-sizing-legd-0\n",
        encoding="utf-8",
    )

    result = _run(
        {
            "deliverable_id": "dlv-sizing-legd-0",
            "source_kind": "plan",
            "source_path": "docs/plans/dummy.md",
            "target_kind": "sizing",
        },
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0
    assert len(result["advanced"]) == 1


# ---------------------------------------------------------------------------
# AC1 — a kind: spinoff candidate is never advanced, even on exact match
# ---------------------------------------------------------------------------


def test_ac1_spinoff_candidate_is_not_advanced_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(
        repo,
        "20260101-spinoff.md",
        deliverable_id="dlv-inherited-000000",
        kind="spinoff",
        commit=True,
    )

    result = _run(
        {
            "deliverable_id": "dlv-inherited-000000",
            "source_kind": "plan",
            "source_path": "docs/plans/dummy.md",
        },
        repo_root=repo / ".git",
    )

    assert result["advanced"] == []
    assert result["exit_code"] == 1


def test_ac1_non_spinoff_kind_with_same_shape_is_unaffected(tmp_path, monkeypatch):
    """Regression companion to AC1: an otherwise-identical candidate that is
    NOT kind: spinoff must still advance — leg (d) must discriminate on
    `kind`, not on any other field of the fixture."""
    session_id = "55555555-5555-5555-5555-555555555555"
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    repo = tmp_path / "repo"
    _init_repo(repo)
    scoped = repo / "feature.txt"
    scoped.write_text("feature body\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(
        repo, "commit", "-m",
        f"implement the feature this handoff scopes\n\nSession-Id: {session_id}",
    )
    _seed_handoff(
        repo,
        "20260101-notspinoff.md",
        deliverable_id="dlv-not-spinoff-0",
        kind="session-handoff",
        scope=["feature.txt"],
        commit=True,
    )

    result = _run(
        {
            "deliverable_id": "dlv-not-spinoff-0",
            "source_kind": "plan",
            "source_path": "docs/plans/dummy.md",
        },
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0
    assert len(result["advanced"]) == 1


# ---------------------------------------------------------------------------
# AC2 — the refusal is recorded through _predicate_refusal, surfaces in
# refused[], no new output key, no branch logic in the collector.
# ---------------------------------------------------------------------------


def test_ac2_predicate_refusal_directly_returns_named_spinoff_reason(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo, "20260101-spinoff.md", deliverable_id="dlv-inherited-000000", kind="spinoff"
    )
    fm = {"deployment_state": "ready_to_fire", "deliverable_id": "dlv-inherited-000000", "kind": "spinoff"}

    reason = asyncio.run(
        cascade_mod._predicate_refusal(handoff, fm, repo / ".git")
    )

    assert reason is not None
    assert "spinoff" in reason


def test_ac2_refusal_surfaces_in_refused_list_no_new_output_key(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo,
        "20260101-spinoff.md",
        deliverable_id="dlv-inherited-000000",
        kind="spinoff",
        commit=True,
    )

    result = _run(
        {
            "deliverable_id": "dlv-inherited-000000",
            "source_kind": "plan",
            "source_path": "docs/plans/dummy.md",
        },
        repo_root=repo / ".git",
    )

    assert result["refused"]
    refused_paths = [entry["path"] for entry in result["refused"]]
    assert any(str(handoff) in p or handoff.name in p for p in refused_paths)
    # No new top-level output key introduced for this leg — the refusal rides
    # the existing named list, nothing else changed shape.
    assert "kind_refused" not in result
    assert "spinoff_refused" not in result


def test_ac2_collector_is_unaware_of_kind_no_branch_logic():
    """The collector (`_collect_live_candidates_for_kind`) has no
    refusal-recording channel and must stay ignorant of `kind` policy — a
    matching spinoff is still a CANDIDATE (collected), only refused later by
    the predicate. Proves the exclusion is not a filter inside the
    collector."""
    import inspect

    source = inspect.getsource(cascade_mod._collect_live_candidates_for_kind)
    assert "spinoff" not in source
    assert "_SPINOFF_KINDS" not in source


# ---------------------------------------------------------------------------
# AC10 — composition fixture: parent ships; a correctly-minted spinoff
# (distinct id) is untouched; a legacy inherited-id spinoff is refused.
# ---------------------------------------------------------------------------


def test_ac10_composition_parent_ships_minted_spinoff_untouched_legacy_spinoff_refused(
    tmp_path, monkeypatch
):
    session_id = "66666666-6666-6666-6666-666666666666"
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    repo = tmp_path / "repo"
    _init_repo(repo)
    scoped = repo / "feature.txt"
    scoped.write_text("feature body\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(
        repo, "commit", "-m",
        f"implement the feature this handoff scopes\n\nSession-Id: {session_id}",
    )

    parent_id = "dlv-parent-plan-000000"

    # 1) the parent's own handoff — not a spinoff, should ship.
    parent_handoff = _seed_handoff(
        repo,
        "20260101-parent.md",
        deliverable_id=parent_id,
        kind="session-handoff",
        scope=["feature.txt"],
        commit=True,
    )

    # 2) a correctly-minted spinoff — its own distinct id (per the plan's
    # authoring-door fix, 2026-08-05 PM ruling), never sharing the parent's.
    minted_spinoff = _seed_handoff(
        repo,
        "20260101-minted-spinoff.md",
        deliverable_id="dlv-minted-spinoff-000000",
        kind="spinoff",
        commit=True,
    )

    # 3) a legacy spinoff that inherited the parent's id (the defect this
    # plan closes) — must be refused by leg (d), never advanced.
    legacy_spinoff = _seed_handoff(
        repo,
        "20260101-legacy-spinoff.md",
        deliverable_id=parent_id,
        kind="spinoff",
        commit=True,
    )

    result = _run(
        {
            "deliverable_id": parent_id,
            "source_kind": "plan",
            "source_path": "docs/plans/dummy.md",
        },
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0
    assert len(result["advanced"]) == 1

    assert _fm_field(parent_handoff, "deployment_state") == "shipped"
    # The minted spinoff carries a different deliverable_id entirely — it was
    # never even a candidate for THIS query, so it is untouched.
    assert _fm_field(minted_spinoff, "deployment_state") == "ready_to_fire"
    assert _fm_field(minted_spinoff, "deliverable_id") == "dlv-minted-spinoff-000000"
    # The legacy inherited-id spinoff matched the join but was refused by the
    # kind predicate — untouched, not silently flipped.
    assert _fm_field(legacy_spinoff, "deployment_state") == "ready_to_fire"

    refused_paths = [entry["path"] for entry in result["refused"]]
    assert any(
        str(legacy_spinoff) in p or legacy_spinoff.name in p for p in refused_paths
    )
