"""
coordinator_core.ops.tests.test_handoff_reconcile_close_terminal

Tests for the handoff.reconcile_close_terminal composite op (close + archive,
for the "reconcile concluded terminal, no successor" shape).

Import guard: coordinator_core.ops.handoff_reconcile_close_terminal MUST be
imported at module load time to fire
@register_op("handoff.reconcile_close_terminal") and populate _REGISTRY —
mirrors test_handoff_ship_archive.py's own import-guard precedent (lesson:
state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) op registered
  (b) happy path — a live, unreferenced handoff is closed (reason=displaced)
      and archived in one call
  (c) idempotent replay — a second call against the (now-archived) path
      resolves the already-closed-and-archived shape, no mutation attempted
  (d) missing handoff_path -> exit_code:2
  (e) invalid reason -> exit_code:2
  (f) missing repo_root -> exit_code:1
  (g) path outside state/handoffs/ AND every known archive root -> exit_code:1
  (h) live-children guard retains — closed:True, archived:False, retained:True
  (i) close refuses an already-shipped/continued conflicting terminal ->
      exit_code:1, archived stays False (archive step never runs)

Spec backlink: cross-repo/inbox/2026-08-04-example-market-data-repo-em-baton-
terminal-state-not-cleared-programmatically.md, defect 1, item 2.
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("handoff.reconcile_close_terminal") as a
# side-effect. MUST precede any test function so the registry is populated
# before assertions.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_reconcile_close_terminal  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_reconcile_close_terminal import _handler
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

_OP_NAME = "handoff.reconcile_close_terminal"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_reconcile_close_terminal @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency needed."""
    return asyncio.run(coro)


def _deployment_state(repo, name: str) -> str:
    text = repo.read_text(name)
    split = split_frontmatter(text)
    assert split is not None
    return read_fm_field(split.fm_text, "deployment_state")


def _archive_glob(repo, name: str):
    return [p for p in (repo.root / "archive" / "handoffs").rglob("*.md") if p.name == name]


# ---------------------------------------------------------------------------
# (a) registration
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY, (
        f"{_OP_NAME!r} must be registered; present ops: {sorted(_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# (b) happy path
# ---------------------------------------------------------------------------


def test_happy_path_closes_and_archives(handoff_repo):
    name = "2026-08-04-orphan-baton.md"
    handoff_repo.seed_handoff(name, "open", deployment_state="ready_to_fire")

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["closed"] is True
    assert result["already_closed"] is False
    assert result["archived"] is True
    assert result["already_archived"] is False
    assert result["retained"] is False
    assert not (handoff_repo.root / "state" / "handoffs" / name).exists()

    archived = _archive_glob(handoff_repo, name)
    assert len(archived) == 1
    split = split_frontmatter(archived[0].read_text(encoding="utf-8"))
    assert read_fm_field(split.fm_text, "deployment_state") == "closed"
    assert read_fm_field(split.fm_text, "closed_reason") == "displaced"


# ---------------------------------------------------------------------------
# (c) idempotent replay
# ---------------------------------------------------------------------------


def test_idempotent_replay_after_archive(handoff_repo):
    name = "2026-08-04-orphan-baton-replay.md"
    handoff_repo.seed_handoff(name, "open", deployment_state="awaiting_gate")

    first = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        handoff_repo.common_dir,
    ))
    assert first["exit_code"] == 0, first
    assert first["archived"] is True

    archived_path = _archive_glob(handoff_repo, name)[0]
    rel = archived_path.relative_to(handoff_repo.root)

    second = _run(_handler(
        {"handoff_path": str(rel), "reason": "displaced"},
        handoff_repo.common_dir,
    ))
    assert second["exit_code"] == 0, second
    assert second["closed"] is True
    assert second["already_closed"] is True
    assert second["archived"] is True
    assert second["already_archived"] is True
    # No second archive copy was created.
    assert len(_archive_glob(handoff_repo, name)) == 1


# ---------------------------------------------------------------------------
# (d) missing handoff_path
# ---------------------------------------------------------------------------


def test_missing_handoff_path(handoff_repo):
    result = _run(_handler({"reason": "displaced"}, handoff_repo.common_dir))
    assert result["exit_code"] == 2
    assert result["closed"] is False
    assert result["archived"] is False


# ---------------------------------------------------------------------------
# (e) invalid reason
# ---------------------------------------------------------------------------


def test_invalid_reason_rejected(handoff_repo):
    name = "2026-08-04-bad-reason.md"
    handoff_repo.seed_handoff(name, "open", deployment_state="ready_to_fire")

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "not-a-real-reason"},
        handoff_repo.common_dir,
    ))
    assert result["exit_code"] == 2
    assert result["closed"] is False
    assert result["archived"] is False
    # No mutation attempted.
    assert _deployment_state(handoff_repo, name) == "ready_to_fire"


# ---------------------------------------------------------------------------
# (f) missing repo_root
# ---------------------------------------------------------------------------


def test_missing_repo_root(handoff_repo):
    name = "2026-08-04-no-repo-root.md"
    handoff_repo.seed_handoff(name, "open", deployment_state="ready_to_fire")

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"}, None,
    ))
    assert result["exit_code"] == 1
    assert result["closed"] is False
    assert result["archived"] is False


# ---------------------------------------------------------------------------
# (g) path outside every allowed root
# ---------------------------------------------------------------------------


def test_path_outside_every_allowed_root_rejected(handoff_repo):
    result = _run(_handler(
        {"handoff_path": "state/other/not-a-handoff.md", "reason": "displaced"},
        handoff_repo.common_dir,
    ))
    assert result["exit_code"] == 1
    assert result["closed"] is False
    assert result["archived"] is False


# ---------------------------------------------------------------------------
# (h) live-children guard retains — genuinely-live work is never swept
# ---------------------------------------------------------------------------


def test_live_children_guard_retains(handoff_repo):
    name = "2026-08-04-has-live-child.md"
    handoff_repo.seed_handoff(name, "open", deployment_state="ready_to_fire")
    handoff_repo.seed_handoff(
        "2026-08-04-child.md", "claimed", deployment_state="in_flight", predecessor=name
    )

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["closed"] is True
    assert result["archived"] is False
    assert result["retained"] is True
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0
    # The close mutation still applied even though archival was retained.
    assert _deployment_state(handoff_repo, name) == "closed"


# ---------------------------------------------------------------------------
# (i) close refuses a conflicting completed terminal
# ---------------------------------------------------------------------------


def test_close_refuses_conflicting_terminal(handoff_repo):
    name = "2026-08-04-already-shipped.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="shipped",
        shipped_in="a" * 8, shipped_in_kind="ship-commit",
    )

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["closed"] is False
    assert result["archived"] is False
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0
