"""
Tests for coordinator_core.claim_state — the ledger-first claim accessor.

Spec backlink: docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md
§ Tasks, chunk C1 (AC1, AC2).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import claim_state


def _write_claim_dir(common_dir: Path, handoff_name: str, session_id: str, claimed_at: str = "") -> Path:
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at:
        (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


def _write_handoff(path: Path, *, claimed_by: str = "", consumed_by: str = "", claimed_at: str = "", status: str = "open") -> None:
    lines = ["---", f"status: {status}"]
    if claimed_by:
        lines.append(f"claimed_by: {claimed_by}")
    if consumed_by:
        lines.append(f"consumed_by: {consumed_by}")
    if claimed_at:
        lines.append(f"claimed_at: {claimed_at}")
    lines.append("---")
    lines.append("")
    lines.append("# body")
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    common_dir = tmp_path / "gitdir"
    common_dir.mkdir()
    handoff = tmp_path / "state" / "handoffs" / "2026-08-07-example.md"
    handoff.parent.mkdir(parents=True)
    return common_dir, handoff


def test_both_sources_present_agree(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-a", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, claimed_by="sess-a", claimed_at="2026-08-07T10:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder == "sess-a"
    assert state.source == "ledger"
    assert state.disagreement is False
    assert state.ledger_holder == "sess-a"
    assert state.mirror_holder == "sess-a"


def test_ledger_only_holder_live(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-b", "2026-08-07T11:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder == "sess-b"
    assert state.claimed_at == "2026-08-07T11:00:00Z"
    assert state.source == "ledger"
    assert state.mirror_holder is None


def test_mirror_only_no_ledger(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, claimed_by="sess-c", claimed_at="2026-08-07T12:00:00Z", status="claimed")

    state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder == "sess-c"
    assert state.source == "mirror"
    assert state.disagreement is False
    assert state.ledger_holder is None


def test_disagreement_ledger_claims_mirror_does_not(workspace):
    """AC2: the branch-switch-revert desync — ledger holds a live claim, the
    mirror reverted to open. The accessor must report this as a distinct,
    inspectable flag, resolving ledger-first (never silently trusting the
    mirror's "open" answer)."""
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-d", "2026-08-07T13:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.disagreement is True
    assert state.holder == "sess-d"
    assert state.source == "ledger"
    assert state.mirror_holder is None


def test_ledger_holder_dead_degrades_to_mirror(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-e", "2026-08-07T14:00:00Z")
    _write_handoff(handoff, claimed_by="sess-e", claimed_at="2026-08-07T14:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=False):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.ledger_holder is None
    assert state.holder == "sess-e"
    assert state.source == "mirror"
    assert state.disagreement is False


def test_legacy_consumed_by_shape(workspace):
    """DR-084: consumed_by is still accepted on the mirror side."""
    common_dir, handoff = workspace
    _write_handoff(handoff, consumed_by="sess-f", claimed_at="2026-08-07T15:00:00Z", status="claimed")

    state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder == "sess-f"
    assert state.source == "mirror"


def test_neither_source_present(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, status="open")

    state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder is None
    assert state.source == "none"
    assert state.disagreement is False


def test_ledger_liveness_check_raises_degrades_to_mirror(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-g", "2026-08-07T16:00:00Z")
    _write_handoff(handoff, claimed_by="sess-g", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", side_effect=RuntimeError("boom")):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.ledger_holder is None
    assert state.holder == "sess-g"
    assert state.source == "mirror"


def test_handoff_claim_dir_reexported_from_fleet_common():
    from coordinator_core.ops.fleet import _common

    assert _common.handoff_claim_dir is claim_state.handoff_claim_dir
    assert _common._sessions_dir is claim_state._sessions_dir


def test_handoff_claim_dir_path_shape(tmp_path):
    common_dir = tmp_path / "gitdir"
    handoff = Path("state/handoffs/example.md")
    got = claim_state.handoff_claim_dir(common_dir, handoff)
    assert got == common_dir / "coordinator-sessions" / "handoff-claims" / "example.md"


def test_no_subprocess_re_shelled_out_per_call(workspace, monkeypatch):
    """COST constraint: `git_common_dir` is lru_cache'd and this accessor takes
    an optional pre-resolved `common_dir` (matching `handoff_claim_dir`'s own
    signature) — confirm no subprocess is spawned when common_dir is supplied,
    and that git_common_dir itself is never even called in that path."""
    common_dir, handoff = workspace
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "git_common_dir") as mocked_git_common_dir:
        claim_state.resolve_claim_state(handoff, common_dir=common_dir)
        mocked_git_common_dir.assert_not_called()


def test_git_common_dir_is_lru_cached_when_common_dir_omitted(workspace):
    """When common_dir IS omitted, resolution falls through to
    lifecycle.git_common_dir, which is lru_cache'd process-wide — confirm the
    cache decorator is present so repeated calls with the same argument do not
    re-invoke the underlying (subprocess-spawning) resolver."""
    from coordinator_core.lifecycle import git_common_dir

    assert hasattr(git_common_dir, "cache_info"), (
        "lifecycle.git_common_dir must stay lru_cache'd — claim_state.py's "
        "no-common_dir fallback path relies on this to avoid a subprocess "
        "shell-out per call."
    )


def test_malformed_session_id_degrades_not_raises(workspace):
    """EM-authored P1: a ledger `session_id` file holding invalid UTF-8 raises
    UnicodeDecodeError (a ValueError, NOT an OSError) out of the bare
    `except OSError` guard — this must degrade to "no usable ledger evidence"
    instead, per this accessor's entire degrade-not-raise contract. Blast
    radius: ~25 readers migrated onto this accessor all assume it degrades."""
    common_dir, handoff = workspace
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff.name
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_bytes(b"\xff\xfe\x00bad")
    _write_handoff(handoff, status="open")

    state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.ledger_holder is None
    assert state.holder is None
    assert state.source == "none"


def test_malformed_claimed_at_degrades_not_raises(workspace):
    """EM-authored P1 sibling: a malformed (invalid-UTF-8) `claimed_at` file
    must also degrade, not raise — the ledger session_id is well-formed and
    live here, so the record resolves with claimed_at coerced to None rather
    than propagating a decode error."""
    common_dir, handoff = workspace
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff.name
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text("sess-h", encoding="utf-8")
    (claim_dir / "claimed_at").write_bytes(b"\xff\xfe\x00bad")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.ledger_holder == "sess-h"
    assert state.claimed_at is None


def test_disagreement_flag_narrower_than_full_holder_mismatch(workspace):
    """Reviewer P2: `disagreement` is deliberately narrower than "any
    ledger/mirror mismatch" — it flags ONLY ledger-live-mirror-empty, not a
    both-present holder mismatch. A same-slot mismatch (ledger says sess-x,
    mirror says sess-y) resolves with disagreement=False, but the mismatch
    stays independently visible via ledger_holder/mirror_holder."""
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-x", "2026-08-07T17:00:00Z")
    _write_handoff(handoff, claimed_by="sess-y", claimed_at="2026-08-07T16:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.disagreement is False
    assert state.ledger_holder == "sess-x"
    assert state.mirror_holder == "sess-y"
    assert state.ledger_holder != state.mirror_holder


def test_import_cycle_stays_broken():
    """Slice A P3: the C1 commit message claims an explicit import-cycle
    smoke test was verified; no such test existed. This is that test — import
    claim_state first, then every module the plan's anti-scope names as a
    cycle risk, asserting none raises ImportError."""
    import coordinator_core.claim_state  # noqa: F401
    import coordinator_core.ops  # noqa: F401
    import coordinator_core.ops.fleet._common  # noqa: F401
    import coordinator_core.session.claims  # noqa: F401
    import coordinator_core.baton_assemble.apply  # noqa: F401
