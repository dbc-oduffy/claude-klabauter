"""Tests for the W4-C4 arrival footprint of `coordinator_core.hooks.support`.

Scope: the nine modules W4-C4's `writes:` list names
(docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C4) --
`git_common_dir`, `touch_record`, `bin_impl_drift`, `plan_path_bridge`,
`worktree_isolation_strip`, `named_dispatch_strip`, `foreground_dispatch_strip`,
`posture`, `next_move_ledger`. These exercise the DECISION/COMPUTATION shape
of each module directly (never a real registered hook body -- W4-C5/C6 land
those), matching W4-C3's own `test_support_runners.py` precedent.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.hooks.support import (
    bin_impl_drift,
    foreground_dispatch_strip,
    git_common_dir,
    named_dispatch_strip,
    next_move_ledger,
    plan_path_bridge,
    posture,
    touch_record,
    worktree_isolation_strip,
)


def _init_git_dir(root, common_dir_name=".git"):
    (root / common_dir_name).mkdir()
    return root / common_dir_name


# ---------------------------------------------------------------------------
# git_common_dir
# ---------------------------------------------------------------------------


class TestGitCommonDir:
    def test_plain_clone_returns_dot_git_dir(self, tmp_path):
        _init_git_dir(tmp_path)
        result = git_common_dir.resolve_git_common_dir(str(tmp_path))
        assert result == os.path.join(str(tmp_path), ".git")

    def test_missing_git_entry_returns_empty_string(self, tmp_path):
        assert git_common_dir.resolve_git_common_dir(str(tmp_path)) == ""

    def test_worktree_gitdir_file_resolves_common_dir_via_commondir(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        main_git = main / ".git"
        main_git.mkdir()

        worktree = tmp_path / "wt"
        worktree.mkdir()
        private_gitdir = main_git / "worktrees" / "wt"
        private_gitdir.mkdir(parents=True)
        (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        (worktree / ".git").write_text(f"gitdir: {private_gitdir}\n", encoding="utf-8")

        result = git_common_dir.resolve_git_common_dir(str(worktree))
        assert os.path.normpath(result) == os.path.normpath(str(main_git))

    def test_malformed_gitdir_file_fails_open_to_empty_string(self, tmp_path):
        (tmp_path / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        assert git_common_dir.resolve_git_common_dir(str(tmp_path)) == ""

    def test_never_raises_on_garbage_input(self):
        assert git_common_dir.resolve_git_common_dir("\x00bad") == ""


# ---------------------------------------------------------------------------
# touch_record
# ---------------------------------------------------------------------------


class TestTouchRecord:
    def test_touch_lines_reads_new_file_before_legacy(self, tmp_path):
        git_dir = _init_git_dir(tmp_path)
        session_dir = git_dir / "coordinator-sessions" / "sid1"
        session_dir.mkdir(parents=True)
        (session_dir / "touch-record.jsonl").write_text(
            json.dumps({"verb": "T", "path": "new.py"}) + "\n", encoding="utf-8"
        )
        (session_dir / "touched.txt").write_text("T 100 legacy.py\n", encoding="utf-8")

        lines = touch_record._touch_lines(str(git_dir), "sid1")
        assert lines == ["new.py", "legacy.py"]

    def test_touch_lines_missing_files_returns_empty(self, tmp_path):
        git_dir = _init_git_dir(tmp_path)
        assert touch_record._touch_lines(str(git_dir), "no-such-session") == []

    def test_touch_record_jsonl_skips_malformed_and_pathless_rows(self, tmp_path):
        git_dir = _init_git_dir(tmp_path)
        session_dir = git_dir / "coordinator-sessions" / "sid2"
        session_dir.mkdir(parents=True)
        (session_dir / "touch-record.jsonl").write_text(
            "not json\n" + json.dumps({"verb": "T"}) + "\n" + json.dumps({"path": "ok.py"}) + "\n",
            encoding="utf-8",
        )
        assert touch_record._touch_record_jsonl_paths(str(session_dir)) == ["ok.py"]

    def test_touch_path_legacy_line_shapes(self):
        assert touch_record._touch_path("T 100 foo/bar.py") == "foo/bar.py"
        assert touch_record._touch_path("bare/path.py") == "bare/path.py"
        assert touch_record._touch_path("   ") is None


# ---------------------------------------------------------------------------
# bin_impl_drift
# ---------------------------------------------------------------------------


class TestBinImplDrift:
    def test_no_plugin_root_env_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        assert bin_impl_drift.check_and_refresh(bin_dir) is None

    def test_refreshes_drifted_installed_file_from_plugin_root(self, tmp_path, monkeypatch):
        plugin_root = tmp_path / "plugin"
        templates_bin = plugin_root / "templates" / "bin"
        templates_bin.mkdir(parents=True)
        (templates_bin / "machine-local").write_text("NEW\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        bin_dir = tmp_path / "settings-home" / "bin"
        bin_dir.mkdir(parents=True)
        installed = bin_dir / "machine-local"
        installed.write_text("OLD\n", encoding="utf-8")
        os.chmod(installed, 0o755)

        banner = bin_impl_drift.check_and_refresh(bin_dir, now=1000.0)
        assert banner is not None
        assert "machine-local" in banner
        assert installed.read_text(encoding="utf-8") == "NEW\n"
        assert oct(installed.stat().st_mode)[-3:] == "755"

    def test_never_seeds_a_file_not_already_installed(self, tmp_path, monkeypatch):
        plugin_root = tmp_path / "plugin"
        templates_bin = plugin_root / "templates" / "bin"
        templates_bin.mkdir(parents=True)
        (templates_bin / "brand-new-tool").write_text("X\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        assert bin_impl_drift.check_and_refresh(bin_dir, now=1000.0) is None
        assert not (bin_dir / "brand-new-tool").exists()

    def test_never_overwrites_a_native_image(self, tmp_path, monkeypatch):
        plugin_root = tmp_path / "plugin"
        templates_bin = plugin_root / "templates" / "bin"
        templates_bin.mkdir(parents=True)
        (templates_bin / "door").write_text("#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        door = bin_dir / "door"
        door.write_bytes(b"\x7fELF" + b"\x00" * 20)

        assert bin_impl_drift.check_and_refresh(bin_dir, now=1000.0) is None
        assert door.read_bytes().startswith(b"\x7fELF")

    def test_claim_interval_skips_within_window(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))  # no templates/bin -> no-op anyway
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stamp = bin_dir / bin_impl_drift._STAMP_BASENAME
        stamp.write_text("0\n", encoding="utf-8")
        os.utime(stamp, (0, 0))
        assert bin_impl_drift._claim_interval(stamp, now=100.0) is False
        assert bin_impl_drift._claim_interval(stamp, now=100000.0) is True


# ---------------------------------------------------------------------------
# plan_path_bridge
# ---------------------------------------------------------------------------


class TestPlanPathBridge:
    def test_extract_plan_path_finds_docs_plans_citation(self):
        assert (
            plan_path_bridge.extract_plan_path("see docs/plans/2026-01-01-foo.md for detail")
            == "docs/plans/2026-01-01-foo.md"
        )

    def test_extract_plan_path_rejects_url_prefixed_candidate(self):
        assert (
            plan_path_bridge.extract_plan_path("https://example.com/docs/plans/foo.md") is None
        )

    def test_extract_plan_path_none_when_absent(self):
        assert plan_path_bridge.extract_plan_path("nothing relevant here") is None
        assert plan_path_bridge.extract_plan_path("") is None

    def test_record_and_read_plan_path_round_trip(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        session_id = "sidA"
        subagent_type = next(iter(plan_path_bridge.PLAN_DERIVABLE_TYPES))
        assert plan_path_bridge.record_plan_path(
            session_id, subagent_type, "docs/plans/x.md", cwd=str(tmp_path)
        )
        assert (
            plan_path_bridge.read_plan_path(session_id, subagent_type, cwd=str(tmp_path))
            == "docs/plans/x.md"
        )

    def test_record_plan_path_rejects_non_plan_derivable_type(self, tmp_path):
        _init_git_dir(tmp_path)
        assert not plan_path_bridge.record_plan_path(
            "sidB", "coordinator:not-a-lens", "docs/plans/x.md", cwd=str(tmp_path)
        )

    def test_read_plan_path_none_outside_repo(self, tmp_path):
        subagent_type = next(iter(plan_path_bridge.PLAN_DERIVABLE_TYPES))
        assert plan_path_bridge.read_plan_path(subagent_type, subagent_type, cwd=str(tmp_path)) is None


# ---------------------------------------------------------------------------
# worktree_isolation_strip
# ---------------------------------------------------------------------------


class TestWorktreeIsolationStrip:
    def test_compute_strip_removes_worktree_isolation(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        tool_input = {"subagent_type": "coordinator:executor", "isolation": "worktree", "prompt": "x"}
        result = worktree_isolation_strip.compute_strip(tool_input)
        assert result is not None
        merged, note = result
        assert "isolation" not in merged
        assert merged["prompt"] == "x"
        assert "worktree" in note.lower()
        # Original input is untouched (full-copy contract).
        assert tool_input["isolation"] == "worktree"

    def test_compute_strip_passes_through_non_worktree_isolation(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        tool_input = {"isolation": "remote"}
        assert worktree_isolation_strip.compute_strip(tool_input) is None

    def test_compute_strip_none_when_isolation_absent(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert worktree_isolation_strip.compute_strip({}) is None

    def test_override_sentinel_suppresses_strip(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        (tmp_path / worktree_isolation_strip._OVERRIDE_SENTINEL_NAME).write_text(
            "", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        assert worktree_isolation_strip.compute_strip({"isolation": "worktree"}) is None

    def test_git_root_none_outside_a_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert worktree_isolation_strip.sentinel_override_active() is False


# ---------------------------------------------------------------------------
# named_dispatch_strip
# ---------------------------------------------------------------------------


class TestNamedDispatchStrip:
    def test_none_when_subagent_type_out_of_scope(self):
        assert named_dispatch_strip.compute_named_dispatch_result(
            {"subagent_type": "coordinator:git-commit-agent", "name": "x"}
        ) is None

    def test_none_when_name_absent(self):
        assert named_dispatch_strip.compute_named_dispatch_result(
            {"subagent_type": "Explore"}
        ) is None

    def test_restricted_type_strips_name(self):
        result = named_dispatch_strip.compute_named_dispatch_result(
            {"subagent_type": "Explore", "prompt": "x", "name": "scout"}
        )
        assert result is not None
        verdict, merged, message = result
        assert verdict == "strip"
        assert "name" not in merged
        assert "Explore" in message

    def test_reporting_type_strips_name_too(self):
        reporting_type = named_dispatch_strip._REPORTING_SUBAGENT_TYPES[0]
        result = named_dispatch_strip.compute_named_dispatch_result(
            {"subagent_type": reporting_type, "prompt": "x", "name": "teammate"}
        )
        assert result is not None
        verdict, merged, _ = result
        assert verdict == "strip"
        assert "name" not in merged

    def test_staff_session_carve_out_passes_through_named(self):
        carved = named_dispatch_strip._STAFF_SESSION_NAMING_CARVE_OUT[0]
        assert named_dispatch_strip.compute_named_dispatch_result(
            {"subagent_type": carved, "prompt": "x", "name": "persona"}
        ) is None

    def test_deep_research_carve_out_passes_through_named(self):
        carved = named_dispatch_strip._DEEP_RESEARCH_NAMING_CARVE_OUT[0]
        assert named_dispatch_strip.compute_named_dispatch_result(
            {"subagent_type": carved, "prompt": "x", "name": "specialist"}
        ) is None

    def test_restricted_type_unknown_key_denies_fail_closed(self):
        result = named_dispatch_strip.compute_named_dispatch_result(
            {"subagent_type": "Explore", "name": "scout", "some_unknown_key": 1}
        )
        assert result is not None
        verdict, merged, _ = result
        assert verdict == "deny"
        assert merged is None

    def test_reporting_type_unknown_key_fails_open_to_none(self):
        reporting_type = named_dispatch_strip._REPORTING_SUBAGENT_TYPES[0]
        assert named_dispatch_strip.compute_named_dispatch_result(
            {"subagent_type": reporting_type, "name": "x", "some_unknown_key": 1}
        ) is None


# ---------------------------------------------------------------------------
# foreground_dispatch_strip
# ---------------------------------------------------------------------------


class TestForegroundDispatchStrip:
    def test_present_true_is_noop(self, tmp_path):
        assert foreground_dispatch_strip.compute_foreground_reroute(
            True, "sid-1234", {"prompt": "x"}, str(tmp_path)
        ) is None

    def test_present_false_reroutes_to_background(self, tmp_path):
        _init_git_dir(tmp_path)
        result = foreground_dispatch_strip.compute_foreground_reroute(
            False, "sid-1234", {"prompt": "x"}, str(tmp_path)
        )
        assert result is not None
        verdict, value, _ = result
        assert verdict == "reroute"
        assert value is True

    def test_present_false_without_prompt_denies(self, tmp_path):
        _init_git_dir(tmp_path)
        result = foreground_dispatch_strip.compute_foreground_reroute(
            False, "sid-1234", {}, str(tmp_path)
        )
        assert result is not None
        verdict, value, _ = result
        assert verdict == "deny"
        assert value is None

    def test_absent_uncalibrated_is_noop(self, tmp_path):
        _init_git_dir(tmp_path)
        assert foreground_dispatch_strip.compute_foreground_reroute(
            None, "sid-calib-1", {"prompt": "x"}, str(tmp_path)
        ) is None

    def test_absent_calibrated_reroutes(self, tmp_path):
        _init_git_dir(tmp_path)
        sid = "abcdef12-3456-7890-abcd-ef1234567890"
        # Calibrate first: presence proves the harness exposes the param.
        foreground_dispatch_strip.compute_foreground_reroute(
            True, sid, {"prompt": "x"}, str(tmp_path)
        )
        result = foreground_dispatch_strip.compute_foreground_reroute(
            None, sid, {"prompt": "x"}, str(tmp_path)
        )
        assert result is not None
        assert result[0] == "reroute"

    def test_foreground_ok_escape_hatch_suppresses_reroute(self, tmp_path):
        git_dir = _init_git_dir(tmp_path)
        sid = "sid-escape"
        session_dir = git_dir / "coordinator-sessions" / sid
        session_dir.mkdir(parents=True)
        (session_dir / foreground_dispatch_strip._FOREGROUND_OK_MARKER_NAME).write_text(
            "", encoding="utf-8"
        )
        assert foreground_dispatch_strip.compute_foreground_reroute(
            False, sid, {"prompt": "x"}, str(tmp_path)
        ) is None

    def test_unresolvable_cwd_fails_open_to_none_on_absent(self, tmp_path):
        assert foreground_dispatch_strip.compute_foreground_reroute(
            None, "sid-x", {"prompt": "x"}, "/no/such/path"
        ) is None


# ---------------------------------------------------------------------------
# posture
# ---------------------------------------------------------------------------


class TestPosture:
    def _reset_cache(self, monkeypatch):
        monkeypatch.setattr(posture, "_cached_posture", None)
        posture._cached_posture_by_root.clear()

    def test_resolve_posture_from_explicit_repo_root(self, tmp_path, monkeypatch):
        self._reset_cache(monkeypatch)
        (tmp_path / "coordinator.local.md").write_text(
            "---\nengagement_posture: default\n---\n", encoding="utf-8"
        )
        assert posture.resolve_posture(repo_root=str(tmp_path)) == "default"

    def test_resolve_posture_invalid_value_falls_through_to_identity_file(
        self, tmp_path, monkeypatch
    ):
        self._reset_cache(monkeypatch)
        (tmp_path / "coordinator.local.md").write_text(
            "engagement_posture: not-a-real-posture\n", encoding="utf-8"
        )
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "coordinator-identity.yaml").write_text(
            "engagement_posture: substrate-free\n", encoding="utf-8"
        )
        monkeypatch.setenv("CLAUDE_HOME", str(home))
        assert posture.resolve_posture(repo_root=str(tmp_path)) == "substrate-free"

    def test_resolve_posture_fails_open_to_precision(self, tmp_path, monkeypatch):
        self._reset_cache(monkeypatch)
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "no-such-home"))
        assert posture.resolve_posture(repo_root=str(tmp_path)) == "precision"

    def test_resolve_posture_caches_per_explicit_root(self, tmp_path, monkeypatch):
        self._reset_cache(monkeypatch)
        (tmp_path / "coordinator.local.md").write_text(
            "engagement_posture: default\n", encoding="utf-8"
        )
        first = posture.resolve_posture(repo_root=str(tmp_path))
        (tmp_path / "coordinator.local.md").write_text(
            "engagement_posture: substrate-free\n", encoding="utf-8"
        )
        # Cached: the second read does not reflect the file changing underfoot.
        second = posture.resolve_posture(repo_root=str(tmp_path))
        assert first == second == "default"


# ---------------------------------------------------------------------------
# next_move_ledger
# ---------------------------------------------------------------------------


class TestNextMoveLedger:
    def test_open_read_progress_discharge_lifecycle(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        sid = "ledger-sid-1"

        assert next_move_ledger.open_obligation(sid, "ob-1", "seam-x", "do the thing")
        records = next_move_ledger.read_records(sid)
        assert len(records) == 1
        assert records[0]["discharged_at"] is None

        # Re-opening the same obligation_id while still open is a no-op.
        assert not next_move_ledger.open_obligation(sid, "ob-1", "seam-x", "do the thing")

        assert next_move_ledger.progress_obligation(sid, "ob-1")
        assert next_move_ledger.read_records(sid)[0]["progressed_at"] is not None

        assert next_move_ledger.discharge_obligation(sid, "ob-1")
        assert next_move_ledger.read_records(sid)[0]["discharged_at"] is not None
        # Discharging again is a no-op (no open record left to match).
        assert not next_move_ledger.discharge_obligation(sid, "ob-1")

    def test_mark_fired_latches_once(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        sid = "ledger-sid-2"
        next_move_ledger.open_obligation(sid, "ob-2", "seam-y", "do the other thing")
        assert next_move_ledger.mark_fired(sid, "ob-2")
        # A record already fired is a no-op on the second call.
        assert not next_move_ledger.mark_fired(sid, "ob-2")

    def test_find_undischarged_unfired_returns_first_match(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        sid = "ledger-sid-3"
        assert next_move_ledger.find_undischarged_unfired(sid) is None
        next_move_ledger.open_obligation(sid, "ob-3", "seam-z", "do a third thing")
        found = next_move_ledger.find_undischarged_unfired(sid)
        assert found is not None
        assert found["obligation_id"] == "ob-3"

    def test_block_obligation_stamps_and_is_cleared_by_progress(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        sid = "ledger-sid-4"
        next_move_ledger.open_obligation(sid, "ob-4", "seam-w", "wait on peer")
        assert next_move_ledger.block_obligation(sid, "ob-4", "peer-session-id", "peer-name")
        record = next_move_ledger.read_records(sid)[0]
        assert record["blocked_on_session_id"] == "peer-session-id"

        next_move_ledger.progress_obligation(sid, "ob-4")
        record = next_move_ledger.read_records(sid)[0]
        assert record["blocked_at"] is None
        assert record["blocked_on_session_id"] is None

    def test_ledger_path_none_outside_a_repo(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert next_move_ledger.ledger_path("some-session") is None

    def test_parse_intake_tolerates_trailing_partial_line(self):
        session_id = "sidZ"
        row = {
            "schema": 1,
            "session_id": session_id,
            "op": "open",
            "obligation_id": "ob-x",
            "seam": "s",
            "next_action": "a",
        }
        text = json.dumps(row) + "\n" + '{"incomple'
        rows, rejected = next_move_ledger.parse_intake(text, session_id)
        assert rows == [row]
        assert rejected == []

    def test_parse_intake_rejects_malformed_mid_file_line(self):
        session_id = "sidY"
        row = {
            "schema": 1,
            "session_id": session_id,
            "op": "open",
            "obligation_id": "ob-x",
            "seam": "s",
            "next_action": "a",
        }
        text = "not json\n" + json.dumps(row) + "\n"
        rows, rejected = next_move_ledger.parse_intake(text, session_id)
        assert rows == [row]
        assert rejected == ["not json"]

    def test_drain_intake_folds_open_row_into_ledger(self, tmp_path, monkeypatch):
        _init_git_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        sid = "ledger-sid-5"
        intake = next_move_ledger.intake_path(sid)
        os.makedirs(os.path.dirname(intake), exist_ok=True)
        row = {
            "schema": 1,
            "session_id": sid,
            "op": "open",
            "obligation_id": "ob-5",
            "seam": "s",
            "next_action": "a",
        }
        with open(intake, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

        report = next_move_ledger.drain_intake(sid)
        assert report["folded"] == 1
        assert not os.path.isfile(intake)
        assert next_move_ledger.find_undischarged_unfired(sid)["obligation_id"] == "ob-5"
