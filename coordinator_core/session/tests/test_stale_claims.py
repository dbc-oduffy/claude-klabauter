"""
coordinator_core.session.tests.test_stale_claims — tests for
coordinator_core.session.stale_claims.list_stale_claim_handoffs.

Coverage (per this module's dispatch brief):
  - a live handoff whose claimer session is DEAD is reported as stale.
  - a live handoff whose claimer session is LIVE is excluded.
  - an unclaimed (open) handoff is excluded (no claimer to ask liveness about).
  - the DR-084 ``consumed_by`` fallback is honored when ``claimed_by`` is absent.
  - ``claimed_by`` wins over ``consumed_by`` when a record carries both.

Fixture style mirrors coordinator_core/session/tests/test_liveness.py's
``_make_repo``/``_write_session`` helpers (real git repo + real
``.git/coordinator-sessions/<sid>/meta.json``, never a liveness stub) — this
module's whole point is that the enumerator agrees with the REAL
``session_live`` verdict, not a mocked one.

Spec backlink: cross-repo/inbox/2026-07-23-claude-klabauter-em-wsc-step0-fails-open-crash-recovery.md
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordinator_core.session import core, stale_claims

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
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


class TestListStaleClaimHandoffs:
    def test_dead_claimer_is_reported_stale(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-dead", {"pid": "999", "last_activity": "2000-01-01T00:00:00Z"})
        p = _seed_handoff(repo, "2026-07-23-a.md", "claimed_by: s-dead")

        result = stale_claims.list_stale_claim_handoffs(str(repo))

        paths = [r.path for r in result]
        assert str(p.resolve()) in paths or str(p) in paths
        entry = next(r for r in result if r.claimer_sid == "s-dead")
        assert entry.claimer_sid == "s-dead"

    def test_live_claimer_is_excluded(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-live", {"pid": "999", "last_activity": core.now_iso()})
        _seed_handoff(repo, "2026-07-23-b.md", "claimed_by: s-live")

        result = stale_claims.list_stale_claim_handoffs(str(repo))

        assert result == []

    def test_unclaimed_handoff_is_excluded(self, tmp_path):
        repo = _make_repo(tmp_path)
        _seed_handoff(repo, "2026-07-23-c.md", "")

        result = stale_claims.list_stale_claim_handoffs(str(repo))

        assert result == []

    def test_consumed_by_fallback_when_claimed_by_absent(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-dead-legacy", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"})
        _seed_handoff(repo, "2026-07-23-d.md", "consumed_by: s-dead-legacy")

        result = stale_claims.list_stale_claim_handoffs(str(repo))

        assert len(result) == 1
        assert result[0].claimer_sid == "s-dead-legacy"

    def test_claimed_by_wins_over_consumed_by_when_both_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-dead-new", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"})
        _write_session(repo, "s-live-old", {"pid": "2", "last_activity": core.now_iso()})
        _seed_handoff(
            repo, "2026-07-23-e.md",
            "claimed_by: s-dead-new\nconsumed_by: s-live-old",
        )

        result = stale_claims.list_stale_claim_handoffs(str(repo))

        assert len(result) == 1
        assert result[0].claimer_sid == "s-dead-new"

    def test_defaults_to_cwd_when_repo_root_absent(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-dead", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"})
        _seed_handoff(repo, "2026-07-23-f.md", "claimed_by: s-dead")
        monkeypatch.chdir(repo)

        result = stale_claims.list_stale_claim_handoffs(None)

        assert len(result) == 1
        assert result[0].claimer_sid == "s-dead"
