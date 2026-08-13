"""Unit tests for resolve_repo_name() and EmitContext.resolve() — C1 attribution reversal.

Verifies the per-repo-emission-cutover (2026-07-07) invariants on the context module:
  1. resolve_repo_name() resolves the emitting-repo slug from a fixture git remote.
  2. resolve_repo_name() returns ``local/<basename>`` when no parseable remote exists
     but repo_root IS a valid directory (air-gapped / local-only repos stay observable).
  3. resolve_repo_name() raises RuntimeError for an UNDERIVABLE repo_root (None or
     non-existent path) — the corruption/aliasing guard.
  4. EmitContext.resolve() succeeds (returns local/<basename>) when no remote exists.
  5. META_REPO_NAME_FALLBACK is NOT used as a runtime fallback (the constant's value is only
     reached by a repo whose real origin URL resolves to that slug).

No-rename gate note: uses positional constructor args throughout to keep the
coordinator_core/ grep-count for the state-root field identifier unchanged (value-only
edit — no rename permitted per-repo-emission-cutover gate spec).

Spec backlink: pln-per-repo-emission-cutover-un-h-03f05e § C1 / AC5
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.emit.context import (
    META_REPO_NAME_FALLBACK,
    EmitContext,
    _remote_url_to_slug,
    resolve_repo_name,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_SHA = "a" * 40


def _make_ctx(repo_root: Path) -> EmitContext:
    """Minimal EmitContext via positional constructor args (no live git required)."""
    # Field order: repo_root, coordinator_root, state_root, git_branch, git_sha,
    # git_sha_short, observed_at, hostname, repo_name
    return EmitContext(
        repo_root,
        repo_root,
        repo_root / "state",
        "test-branch",
        _FAKE_SHA,
        _FAKE_SHA[:8],
        "2026-07-07T00:00:00Z",
        "test-host",
        "dbc-oduffy/test-repo",
    )


# ---------------------------------------------------------------------------
# Tests for _remote_url_to_slug (unit)
# ---------------------------------------------------------------------------

class TestRemoteUrlToSlug:
    """_remote_url_to_slug parses common git remote URL forms."""

    def test_ssh_form(self) -> None:
        assert _remote_url_to_slug("git@github.com:dbc-oduffy/.example-doctrine-mirror-repo.git") == "dbc-oduffy/.example-doctrine-mirror-repo"

    def test_https_form(self) -> None:
        assert _remote_url_to_slug("https://github.com/dbc-oduffy/claude-klabauter") == "dbc-oduffy/claude-klabauter"

    def test_https_form_with_git_suffix(self) -> None:
        assert _remote_url_to_slug("https://github.com/dbc-oduffy/claude-klabauter.git") == "dbc-oduffy/claude-klabauter"

    def test_empty_string_returns_none(self) -> None:
        assert _remote_url_to_slug("") is None

    def test_unparseable_returns_none(self) -> None:
        assert _remote_url_to_slug("not-a-url") is None

    def test_single_segment_url_returns_none(self) -> None:
        """A URL with only one path segment (no owner prefix) returns None — not a valid slug.

        e.g. https://github.com/myrepo — no owner component; returning a bare name would
        be silently mis-keyed in rag (expects owner/repo format).
        Review: code-reviewer (Slice-1 F3) — single-segment was returning parts[-1]; now returns None.
        """
        assert _remote_url_to_slug("https://github.com/myrepo") is None

    def test_gitlab_three_segment_url_returns_last_two(self) -> None:
        """A URL with 3+ path segments (GitLab subgroup) returns the last two segments.

        This is a documented truncation — group/subgroup/repo → subgroup/repo.  The result
        is plausible but not uniquely identifying; the coordinator_root_path field (AC12)
        is the disambiguation anchor.  The test documents the known behaviour, not silence it.
        Review: code-reviewer (Slice-1 F4) — documents GitLab truncation (not a regression net for correctness).
        """
        assert _remote_url_to_slug("https://gitlab.company.com/group/subgroup/repo") == "subgroup/repo"


# ---------------------------------------------------------------------------
# Tests for resolve_repo_name — slug from fixture remote
# ---------------------------------------------------------------------------

class TestResolveRepoName:
    """resolve_repo_name() resolves slug from the emitting repo's git remote."""

    def test_resolves_slug_from_ssh_remote(self, tmp_path: Path) -> None:
        """Slug is resolved correctly from an SSH-form git remote URL."""
        fixture_url = "git@github.com:dbc-oduffy/claude-klabauter.git"

        with patch("coordinator_core.ops.emit.context._run_git", return_value=fixture_url):
            result = resolve_repo_name(tmp_path)

        assert result == "dbc-oduffy/claude-klabauter"

    def test_resolves_slug_from_https_remote(self, tmp_path: Path) -> None:
        """Slug is resolved correctly from an HTTPS-form git remote URL.

        Also verifies that ~/.claude's emission reaches META_REPO_NAME_FALLBACK's value via
        the NORMAL resolution path (its own origin remote), not via any catch clause.
        """
        fixture_url = "https://github.com/dbc-oduffy/.example-doctrine-mirror-repo"

        with patch("coordinator_core.ops.emit.context._run_git", return_value=fixture_url):
            result = resolve_repo_name(tmp_path)

        assert result == META_REPO_NAME_FALLBACK

    def test_local_slug_when_no_remote(self, tmp_path: Path) -> None:
        """Returns local/<basename> when the git remote cannot be resolved (air-gapped repo).

        Valid dir + no remote → local-slug (not a raise).  Air-gapped repos must stay
        observable (per-repo-emission-cutover AC5 Q-B hybrid).
        """
        with patch("coordinator_core.ops.emit.context._run_git", return_value=None):
            result = resolve_repo_name(tmp_path)

        assert result == f"local/{tmp_path.name}"

    def test_local_slug_when_remote_url_unparseable(self, tmp_path: Path) -> None:
        """Returns local/<basename> when the remote URL cannot be parsed into a slug.

        Unparseable URL falls through _remote_url_to_slug → None → local slug (not a raise).
        """
        with patch("coordinator_core.ops.emit.context._run_git", return_value="not-a-valid-remote-url"):
            result = resolve_repo_name(tmp_path)

        assert result == f"local/{tmp_path.name}"

    def test_does_not_fall_back_to_meta_repo_fallback(self, tmp_path: Path) -> None:
        """On no-remote, resolve_repo_name returns local/<basename>, NEVER META_REPO_NAME_FALLBACK.

        Uses a directory literally named `.example-doctrine-mirror-repo` so the non-equality assertion is
        load-bearing — if the code ever returned META_REPO_NAME_FALLBACK as a runtime catch,
        this test would catch it (unlike a hex-UUID tmp_path whose name can never match).
        Review: code-reviewer (Slice-4 F9) — previous form used pytest hex-UUID tmp_path; assertion was trivially true.
        """
        fake_dir = tmp_path / ".example-doctrine-mirror-repo"
        fake_dir.mkdir()

        with patch("coordinator_core.ops.emit.context._run_git", return_value=None):
            result = resolve_repo_name(fake_dir)

        assert result == "local/.example-doctrine-mirror-repo", (
            "local-slug for a .example-doctrine-mirror-repo dir must be local/.example-doctrine-mirror-repo"
        )
        assert result != META_REPO_NAME_FALLBACK, (
            "local-slug must never silently alias to the meta-repo fallback constant; "
            "META_REPO_NAME_FALLBACK is only reached via normal remote resolution"
        )

    def test_error_message_mentions_per_repo_cutover(self, tmp_path: Path) -> None:
        """RuntimeError for UNDERIVABLE repo_root references AC5 for diagnosability.

        Only an underivable repo_root (non-existent path or None) raises; the no-remote
        path (valid dir, no remote) returns local/<basename>.
        """
        non_existent = tmp_path / "does-not-exist"
        with pytest.raises(RuntimeError, match="AC5"):
            resolve_repo_name(non_existent)


