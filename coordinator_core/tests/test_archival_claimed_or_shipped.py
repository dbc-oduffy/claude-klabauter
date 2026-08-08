"""
Tests for coordinator_core.archival::claimed_or_shipped / claimed_or_shipped_at_path
— the ledger-first widening of the DR-242 "was this ever claimed or shipped"
predicate.

Spec backlink: docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md
§ Tasks, chunk C10 (AC15).

AC15 requires a property test over a fixture corpus of claimed and unclaimed
handoffs asserting BOTH pre-change and post-change behavior: every input that
returned True before this change must still return True (never relax), PLUS a
ledger-only claim whose mirror is reverted must now also return True (widen).

Strategy: `claimed_or_shipped(fm)` — the pure frontmatter-only function — is
UNCHANGED by this chunk (frontmatter checks were never touched), so it stands
in directly as the "pre-change baseline" oracle: for every fixture in the
corpus, `claimed_or_shipped(_frontmatter(path))` is what the predicate would
have returned before C10. The property asserted per fixture is:

    claimed_or_shipped_at_path(path) is True whenever the pre-change oracle
    is True (never relax), and the post-change function is True STRICTLY MORE
    OFTEN — specifically for the ledger-only/mirror-reverted case the
    pre-change oracle necessarily misses.
"""

from __future__ import annotations

import functools
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import archival
from coordinator_core import claim_state


