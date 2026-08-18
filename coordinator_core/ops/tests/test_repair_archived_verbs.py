"""
coordinator_core.ops.tests.test_repair_archived_verbs

C8 (docs/plans/2026-08-18-a-spinoff-is-not-its-parents-deliverable.md) — AC13:
moving an archived record OFF `shipped` clears the provenance the shipped
state implied (`shipped_in`, `advanced_by`, `advanced_at`) in ONE verb
invocation, ordered so `schema_validate._cf_shipped_in_required` never trips.

Spec backlink: coordinator_core/ops/handoff_stamp.py
  ::_repair_archived_deployment_state_handler, § off-shipped provenance repair

Coverage:
  (a) a record carrying the full false triple (shipped_in + advanced_by +
      advanced_at) is left truthful — all three cleared — in ONE call that
      also flips deployment_state off shipped to a non-terminal target.
  (b) the ordering trap this single-call design avoids: clearing shipped_in
      via the SEPARATE shipped_in-repair door FIRST, while deployment_state
      still reads "shipped" on disk, produces a record
      `_cf_shipped_in_required` itself flags as invalid — pinning the "clear-
      then-flip trips the guard" claim against the validator directly rather
      than by comment.
  (c) partial-triple record (only advanced_by present, no shipped_in/
      advanced_at) — only the fields actually present are cleared/reported;
      no KeyError, no spurious "cleared" claim for an absent field.
  (d) sideways-between-terminal-states is UNCHANGED: shipped -> closed still
      refuses (this is NOT "moving off shipped to a non-terminal target");
      pins that the AC13 carve-out did not widen the door beyond its stated
      shape.
  (e) shipped -> shipped (same-state restate) still refuses, unchanged.
  (f) the ordinary in_flight -> shipped repair path (pre-existing behavior)
      is untouched: no provenance fields are cleared moving INTO shipped.
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path

# Import guard — MUST precede any test so @register_op fires first (mirrors
# test_handoff_stamp.py's own guard; this module reaches the same registry).
import coordinator_core.ops.handoff_stamp  # noqa: F401 — fires @register_op

from coordinator_core.frontmatter.schema_validate import _cf_shipped_in_required
from coordinator_core.ops.handoff_stamp import (
    _repair_archived_deployment_state_handler,
    _repair_archived_shipped_in_handler,
)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    """Minimal git repo with a committed state/handoffs/ skeleton.

    Returns repo_root (the main worktree root, NOT the .git dir) — callers
    pass repo_root / ".git" as the handler's repo_root (P9 WORKTREE
    DERIVATION: repo_root arrives as <worktree>/.git).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args), cwd=str(repo), capture_output=True, check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "repair-test@claude-klabauter.test")
    _git("config", "user.name", "Repair Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _seed_shipped_co_tenant(repo: Path, name: str, extra_fm: str = "") -> Path:
    """Archived handoff carrying the full false provenance triple a
    co-tenant cascade casualty leaves behind: deployment_state: shipped,
    shipped_in (the PARENT's ship commit, not this record's own),
    advanced_by (a deliverable that never advanced THIS record), and
    advanced_at."""
    path = repo / "archive" / "handoffs" / "2026-08" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = textwrap.dedent(f"""\
        ---
        title: "Co-tenant Casualty"
        created: 2026-08-01
        branch: "work/test/2026-08-01"
        status: claimed
        predecessor: none
        kind: spinoff
        deployment_state: shipped
        claimed_at: '2026-08-01T00:00:00Z'
        claimed_by: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
        shipped_in: deadbeef
        advanced_by: dlv-unrelated-parent
        advanced_at: '2026-08-01T01:00:00Z'
        {extra_fm.strip()}
        ---

        # Handoff body.
    """)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) One call clears the full false triple while flipping off shipped
# ---------------------------------------------------------------------------


