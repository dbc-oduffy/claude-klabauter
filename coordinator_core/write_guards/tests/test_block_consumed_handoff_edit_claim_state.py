"""Tests for the ``is_holder`` migration in
``coordinator_core.write_guards.block_consumed_handoff_edit`` -- the
holder-leg predicate now resolves ledger-first via
``coordinator_core.claim_state.resolve_claim_state`` instead of reading only
the tracked-frontmatter mirror's ``claimed_by`` field.

Purpose: prove the DESYNCED-BATON negative-spec is now enforced. Before this
migration, a branch-switch-reverted mirror (``claimed_by`` empty/absent while
a live ledger claim still exists) made ``claimed_by`` compare empty against
any real session id -- ``is_holder`` was always False regardless of caller
identity. Two distinct effects follow from routing ``is_holder`` through
``resolve_claim_state``: the TRUE ledger holder, who used to get
misclassified as a non-holder and wrongly hard-denied on their own claimed
baton, is now correctly recognized (non-blocking ``additionalContext`` leg);
a GENUINE non-holder is still hard-denied exactly as before. This suite pins
both directions.

Spec backlink: docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md
  § Tasks, chunk C6b (AC5), split C6b1 (write_guards half).

Negative-spec:
  - Does NOT re-test ``resolve_claim_state`` itself (see
    ``coordinator_core/tests/test_claim_state_accessor.py``) -- only that
    this guard's holder predicate routes through it and degrades correctly
    on resolution failure.
  - Does NOT touch the close-intent leg or the deny-message text of the
    non-holder/close branches -- those are unchanged by this migration.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import claim_state
from coordinator_core.write_guards import block_consumed_handoff_edit as guard


_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_CONSUMED_HANDOFF_EDIT"

# Mirror reverted to `open`-shaped claimed handoff with NO claimed_by --
# the exact branch-switch desync shape (mirror carries `status: claimed` but
# no claimed_by; ledger still holds a live claim elsewhere).
_DESYNCED_BODY = """---
status: claimed
title: "Ship the thing"
branch: work/example/2026-07-21
---

# Ship the thing

Some prior progress notes.
"""


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(_OVERRIDE_ENV, raising=False)


def _make_repo(tmp_path: Path, body: str = _DESYNCED_BODY) -> tuple[Path, Path]:
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    handoff_path = handoffs_dir / "2026-07-20_120000_abc.md"
    handoff_path.write_text(body, encoding="utf-8")
    return tmp_path, handoff_path


def _payload(repo_root: Path, rel_file_path: str) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": rel_file_path, "old_string": "x", "new_string": "y"},
        "cwd": str(repo_root),
    }


def _write_claim_dir(common_dir: Path, handoff_name: str, session_id: str, claimed_at: str = ""):
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at:
        (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


def test_desynced_baton_blocks_non_holder(tmp_path, monkeypatch):
    """A desynced baton (mirror carries no claimed_by, ledger holds a live
    claim by session A) must still HARD-DENY an edit from session B (a
    genuine non-holder) -- this is the negative-spec the migration fixes:
    previously the empty mirror ``claimed_by`` made `is_holder` False for
    everyone, but the deny message ordering bug meant the non-holder leg
    still ran (unchanged) -- the real bug this AC targets is the holder
    case below. This test pins that the non-holder leg is UNCHANGED: still
    denies.
    """
    repo_root, handoff_path = _make_repo(tmp_path)
    common_dir = tmp_path / ".git"
    common_dir.mkdir()

    ledger_holder = "session-A-holder"
    _write_claim_dir(common_dir, handoff_path.name, ledger_holder, "2026-08-07T09:00:00Z")

    monkeypatch.setattr(guard, "_resolve_git_root", lambda cwd: str(repo_root))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "session-B-non-holder")

    with mock.patch.object(claim_state, "git_common_dir", return_value=common_dir), mock.patch.object(
        claim_state, "cs_claim_holder_live", return_value=True
    ):
        result = guard.check(_payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md"))

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "paper trail" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_desynced_baton_permits_true_ledger_holder(tmp_path, monkeypatch):
    """The true holder (session A, the live ledger claimant) must NOT be
    hard-denied even though the tracked-frontmatter mirror is desynced
    (no claimed_by) -- this is the actual fix: `is_holder` now resolves
    ledger-first via `resolve_claim_state`, so a desynced mirror no longer
    misclassifies the genuine holder as a non-holder.
    """
    repo_root, handoff_path = _make_repo(tmp_path)
    common_dir = tmp_path / ".git"
    common_dir.mkdir()

    ledger_holder = "session-A-holder"
    _write_claim_dir(common_dir, handoff_path.name, ledger_holder, "2026-08-07T09:00:00Z")

    monkeypatch.setattr(guard, "_resolve_git_root", lambda cwd: str(repo_root))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", ledger_holder)

    with mock.patch.object(claim_state, "git_common_dir", return_value=common_dir), mock.patch.object(
        claim_state, "cs_claim_holder_live", return_value=True
    ):
        result = guard.check(_payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md"))

    assert result is not None
    hook_output = result["hookSpecificOutput"]
    # Holder leg is non-blocking: additionalContext, no permissionDecision.
    assert "permissionDecision" not in hook_output
    assert "you hold this claim" in hook_output["additionalContext"]


def test_resolve_claim_state_failure_falls_back_to_mirror_read(tmp_path, monkeypatch):
    """If `resolve_claim_state` itself raises, the guard must degrade to its
    pre-migration mirror-only `claimed_by` read rather than crash or
    silently allow -- never regress below what the guard already had.
    """
    repo_root, handoff_path = _make_repo(
        tmp_path,
        body=(
            "---\n"
            "status: claimed\n"
            "claimed_by: mirror-holder\n"
            "title: \"Ship the thing\"\n"
            "---\n\n# Ship the thing\n"
        ),
    )

    monkeypatch.setattr(guard, "_resolve_git_root", lambda cwd: str(repo_root))
    monkeypatch.setattr(
        guard,
        "resolve_claim_state",
        mock.Mock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "mirror-holder")

    result = guard.check(_payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md"))

    assert result is not None
    hook_output = result["hookSpecificOutput"]
    assert "permissionDecision" not in hook_output
    assert "you hold this claim" in hook_output["additionalContext"]
