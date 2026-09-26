
from __future__ import annotations

from pathlib import Path

from coordinator_core.claude_md_budget import DEV_REPO_SENTINEL, HARD_LIMIT_BYTES
from coordinator_core.write_guards import check_claude_md_size as guard


def _init_git_dir(root: Path) -> None:
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _write_payload(tool_name: str, file_path: str, **tool_input_extra):
    tool_input = {"file_path": file_path, **tool_input_extra}
    return {"tool_name": tool_name, "tool_input": tool_input}


class TestUngovernedSurfaceNeverDenied:
    def test_repo_root_claude_md_without_manifest_is_not_governed(self, tmp_path):
        _init_git_dir(tmp_path)
        target = tmp_path / "CLAUDE.md"

        result = guard.check(
            _write_payload("Write", str(target), content="x" * (HARD_LIMIT_BYTES + 500))
        )

        assert result is None


class TestFlatHardLimitStillApplies:
    def test_dev_repo_coordinator_claude_md_over_hard_limit_denied(self, tmp_path):
        _init_git_dir(tmp_path)
        (tmp_path / DEV_REPO_SENTINEL).write_text("sentinel", encoding="utf-8")
        coord_dir = tmp_path / "coordinator"
        coord_dir.mkdir()
        target = coord_dir / "CLAUDE.md"

        result = guard.check(
            _write_payload("Write", str(target), content="x" * (HARD_LIMIT_BYTES + 1))
        )

        assert result is not None
        out = result["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert "additionalContext" in out

    def test_dev_repo_coordinator_claude_md_under_hard_limit_allowed(self, tmp_path):
        _init_git_dir(tmp_path)
        (tmp_path / DEV_REPO_SENTINEL).write_text("sentinel", encoding="utf-8")
        coord_dir = tmp_path / "coordinator"
        coord_dir.mkdir()
        target = coord_dir / "CLAUDE.md"

        result = guard.check(_write_payload("Write", str(target), content="x" * 10))

        assert result is None


class TestAudienceManifestWidensGovernance:
    def test_manifest_named_surface_of_any_filename_becomes_governed(self, tmp_path):
        _init_git_dir(tmp_path)
        coord_dir = tmp_path / "coordinator"
        coord_dir.mkdir()
        (coord_dir / "audience-manifest.txt").write_text(
            "coordinator/snippets/em-operating-doctrine.md\n", encoding="utf-8"
        )
        snippets_dir = coord_dir / "snippets"
        snippets_dir.mkdir()
        target = snippets_dir / "em-operating-doctrine.md"

        result = guard.check(
            _write_payload("Write", str(target), content="x" * (HARD_LIMIT_BYTES + 1))
        )

        assert result is not None
        out = result["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert "additionalContext" in out

    def test_surface_absent_from_manifest_stays_ungoverned(self, tmp_path):
        _init_git_dir(tmp_path)
        coord_dir = tmp_path / "coordinator"
        coord_dir.mkdir()
        (coord_dir / "audience-manifest.txt").write_text(
            "coordinator/snippets/em-operating-doctrine.md\n", encoding="utf-8"
        )
        snippets_dir = coord_dir / "snippets"
        snippets_dir.mkdir()
        unrelated = snippets_dir / "some-other-file.md"

        result = guard.check(
            _write_payload("Write", str(unrelated), content="x" * (HARD_LIMIT_BYTES + 1))
        )

        assert result is None


class TestRatchetWatermarkEnforced:
    def _governed_target(self, tmp_path: Path) -> Path:
        _init_git_dir(tmp_path)
        coord_dir = tmp_path / "coordinator"
        coord_dir.mkdir()
        (coord_dir / "audience-manifest.txt").write_text(
            "global-doctrine/CLAUDE.md\n", encoding="utf-8"
        )
        global_dir = tmp_path / "global-doctrine"
        global_dir.mkdir()
        return global_dir / "CLAUDE.md"

    def _arm_watermark(self, tmp_path: Path, bytes_val: int, reason: str) -> None:
        ledger_dir = tmp_path / "state" / "audits"
        ledger_dir.mkdir(parents=True)
        ledger_path = ledger_dir / "2026-07-27-doctrine-envelope-classification.md"
        ledger_path.write_text(
            "## Classification table\n\n"
            "## Watermark\n"
            f"- Bytes: {bytes_val}\n"
            f"- Reason: {reason}\n",
            encoding="utf-8",
        )

    def test_unarmed_ratchet_only_the_flat_hard_limit_applies(self, tmp_path):
        target = self._governed_target(tmp_path)

        result = guard.check(_write_payload("Write", str(target), content="x" * 100))

        assert result is None

    def test_growing_past_armed_watermark_denied_even_under_hard_limit(self, tmp_path):
        target = self._governed_target(tmp_path)
        self._arm_watermark(tmp_path, bytes_val=6000, reason="post-cut arming, C7b")

        result = guard.check(_write_payload("Write", str(target), content="x" * 6001))

        assert result is not None
        out = result["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert "6000" in out["additionalContext"]
        assert "post-cut arming, C7b" in out["additionalContext"]

    def test_holding_at_armed_watermark_allowed(self, tmp_path):
        target = self._governed_target(tmp_path)
        self._arm_watermark(tmp_path, bytes_val=6000, reason="post-cut arming, C7b")

        result = guard.check(_write_payload("Write", str(target), content="x" * 6000))

        assert result is None

    def test_shrinking_under_armed_watermark_allowed(self, tmp_path):
        target = self._governed_target(tmp_path)
        self._arm_watermark(tmp_path, bytes_val=6000, reason="post-cut arming, C7b")

        result = guard.check(_write_payload("Write", str(target), content="x" * 100))

        assert result is None

    def test_malformed_watermark_does_not_deny_and_names_ledger_path(self, tmp_path, capsys):
        # COORDINATOR_OVERRIDE_* keys, so a hard deny here had no
        target = self._governed_target(tmp_path)
        ledger_dir = tmp_path / "state" / "audits"
        ledger_dir.mkdir(parents=True)
        ledger_path = ledger_dir / "2026-07-27-doctrine-envelope-classification.md"
        ledger_path.write_text("## Watermark\n- Bytes: 6000\n", encoding="utf-8")

        result = guard.check(_write_payload("Write", str(target), content="x" * 10))

        assert result is None
        err = capsys.readouterr().err
        assert str(ledger_path) in err

    def test_malformed_watermark_hard_limit_breach_still_denies(self, tmp_path):
        # OTHER, unrelated deny leg -- an actual HARD_LIMIT_BYTES breach
        target = self._governed_target(tmp_path)
        ledger_dir = tmp_path / "state" / "audits"
        ledger_dir.mkdir(parents=True)
        ledger_path = ledger_dir / "2026-07-27-doctrine-envelope-classification.md"
        ledger_path.write_text("## Watermark\n- Bytes: 6000\n", encoding="utf-8")

        result = guard.check(
            _write_payload("Write", str(target), content="x" * (HARD_LIMIT_BYTES + 1))
        )

        assert result is not None
        out = result["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert "additionalContext" in out

    def test_over_watermark_shrink_is_admitted(self, tmp_path):
        target = self._governed_target(tmp_path)
        self._arm_watermark(tmp_path, bytes_val=6000, reason="post-cut arming, C7b")
        target.write_text("x" * 8000, encoding="utf-8")

        result = guard.check(_write_payload("Write", str(target), content="x" * 7000))

        assert result is None

    def test_over_watermark_growth_is_still_denied(self, tmp_path):
        target = self._governed_target(tmp_path)
        self._arm_watermark(tmp_path, bytes_val=6000, reason="post-cut arming, C7b")
        target.write_text("x" * 8000, encoding="utf-8")

        result = guard.check(_write_payload("Write", str(target), content="x" * 8001))

        assert result is not None
        out = result["hookSpecificOutput"]
        assert "6000" in out["additionalContext"]

    def test_over_watermark_same_size_is_still_denied(self, tmp_path):
        target = self._governed_target(tmp_path)
        self._arm_watermark(tmp_path, bytes_val=6000, reason="post-cut arming, C7b")
        target.write_text("x" * 8000, encoding="utf-8")

        result = guard.check(_write_payload("Write", str(target), content="x" * 8000))

        assert result is not None
        out = result["hookSpecificOutput"]
        assert "6000" in out["additionalContext"]

    def test_over_watermark_shrink_via_edit_is_admitted(self, tmp_path):
        target = self._governed_target(tmp_path)
        self._arm_watermark(tmp_path, bytes_val=6000, reason="post-cut arming, C7b")
        target.write_text("PREFIX" + ("x" * 8000), encoding="utf-8")

        result = guard.check(
            _write_payload(
                "Edit", str(target), old_string="x" * 8000, new_string="x" * 6500
            )
        )

        assert result is None

    def test_genuine_ratchet_failure_still_denies(self, tmp_path):
        # as before -- only the PARSE-FAILURE branch changed.
        target = self._governed_target(tmp_path)
        self._arm_watermark(tmp_path, bytes_val=6000, reason="post-cut arming, C7b")

        result = guard.check(_write_payload("Write", str(target), content="x" * 6001))

        assert result is not None
        out = result["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert "6000" in out["additionalContext"]