def _write_handoff(
    path: Path,
    *,
    status: str = "open",
    claimed_by: str = "",
    consumed_by: str = "",
    claimed_at: str = "",
    deployment_state: str = "",
    shipped_in: str = "",
) -> None:
    lines = ["---", f"status: {status}"]
    if claimed_by:
        lines.append(f"claimed_by: {claimed_by}")
    if consumed_by:
        lines.append(f"consumed_by: {consumed_by}")
    if claimed_at:
        lines.append(f"claimed_at: {claimed_at}")
    if deployment_state:
        lines.append(f"deployment_state: {deployment_state}")
    if shipped_in:
        lines.append(f"shipped_in: {shipped_in}")
    lines.append("---")
    lines.append("")
    lines.append("# body")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_claim_dir(common_dir: Path, handoff_name: str, session_id: str, claimed_at: str = "") -> Path:
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at:
        (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


@pytest.fixture
def workspace(tmp_path):
    common_dir = tmp_path / "gitdir"
    common_dir.mkdir()
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    return common_dir, handoffs_dir


def _bind_common_dir(monkeypatch, common_dir: Path, holder_live: bool = True) -> None:
    """Route archival.resolve_claim_state at a fixed common_dir, with ledger
    holder liveness fixed, so tests don't need a real git repo or spawn a
    subprocess."""
    bound = functools.partial(claim_state.resolve_claim_state, common_dir=common_dir)
    monkeypatch.setattr(archival, "resolve_claim_state", bound)
    monkeypatch.setattr(claim_state, "cs_claim_holder_live", lambda *_a, **_k: holder_live)


# ---------------------------------------------------------------------------
# AC15 fixture corpus — a mix of pre-change-True, pre-change-False, and the
# widened ledger-only/mirror-reverted case.
# ---------------------------------------------------------------------------


def _corpus(handoffs_dir: Path):
    """Yield (name, write_fn, ledger: Optional[(session_id, claimed_at)])
    fixtures. `ledger` is None when no ledger claim dir should be written."""
    return [
        (
            "status-claimed.md",
            lambda p: _write_handoff(p, status="claimed"),
            None,
        ),
        (
            "status-consumed.md",
            lambda p: _write_handoff(p, status="consumed"),
            None,
        ),
        (
            "status-superseded.md",
            lambda p: _write_handoff(p, status="superseded"),
            None,
        ),
        (
            "claimed-by-field.md",
            lambda p: _write_handoff(p, claimed_by="sess-x", claimed_at="2026-08-07T10:00:00Z"),
            None,
        ),
        (
            "consumed-by-legacy.md",
            lambda p: _write_handoff(p, consumed_by="sess-y", claimed_at="2026-08-07T11:00:00Z"),
            None,
        ),
        (
            "shipped-deployment-state.md",
            lambda p: _write_handoff(p, deployment_state="shipped"),
            None,
        ),
        (
            "shipped-in.md",
            lambda p: _write_handoff(p, shipped_in="abc1234"),
            None,
        ),
        (
            "abandoned-legacy.md",
            lambda p: _write_handoff(p, deployment_state="abandoned"),
            None,
        ),
        (
            "open-never-claimed.md",
            lambda p: _write_handoff(p, status="open"),
            None,
        ),
        (
            "open-with-live-ledger-claim-and-matching-mirror.md",
            lambda p: _write_handoff(p, status="claimed", claimed_by="sess-z", claimed_at="2026-08-07T12:00:00Z"),
            ("sess-z", "2026-08-07T12:00:00Z"),
        ),
        (
            "ledger-only-mirror-reverted.md",
            lambda p: _write_handoff(p, status="open"),
            ("sess-w", "2026-08-07T13:00:00Z"),
        ),
    ]


def test_ac15_widen_never_relax_property(workspace, monkeypatch):
    common_dir, handoffs_dir = workspace
    _bind_common_dir(monkeypatch, common_dir, holder_live=True)

    corpus = _corpus(handoffs_dir)
    saw_widened_true = False

    for name, write_fn, ledger in corpus:
        path = handoffs_dir / name
        write_fn(path)
        if ledger is not None:
            session_id, claimed_at = ledger
            _write_claim_dir(common_dir, name, session_id, claimed_at)

        pre_change_result = archival.claimed_or_shipped(archival._frontmatter(str(path)))
        post_change_result = archival.claimed_or_shipped_at_path(str(path))

        # WIDEN, NEVER RELAX: every pre-change True stays True post-change.
        if pre_change_result:
            assert post_change_result is True, (
                f"{name}: pre-change True but post-change False — RELAXED, "
                "violates AC15/DR-242"
            )

        if name == "ledger-only-mirror-reverted.md":
            # The widened case: pre-change (frontmatter-only) says False,
            # post-change (ledger-first) says True.
            assert pre_change_result is False
            assert post_change_result is True
            saw_widened_true = True
        elif name == "open-never-claimed.md":
            assert pre_change_result is False
            assert post_change_result is False

    assert saw_widened_true, "corpus must exercise the widened ledger-only/mirror-reverted case"


def test_ledger_only_claim_dead_holder_does_not_widen(workspace, monkeypatch):
    """The ledger side degrades to 'no claim' for a dead holder (mirrors
    resolve_claim_state's own negative-spec) — a dead-holder ledger claim must
    not cause claimed_or_shipped_at_path to widen to True."""
    common_dir, handoffs_dir = workspace
    _bind_common_dir(monkeypatch, common_dir, holder_live=False)

    path = handoffs_dir / "dead-holder-ledger-claim.md"
    _write_handoff(path, status="open")
    _write_claim_dir(common_dir, path.name, "sess-dead", "2026-08-07T14:00:00Z")

    assert archival.claimed_or_shipped_at_path(str(path)) is False


def test_shipped_in_frontmatter_only_unaffected_by_ledger(workspace, monkeypatch):
    """The SHIPPED half has no ledger counterpart — a handoff with no ledger
    claim at all but a shipped_in stamp must still return True via the
    frontmatter-only path, unaffected by ledger resolution."""
    common_dir, handoffs_dir = workspace
    _bind_common_dir(monkeypatch, common_dir, holder_live=True)

    path = handoffs_dir / "shipped-in-only.md"
    _write_handoff(path, shipped_in="deadbeef")

    assert archival.claimed_or_shipped_at_path(str(path)) is True


def test_no_ledger_no_mirror_claim_stays_false(workspace, monkeypatch):
    common_dir, handoffs_dir = workspace
    _bind_common_dir(monkeypatch, common_dir, holder_live=True)

    path = handoffs_dir / "truly-unclaimed.md"
    _write_handoff(path, status="open")

    assert archival.claimed_or_shipped_at_path(str(path)) is False


def test_ledger_resolution_error_degrades_to_frontmatter_only(workspace, monkeypatch):
    """Any error resolving the ledger side degrades to the pre-widening
    frontmatter-only answer — fail-closed on the widening, never fail-open."""
    common_dir, handoffs_dir = workspace

    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(archival, "resolve_claim_state", _raise)

    path = handoffs_dir / "ledger-errors-but-claimed-by-mirror.md"
    _write_handoff(path, claimed_by="sess-q", claimed_at="2026-08-07T15:00:00Z")
    assert archival.claimed_or_shipped_at_path(str(path)) is True

    path2 = handoffs_dir / "ledger-errors-and-unclaimed.md"
    _write_handoff(path2, status="open")
    assert archival.claimed_or_shipped_at_path(str(path2)) is False


def test_missing_file_still_fails_closed(tmp_path):
    missing = tmp_path / "does-not-exist.md"
    assert archival.claimed_or_shipped_at_path(str(missing)) is False


def test_claimed_or_shipped_pure_function_unchanged_by_this_chunk():
    """Regression guard: claimed_or_shipped(fm) itself — the pure oracle this
    test file uses as its pre-change baseline — is untouched by C10; only the
    path-based wrapper widens."""
    fm = "status: claimed\n"
    assert archival.claimed_or_shipped(fm) is True
    fm_open = "status: open\n"
    assert archival.claimed_or_shipped(fm_open) is False