# ---------------------------------------------------------------------------
# Tests for EmitContext.resolve() — propagates fail-loud
# ---------------------------------------------------------------------------

class TestEmitContextResolve:
    """EmitContext.resolve() succeeds with local/<basename> when no remote is present."""

    def test_resolve_succeeds_with_local_slug_when_repo_has_no_remote(self, tmp_path: Path) -> None:
        """EmitContext.resolve() returns ctx.repo_name == local/<basename> when no remote.

        A valid repo_root with no parseable git remote is a local-only / air-gapped repo —
        resolve_repo_name returns local/<basename>; EmitContext.resolve must NOT raise.
        """
        def _fake_run_git(repo_root: Path, *args: str):
            if "remote" in args:
                return None
            if "--abbrev-ref" in args:
                return "main"
            if "rev-parse" in args:
                return "b" * 40
            return None

        with patch("coordinator_core.ops.emit.context._run_git", side_effect=_fake_run_git):
            # Positional args — see module docstring (no-rename gate note).
            ctx = EmitContext.resolve(tmp_path, tmp_path, tmp_path / "state")

        assert ctx.repo_name == f"local/{tmp_path.name}"
        # D7a regression guard: a regression back to coordinator_root/state would fail this.
        # Review: code-reviewer (Slice-4 F5) — central_state_root must be repo_root/state, not ~/.claude/state.
        assert ctx.central_state_root == tmp_path / "state"

    def test_resolve_succeeds_with_valid_remote(self, tmp_path: Path) -> None:
        """EmitContext.resolve() builds a context with the emitting-repo slug on success."""
        fixture_url = "git@github.com:dbc-oduffy/claude-klabauter.git"

        def _fake_run_git(repo_root: Path, *args: str):
            if "remote" in args:
                return fixture_url
            if "--abbrev-ref" in args:
                return "main"
            if "rev-parse" in args:
                return "c" * 40
            return None

        with patch("coordinator_core.ops.emit.context._run_git", side_effect=_fake_run_git):
            ctx = EmitContext.resolve(tmp_path, tmp_path, tmp_path / "state")

        assert ctx.repo_name == "dbc-oduffy/claude-klabauter"
        assert ctx.repo_root == tmp_path
        assert ctx.git_branch == "main"
        assert len(ctx.git_sha) == 40
        # D7a regression guard: a regression back to coordinator_root/state would fail this.
        # Review: code-reviewer (Slice-4 F5) — central_state_root must be repo_root/state, not ~/.claude/state.
        assert ctx.central_state_root == tmp_path / "state"


# ---------------------------------------------------------------------------
# Guard: META_REPO_NAME_FALLBACK is reached via normal resolution, not a catch
# ---------------------------------------------------------------------------

class TestMetaRepoFallbackIsOracleOnly:
    """META_REPO_NAME_FALLBACK holds the expected slug value but is never a runtime default."""

    def test_fallback_constant_holds_example_doctrine_mirror_repo_slug(self) -> None:
        """The constant's value matches the expected ~/.claude origin slug."""
        assert META_REPO_NAME_FALLBACK == "dbc-oduffy/.example-doctrine-mirror-repo"

    def test_resolve_repo_name_reaches_fallback_value_via_normal_resolution(
        self, tmp_path: Path
    ) -> None:
        """A repo whose origin URL resolves to the fallback value gets that slug normally."""
        fixture_url = f"git@github.com:{META_REPO_NAME_FALLBACK}.git"

        with patch("coordinator_core.ops.emit.context._run_git", return_value=fixture_url):
            result = resolve_repo_name(tmp_path)

        assert result == META_REPO_NAME_FALLBACK, (
            "Meta-repo slug is reached via normal remote-resolution, not as a catch fallback"
        )
