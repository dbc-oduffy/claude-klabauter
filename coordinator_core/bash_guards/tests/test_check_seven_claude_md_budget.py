"""Tests for Check 7 (CLAUDE.md char budget) in
coordinator_core.bash_guards.dispatch_checks.check_validate_commit -- never
tested before this file (C1 re-siting, plan
docs/plans/2026-07-27-doctrine-envelope-allocation.md).

Covers:
    - Unification onto the coordinator_core.claude_md_budget SSOT
      (HARD_LIMIT_BYTES / SOFT_LIMIT_BYTES), replacing the two independent
      literal pairs this check and DoE's check-claude-md-size.py hook used
      to carry (40000/38000 here, 39900/39000 there).
    - Boundary values at, just under, and just over each threshold.
    - The GOVERNED-surface discriminant: a repo-scoped CLAUDE.md (this
      test repo's own root CLAUDE.md, without the dev-repo sentinel) must
      NOT be gated even when staged far over the hard limit -- the live
      bug a bare basename match produces.

Spec backlink: DoE-claude:pln-always-loaded-doctrine-envelop-cd5932 § C1(c)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.claude_md_budget import (
    DEV_REPO_SENTINEL,
    HARD_LIMIT_BYTES,
    SOFT_LIMIT_BYTES,
)
from coordinator_core.win_portability import no_console_creationflags

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, **no_console_creationflags())


def _init_repo(tmp_path: Path) -> str:
    root = str(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _stage_claude_md(root: str, relpath: str, size: int) -> None:
    target = Path(root) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x" * size, encoding="utf-8")
    _git(root, "add", relpath)


class TestCheckSevenThresholdsAreTheSSOT:
    def test_thresholds_match_the_ssot_module(self):
        assert HARD_LIMIT_BYTES == 40000
        assert SOFT_LIMIT_BYTES == 38000


class TestCheckSevenGovernedGlobalSurfaceBoundaries:

    def _init_dev_repo(self, tmp_path: Path) -> str:
        root = _init_repo(tmp_path)
        (Path(root) / DEV_REPO_SENTINEL).write_text("sentinel\n", encoding="utf-8")
        _git(root, "add", DEV_REPO_SENTINEL)
        _git(root, "commit", "-q", "-m", "seed dev-repo sentinel")
        return root

    def test_over_hard_limit_denies(self, tmp_path):
        root = self._init_dev_repo(tmp_path)
        _stage_claude_md(root, "coordinator/CLAUDE.md", HARD_LIMIT_BYTES + 1)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "CLAUDE.md" in out["permissionDecisionReason"]

    def test_at_exact_hard_limit_does_not_deny(self, tmp_path):
        root = self._init_dev_repo(tmp_path)
        _stage_claude_md(root, "coordinator/CLAUDE.md", HARD_LIMIT_BYTES)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is None or result["hookSpecificOutput"]["permissionDecision"] != "deny"

    def test_over_soft_limit_warns_but_allows(self, tmp_path):
        root = self._init_dev_repo(tmp_path)
        _stage_claude_md(root, "coordinator/CLAUDE.md", SOFT_LIMIT_BYTES + 1)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "CLAUDEMD-BUDGET" in out["additionalContext"]

    def test_at_exact_soft_limit_does_not_warn(self, tmp_path):
        root = self._init_dev_repo(tmp_path)
        _stage_claude_md(root, "coordinator/CLAUDE.md", SOFT_LIMIT_BYTES)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is None or "CLAUDEMD-BUDGET" not in result["hookSpecificOutput"].get(
            "additionalContext", ""
        )

    def test_just_under_soft_limit_is_clean(self, tmp_path):
        root = self._init_dev_repo(tmp_path)
        _stage_claude_md(root, "coordinator/CLAUDE.md", SOFT_LIMIT_BYTES - 1)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is None or "CLAUDEMD-BUDGET" not in result["hookSpecificOutput"].get(
            "additionalContext", ""
        )

    def test_override_env_downgrades_hard_deny_to_advisory(self, tmp_path, monkeypatch):
        root = self._init_dev_repo(tmp_path)
        _stage_claude_md(root, "coordinator/CLAUDE.md", HARD_LIMIT_BYTES + 1)

        monkeypatch.setenv("COORDINATOR_OVERRIDE_CLAUDEMD_BUDGET", "1")
        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "CLAUDEMD-BUDGET" in out["additionalContext"]


class TestCheckSevenRepoScopedSurfaceNeverGated:

    def test_repo_root_claude_md_ungated_even_huge(self, tmp_path):
        root = _init_repo(tmp_path)
        _stage_claude_md(root, "CLAUDE.md", HARD_LIMIT_BYTES + 50000)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump project doctrine"', "no-session", cwd=root
        )
        assert result is None or "CLAUDEMD-BUDGET" not in result["hookSpecificOutput"].get(
            "additionalContext", ""
        ) and result["hookSpecificOutput"]["permissionDecision"] != "deny"

    def test_coordinator_claude_md_without_sentinel_ungated_even_huge(self, tmp_path):
        root = _init_repo(tmp_path)
        _stage_claude_md(root, "coordinator/CLAUDE.md", HARD_LIMIT_BYTES + 50000)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is None or (
            "CLAUDEMD-BUDGET" not in result["hookSpecificOutput"].get("additionalContext", "")
            and result["hookSpecificOutput"]["permissionDecision"] != "deny"
        )


class TestCheckSevenAudienceManifestWidening:

    def _init_manifested_repo(self, tmp_path: Path) -> str:
        root = _init_repo(tmp_path)
        coord_dir = Path(root) / "coordinator"
        coord_dir.mkdir(parents=True, exist_ok=True)
        (coord_dir / "audience-manifest.txt").write_text(
            "coordinator/snippets/em-operating-doctrine.md\n", encoding="utf-8"
        )
        _git(root, "add", "coordinator/audience-manifest.txt")
        _git(root, "commit", "-q", "-m", "seed audience manifest")
        return root

    def _stage(self, root: str, relpath: str, size: int) -> None:
        target = Path(root) / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x" * size, encoding="utf-8")
        _git(root, "add", relpath)

    def test_manifest_named_non_claude_md_surface_is_gated_over_hard_limit(self, tmp_path):
        root = self._init_manifested_repo(tmp_path)
        self._stage(
            root,
            "coordinator/snippets/em-operating-doctrine.md",
            HARD_LIMIT_BYTES + 1,
        )

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump em doctrine"', "no-session", cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"

    def test_surface_absent_from_manifest_stays_ungated(self, tmp_path):
        root = self._init_manifested_repo(tmp_path)
        self._stage(
            root,
            "coordinator/snippets/unrelated-file.md",
            HARD_LIMIT_BYTES + 1,
        )

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump unrelated file"', "no-session", cwd=root
        )
        assert result is None or result["hookSpecificOutput"]["permissionDecision"] != "deny"


class TestCheckSevenRatchetWatermark:

    def _init_watermarked_repo(self, tmp_path: Path, bytes_val: int, reason: str) -> str:
        root = _init_repo(tmp_path)
        (Path(root) / DEV_REPO_SENTINEL).write_text("sentinel\n", encoding="utf-8")
        # "coordinator/CLAUDE.md" has no `_LEDGER_PATH_OVERRIDES` entry (that
        ledger_dir = Path(root) / "state" / "audits"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = ledger_dir / "coordinator-claude-classification.md"
        ledger_path.write_text(
            "## Classification table\n\n"
            "## Watermark\n"
            f"- Bytes: {bytes_val}\n"
            f"- Reason: {reason}\n",
            encoding="utf-8",
        )
        _git(root, "add", DEV_REPO_SENTINEL, "state/audits/coordinator-claude-classification.md")
        _git(root, "commit", "-q", "-m", "seed dev-repo sentinel + armed watermark")
        return root

    def test_growing_past_watermark_denied_even_under_hard_limit(self, tmp_path):
        root = self._init_watermarked_repo(tmp_path, bytes_val=6000, reason="post-cut arming")
        _stage_claude_md(root, "coordinator/CLAUDE.md", 6001)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "6000" in out["permissionDecisionReason"]

    def test_holding_at_watermark_allowed(self, tmp_path):
        root = self._init_watermarked_repo(tmp_path, bytes_val=6000, reason="post-cut arming")
        _stage_claude_md(root, "coordinator/CLAUDE.md", 6000)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is None or result["hookSpecificOutput"]["permissionDecision"] != "deny"

    def test_shrinking_under_watermark_allowed(self, tmp_path):
        root = self._init_watermarked_repo(tmp_path, bytes_val=6000, reason="post-cut arming")
        _stage_claude_md(root, "coordinator/CLAUDE.md", 100)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "bump doctrine"', "no-session", cwd=root
        )
        assert result is None or result["hookSpecificOutput"]["permissionDecision"] != "deny"

    def _commit_over_watermark_surface(self, tmp_path: Path, bytes_val: int, size: int) -> str:
        # C7c: seed a surface whose LAST-COMMITTED (HEAD) size is already
        root = self._init_watermarked_repo(tmp_path, bytes_val=bytes_val, reason="post-cut arming")
        _stage_claude_md(root, "coordinator/CLAUDE.md", size)
        _git(root, "commit", "-q", "-m", "land an over-watermark surface")
        return root

    def test_over_watermark_shrink_admitted(self, tmp_path):
        root = self._commit_over_watermark_surface(tmp_path, bytes_val=6000, size=8000)
        _stage_claude_md(root, "coordinator/CLAUDE.md", 7000)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "shrink doctrine"', "no-session", cwd=root
        )
        assert result is None or result["hookSpecificOutput"]["permissionDecision"] != "deny"

    def test_over_watermark_growth_still_denied(self, tmp_path):
        root = self._commit_over_watermark_surface(tmp_path, bytes_val=6000, size=8000)
        _stage_claude_md(root, "coordinator/CLAUDE.md", 8001)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "grow doctrine"', "no-session", cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "6000" in out["permissionDecisionReason"]

    def test_over_watermark_same_size_still_denied(self, tmp_path):
        root = self._commit_over_watermark_surface(tmp_path, bytes_val=6000, size=8000)
        target = Path(root) / "coordinator/CLAUDE.md"
        target.write_text("y" * 8000, encoding="utf-8")
        _git(root, "add", "coordinator/CLAUDE.md")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "no-op re-stage"', "no-session", cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "6000" in out["permissionDecisionReason"]
