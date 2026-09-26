"""
coordinator_core.ops.tests.test_session_context — unit tests for
``coordinator_core.ops.session_context.resolve_current_session_id``.

Coverage:
  (a) tier-1 env — CLAUDE_SESSION_ID set → returned immediately, no lower tiers tried.
  (b) tier-2 env — CLAUDE_CODE_SESSION_ID set (CLAUDE_SESSION_ID absent) → returned.
  (c) tier-1 precedence — both env vars set → CLAUDE_SESSION_ID wins.
  (d) sentinel ignored — neither env var set, sentinel file present and well-formed →
      None returned (the former tier-3 sentinel read was removed KS-2 2026-08-07; see
      session_context.py module docstring negative-spec).
  (e) null when unresolvable — no env vars, no sentinel file → None returned.
  (f) null when worktree_root is None → None (tiers 1+2 absent).
  (g) env vars take priority over an (ignored) sentinel — tier-1 returns even when
      sentinel exists.

Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C3
Extraction source: coordinator_core/ops/review_trail_write.py:_resolve_session_id
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.session_context import resolve_current_session_id


_ENV1 = "CLAUDE_SESSION_ID"
_ENV2 = "CLAUDE_CODE_SESSION_ID"


class TestTier1Env:
    """Tier-1: CLAUDE_SESSION_ID env var."""

    def test_tier1_returned_when_set(self, monkeypatch):
        """CLAUDE_SESSION_ID set → that value is returned."""
        monkeypatch.setenv(_ENV1, "sess-tier1-abc123")
        monkeypatch.delenv(_ENV2, raising=False)
        result = resolve_current_session_id(worktree_root=None)
        assert result == "sess-tier1-abc123"

    def test_tier1_wins_over_tier2(self, monkeypatch):
        """Both env vars set → CLAUDE_SESSION_ID (tier-1) wins."""
        monkeypatch.setenv(_ENV1, "sess-tier1-wins")
        monkeypatch.setenv(_ENV2, "sess-tier2-loses")
        result = resolve_current_session_id(worktree_root=None)
        assert result == "sess-tier1-wins"

    def test_tier1_wins_over_sentinel(self, monkeypatch, tmp_path):
        sentinel = tmp_path / ".git" / "coordinator-sessions" / ".current-session-id"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("sess-from-sentinel\n", encoding="utf-8")

        monkeypatch.setenv(_ENV1, "sess-from-tier1")
        monkeypatch.delenv(_ENV2, raising=False)

        result = resolve_current_session_id(worktree_root=tmp_path)
        assert result == "sess-from-tier1"


class TestTier2Env:
    """Tier-2: CLAUDE_CODE_SESSION_ID env var."""

    def test_tier2_returned_when_tier1_absent(self, monkeypatch):
        """CLAUDE_CODE_SESSION_ID returned when CLAUDE_SESSION_ID is absent."""
        monkeypatch.delenv(_ENV1, raising=False)
        monkeypatch.setenv(_ENV2, "sess-tier2-xyz789")
        result = resolve_current_session_id(worktree_root=None)
        assert result == "sess-tier2-xyz789"

    def test_tier2_empty_string_ignores_sentinel(self, monkeypatch, tmp_path):
        """CLAUDE_CODE_SESSION_ID set to empty string → sentinel is ignored → None."""
        sentinel = tmp_path / ".git" / "coordinator-sessions" / ".current-session-id"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("sess-from-sentinel", encoding="utf-8")

        monkeypatch.delenv(_ENV1, raising=False)
        monkeypatch.setenv(_ENV2, "")

        result = resolve_current_session_id(worktree_root=tmp_path)
        assert result is None


class TestSentinelRemoved:

    def test_sentinel_present_and_well_formed_is_ignored(self, monkeypatch, tmp_path):
        monkeypatch.delenv(_ENV1, raising=False)
        monkeypatch.delenv(_ENV2, raising=False)

        sentinel = tmp_path / ".git" / "coordinator-sessions" / ".current-session-id"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("sess-from-sentinel-abc", encoding="utf-8")

        result = resolve_current_session_id(worktree_root=tmp_path)
        assert result is None

    def test_sentinel_missing_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv(_ENV1, raising=False)
        monkeypatch.delenv(_ENV2, raising=False)

        (tmp_path / ".git" / "coordinator-sessions").mkdir(parents=True)

        result = resolve_current_session_id(worktree_root=tmp_path)
        assert result is None


class TestNullPath:

    def test_null_when_no_env_no_sentinel(self, monkeypatch):
        monkeypatch.delenv(_ENV1, raising=False)
        monkeypatch.delenv(_ENV2, raising=False)
        result = resolve_current_session_id(worktree_root=None)
        assert result is None

    def test_null_when_worktree_root_none_and_no_env(self, monkeypatch):
        monkeypatch.delenv(_ENV1, raising=False)
        monkeypatch.delenv(_ENV2, raising=False)
        result = resolve_current_session_id(None)
        assert result is None