def test_off_shipped_repair_clears_full_provenance_triple_in_one_call(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_shipped_co_tenant(repo, "2026-08-01_test-co-tenant.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "co-tenancy: swept by dlv-unrelated-parent's cascade, not this record's own",
            "deployment_state": "ready_to_fire",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["prior_state"] == "shipped"
    assert result["new_state"] == "ready_to_fire"
    assert set(result["provenance_cleared"]) == {"shipped_in", "advanced_by", "advanced_at"}

    text = hpath.read_text(encoding="utf-8")
    assert "deployment_state: ready_to_fire" in text
    assert "shipped_in:" not in text
    assert "advanced_by:" not in text
    assert "advanced_at:" not in text

    # The final, only-ever-persisted state must itself be valid under the
    # cross-field rule this whole repair exists to keep satisfied.
    fm = {"deployment_state": "ready_to_fire", "created": "2026-08-01"}
    assert _cf_shipped_in_required(fm) is None


# ---------------------------------------------------------------------------
# (b) The two-call ordering trap this single-call design exists to avoid
# ---------------------------------------------------------------------------


def test_clear_then_flip_via_separate_calls_trips_the_validator(tmp_path):
    """Pins the ORDER IS LOAD-BEARING claim directly against the validator:
    clearing shipped_in via the separate shipped_in-repair door FIRST, while
    deployment_state still reads "shipped" on disk, leaves an on-disk record
    that _cf_shipped_in_required itself flags as invalid. This is the exact
    intermediate state the widened single-call
    _repair_archived_deployment_state_handler never persists (test a)."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_shipped_co_tenant(repo, "2026-08-01_test-wrong-order.md")

    clear_result = _run(_repair_archived_shipped_in_handler(
        {
            "handoff_path": str(hpath),
            "reason": "co-tenancy: clearing shipped_in first (deliberately the wrong order)",
            "unset": True,
        },
        repo_root=repo / ".git",
    ))
    assert clear_result["exit_code"] == 0, clear_result
    assert clear_result["applied"] is True

    # On-disk state right now: deployment_state still "shipped", shipped_in
    # gone. Read it back and hand it to the validator exactly as it sits.
    text = hpath.read_text(encoding="utf-8")
    assert "deployment_state: shipped" in text
    assert "shipped_in:" not in text

    fm = {"deployment_state": "shipped", "created": "2026-08-01", "shipped_in": None}
    error = _cf_shipped_in_required(fm)
    assert error is not None, (
        "expected _cf_shipped_in_required to flag the intermediate "
        "clear-then-flip state as invalid — the exact trap the single-call "
        "off-shipped repair path avoids"
    )
    assert error["field"] == "shipped_in"


# ---------------------------------------------------------------------------
# (c) Partial triple — only the fields actually present are touched
# ---------------------------------------------------------------------------


def test_off_shipped_repair_partial_triple_clears_only_present_fields(tmp_path):
    repo = _make_git_repo(tmp_path)
    path = repo / "archive" / "handoffs" / "2026-08" / "2026-08-01_test-partial.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent("""\
            ---
            title: "Partial Triple"
            created: 2026-08-01
            branch: "work/test/2026-08-01"
            status: claimed
            predecessor: none
            kind: spinoff
            deployment_state: shipped
            claimed_at: '2026-08-01T00:00:00Z'
            claimed_by: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
            advanced_by: dlv-unrelated-parent
            ---

            # Handoff body.
        """),
        encoding="utf-8",
    )

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(path),
            "reason": "co-tenancy: only advanced_by was ever stamped on this one",
            "deployment_state": "in_flight",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["provenance_cleared"] == ["advanced_by"]
    text = path.read_text(encoding="utf-8")
    assert "advanced_by:" not in text


# ---------------------------------------------------------------------------
# (d) Sideways between terminal states stays refused — carve-out did not widen
# ---------------------------------------------------------------------------


def test_shipped_to_closed_still_refuses(tmp_path):
    """shipped -> closed is NOT "moving off shipped to a non-terminal
    target" — closed is itself terminal. Must refuse exactly as before
    AC13; the carve-out is narrowly shipped -> non-terminal only."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_shipped_co_tenant(repo, "2026-08-01_test-sideways.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: sideways between terminal states must still refuse",
            "deployment_state": "closed",
            "closed_reason": "stale",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "terminal" in result["error"]
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (e) shipped -> shipped restate stays refused — carve-out did not widen
# ---------------------------------------------------------------------------


def test_shipped_to_shipped_still_refuses(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_shipped_co_tenant(repo, "2026-08-01_test-restate.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: restating shipped must still refuse",
            "deployment_state": "shipped",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "terminal" in result["error"]
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (f) Pre-existing into-shipped repair path is untouched by the carve-out
# ---------------------------------------------------------------------------


def test_in_flight_to_shipped_repair_clears_nothing(tmp_path):
    repo = _make_git_repo(tmp_path)
    path = repo / "archive" / "handoffs" / "2026-08" / "2026-08-01_test-into-shipped.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent("""\
            ---
            title: "Into Shipped"
            created: 2026-08-01
            branch: "work/test/2026-08-01"
            status: claimed
            predecessor: none
            kind: session-handoff
            deployment_state: in_flight
            claimed_at: '2026-08-01T00:00:00Z'
            claimed_by: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
            ---

            # Handoff body.
        """),
        encoding="utf-8",
    )

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(path),
            "reason": "test: ordinary into-shipped repair, unaffected by AC13",
            "deployment_state": "shipped",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["provenance_cleared"] == []
