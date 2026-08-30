"""
coordinator_core.session.tests.test_stale_claims_claim_state — proves
``list_stale_claim_handoffs``/``_claimer_sid`` resolve ledger-first (C5c).

The whole point: a handoff whose frontmatter mirror reverted to ``status:
open`` (branch-switch desync, see ``coordinator_core.claim_state``'s own
docstring for the incident) but whose branch-independent claim LEDGER still
holds a claim by a now-dead session must STILL surface in
``list_stale_claim_handoffs`` — prior to this migration it was silently
excluded (``_claimer_sid`` read the mirror only, saw no ``claimed_by``/
``consumed_by``, and the handoff vanished from the listing).

Fixture style mirrors ``test_stale_claims.py``'s ``_make_repo``/
``_write_session``/``_seed_handoff`` helpers (real git repo + real
``.git/coordinator-sessions/<sid>/meta.json``) plus
``tests/test_claim_state_accessor.py``'s ``_write_claim_dir`` helper for the
ledger side.

Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
§ Tasks, chunk C5 (AC5).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

from coordinator_core import claim_state
from coordinator_core.session import core, stale_claims
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


def _write_session(repo, sid, meta: dict):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return sdir


def _seed_handoff(repo, name: str, frontmatter_extra: str) -> Path:
    path = Path(repo) / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        'title: "Test Handoff"\n'
        "status: active\n"
        f"{frontmatter_extra}\n"
        "---\n\n# Handoff\n\nBody.\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _write_claim_dir(common_dir: Path, handoff_name: str, session_id: str, claimed_at: str = "") -> Path:
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at:
        (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


class TestStaleClaimsLedgerFirst:
    def test_desynced_baton_appears_where_it_previously_vanished(self, tmp_path):
        """AC5: mirror carries NO claimed_by/consumed_by (open) but the
        ledger holds a live claim by a dead session — the desync case that
        ``_claimer_sid``'s prior mirror-only read silently excluded."""
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-dead-ledger", {"pid": "999", "last_activity": "2000-01-01T00:00:00Z"})
        handoff = _seed_handoff(repo, "2026-08-07-desynced.md", "status: open")
        common_dir = Path(repo) / ".git"
        _write_claim_dir(common_dir, handoff.name, "s-dead-ledger", "2026-08-07T10:00:00Z")

        with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
            result = stale_claims.list_stale_claim_handoffs(str(repo))

        assert len(result) == 1
        assert result[0].claimer_sid == "s-dead-ledger"
        assert str(handoff.resolve()) in (result[0].path, str(Path(result[0].path).resolve()))

    def test_desynced_baton_with_live_ledger_holder_is_excluded(self, tmp_path):
        """Ledger claim by a LIVE session must still be excluded — the
        migration must not flip liveness semantics, only claimer resolution."""
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-live-ledger", {"pid": "999", "last_activity": core.now_iso()})
        handoff = _seed_handoff(repo, "2026-08-07-desynced-live.md", "status: open")
        common_dir = Path(repo) / ".git"
        _write_claim_dir(common_dir, handoff.name, "s-live-ledger", "2026-08-07T10:00:00Z")

        with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
            result = stale_claims.list_stale_claim_handoffs(str(repo))

        assert result == []

    def test_ledger_wins_over_disagreeing_mirror(self, tmp_path):
        """Ledger and mirror disagree on WHO holds the claim — ledger must
        win per resolve_claim_state's POSTURE (ledger authoritative)."""
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-dead-ledger-2", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"})
        _write_session(repo, "s-live-mirror", {"pid": "2", "last_activity": core.now_iso()})
        handoff = _seed_handoff(repo, "2026-08-07-disagree.md", "claimed_by: s-live-mirror")
        common_dir = Path(repo) / ".git"
        _write_claim_dir(common_dir, handoff.name, "s-dead-ledger-2", "2026-08-07T10:00:00Z")

        with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
            result = stale_claims.list_stale_claim_handoffs(str(repo))

        assert len(result) == 1
        assert result[0].claimer_sid == "s-dead-ledger-2"

    def test_no_ledger_falls_back_to_mirror(self, tmp_path):
        """No ledger claim dir at all — resolution degrades to the mirror,
        matching the module's prior (pre-migration) behavior exactly."""
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-dead-mirror-only", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"})
        _seed_handoff(repo, "2026-08-07-mirror-only.md", "claimed_by: s-dead-mirror-only")

        result = stale_claims.list_stale_claim_handoffs(str(repo))

        assert len(result) == 1
        assert result[0].claimer_sid == "s-dead-mirror-only"
