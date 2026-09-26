from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import merge_assemble
from coordinator_core.merge_assemble import apply as merge_apply


class TestComputeBranchState:
    def test_clean_when_zero_ahead_and_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            merge_assemble,
            "_run_git",
            lambda args, cwd: SimpleNamespace(returncode=0, stdout="0\t0\n", stderr=""),
        )
        assert merge_assemble.compute_branch_state(tmp_path) == merge_assemble.BRANCH_STATE_CLEAN

    def test_needs_recovery_when_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            merge_assemble,
            "_run_git",
            lambda args, cwd: SimpleNamespace(returncode=0, stdout="3\t1\n", stderr=""),
        )
        assert (
            merge_assemble.compute_branch_state(tmp_path)
            == merge_assemble.BRANCH_STATE_NEEDS_RECOVERY
        )

    def test_diverged_when_ahead_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            merge_assemble,
            "_run_git",
            lambda args, cwd: SimpleNamespace(returncode=0, stdout="0\t2\n", stderr=""),
        )
        assert (
            merge_assemble.compute_branch_state(tmp_path) == merge_assemble.BRANCH_STATE_DIVERGED
        )

    def test_git_failure_falls_back_to_diverged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            merge_assemble,
            "_run_git",
            lambda args, cwd: SimpleNamespace(returncode=1, stdout="", stderr="no origin/main"),
        )
        assert (
            merge_assemble.compute_branch_state(tmp_path) == merge_assemble.BRANCH_STATE_DIVERGED
        )


class TestComputeVersionBumpProposal:
    def test_proposes_patch_bump_from_latest_tag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            merge_assemble,
            "_run_git",
            lambda args, cwd: SimpleNamespace(returncode=0, stdout="v1.2.3\nv1.2.2\n", stderr=""),
        )
        result = merge_assemble.compute_version_bump_proposal(tmp_path)
        assert result == {"current": "v1.2.3", "proposed": "v1.2.4", "bump": "patch"}

    def test_no_existing_tag_returns_none_proposal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            merge_assemble,
            "_run_git",
            lambda args, cwd: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        result = merge_assemble.compute_version_bump_proposal(tmp_path)
        assert result == {"current": None, "proposed": None, "bump": "patch"}


class TestBrief:
    def _stub_git(self, monkeypatch, *, rev_list_out="0\t1\n", tag_out="v1.0.0\n"):
        def _fake_run_git(args, cwd):
            if args[0] == "rev-list":
                return SimpleNamespace(returncode=0, stdout=rev_list_out, stderr="")
            if args[0] == "tag":
                return SimpleNamespace(returncode=0, stdout=tag_out, stderr="")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(merge_assemble, "_run_git", _fake_run_git)

    def test_transport_fail_when_no_repo_root_resolvable(self, monkeypatch):
        monkeypatch.setattr(merge_assemble, "resolve_repo_root", lambda: None)
        result = merge_assemble.brief()
        assert result.exit_code == merge_assemble.EXIT_TRANSPORT_FAIL
        assert "error" in result.decision_object

    def test_decision_object_shape(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        result = merge_assemble.brief(repo_root=tmp_path)
        assert result.exit_code == merge_assemble.EXIT_OK
        do = result.decision_object
        assert set(do.keys()) == {
            "artifact",
            "preflight",
            "gates",
            "directives",
            "judgment_points",
            "decisions",
            "narration",
            "next_move",
        }
        assert do["artifact"]["branch_state"] == merge_assemble.BRANCH_STATE_DIVERGED
        assert do["artifact"]["release_tag_cut"] == "v1.0.1"
        assert do["gates"] == merge_assemble.build_gate_verdicts_scaffold()

    def _write_node_gate_entrypoint(self, repo_root):
        entrypoint = merge_assemble.node_ceremony_gate_entrypoint(repo_root)
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("// stub run.js\n", encoding="utf-8")
        return entrypoint

    def test_node_ceremony_gate_is_first_directive(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        self._write_node_gate_entrypoint(tmp_path)
        do = merge_assemble.brief(repo_root=tmp_path).decision_object
        assert do["directives"][0]["id"] == "d0"
        assert do["directives"][0]["cli"] == "node-ceremony-gate"
        assert do["directives"][0]["depends_on"] is None
        assert do["directives"][0]["already_satisfied"] is False
        assert "skipped_reason" not in do["directives"][0]

    def test_node_ceremony_gate_self_satisfies_when_the_suite_is_absent(
        self, tmp_path, monkeypatch
    ):
        # gate to run; dispatching anyway is a MODULE_NOT_FOUND abort on the
        self._stub_git(monkeypatch)
        assert not merge_assemble.node_ceremony_gate_entrypoint(tmp_path).exists()
        gate = merge_assemble.brief(repo_root=tmp_path).decision_object["directives"][0]
        assert gate["id"] == "d0"
        assert gate["already_satisfied"] is True
        assert "coordinator/tests/plugin-ecosystem/run.js" in gate["skipped_reason"]

    def test_node_ceremony_gate_is_not_satisfied_by_a_directory_at_the_entrypoint(
        self, tmp_path, monkeypatch
    ):
        self._stub_git(monkeypatch)
        merge_assemble.node_ceremony_gate_entrypoint(tmp_path).mkdir(parents=True)
        gate = merge_assemble.brief(repo_root=tmp_path).decision_object["directives"][0]
        assert gate["already_satisfied"] is True

    def test_every_directive_cli_is_in_the_expected_closed_set(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        do = merge_assemble.brief(repo_root=tmp_path).decision_object
        expected_clis = {
            "node-ceremony-gate",
            "merge-recovery-and-tag-cut",
            "merge-gate-and-pr",
            "portability-sweep",
            "check-no-illegal-paths",
            "merge-release-notes-derive",
            "orphan-branch-sweep",
            "tier-u-grant",
        }
        assert {d["cli"] for d in do["directives"]} == expected_clis

    def test_judgment_points_all_carry_recommendation_none(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        do = merge_assemble.brief(repo_root=tmp_path).decision_object
        jp_ids = {jp["id"] for jp in do["judgment_points"]}
        assert jp_ids == {
            "ship_verdict",
            "version_bump_final",
            "portability_disposition",
            "ci_failure_interpretation",
            "merge_conflict_resolution",
        }
        assert all(jp["recommendation"] is None for jp in do["judgment_points"])

    def test_directives_are_well_formed_for_apply_base_ordering(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        do = merge_assemble.brief(repo_root=tmp_path).decision_object
        directive_ids = {d["id"] for d in do["directives"]}
        for d in do["directives"]:
            for dep in d.get("depends_on") or []:
                if dep.startswith("d") and dep[1:].isdigit():
                    assert dep in directive_ids


class TestVersionBumpOverride:
    def _stub_git(self, monkeypatch, *, rev_list_out="0\t1\n", tag_out="v1.0.0\n"):
        def _fake_run_git(args, cwd):
            if args[0] == "rev-list":
                return SimpleNamespace(returncode=0, stdout=rev_list_out, stderr="")
            if args[0] == "tag":
                return SimpleNamespace(returncode=0, stdout=tag_out, stderr="")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(merge_assemble, "_run_git", _fake_run_git)

    def test_default_no_decisions_still_cuts_the_proposed_patch_tag(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        do = merge_assemble.brief(repo_root=tmp_path).decision_object
        assert do["artifact"]["release_tag_cut"] == "v1.0.1"
        d2 = next(d for d in do["directives"] if d["id"] == "d2")
        assert d2["args"] == ["cut-tag", "v1.0.1"]

    def test_confirmed_disposition_still_cuts_the_proposed_patch_tag(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        decisions = {"version_bump_final": {"disposition": "confirmed"}}
        do = merge_assemble.brief(repo_root=tmp_path, decisions=decisions).decision_object
        assert do["artifact"]["release_tag_cut"] == "v1.0.1"
        d2 = next(d for d in do["directives"] if d["id"] == "d2")
        assert d2["args"] == ["cut-tag", "v1.0.1"]

    def test_override_with_explicit_version_wins_over_proposal(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        decisions = {
            "version_bump_final": {"disposition": "override", "value": "v0.16.0"}
        }
        result = merge_assemble.brief(repo_root=tmp_path, decisions=decisions)
        assert result.exit_code == merge_assemble.EXIT_OK
        do = result.decision_object
        assert do["artifact"]["release_tag_cut"] == "v0.16.0"
        assert do["artifact"]["version_bump"]["override"] == "v0.16.0"
        assert do["artifact"]["version_bump"]["proposed"] == "v1.0.1"
        d2 = next(d for d in do["directives"] if d["id"] == "d2")
        assert d2["args"] == ["cut-tag", "v0.16.0"]

    def test_bare_string_decisions_entry_normalizes_to_override(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        decisions = {"version_bump_final": "v0.16.0"}
        do = merge_assemble.brief(repo_root=tmp_path, decisions=decisions).decision_object
        assert do["artifact"]["release_tag_cut"] == "v0.16.0"
        assert do["decisions"]["version_bump_final"] == {
            "disposition": "override",
            "value": "v0.16.0",
        }
        d2 = next(d for d in do["directives"] if d["id"] == "d2")
        assert d2["args"] == ["cut-tag", "v0.16.0"]

    @pytest.mark.parametrize(
        "bad_value",
        [
            "0.16",
            "vX.Y.Z",
            "",
            "v1.2.3.4",
            "v١.٢.٣",
            "v1.2. 3",
            " v1.2.3",
            "v1.2.3 ",
        ],
    )
    def test_malformed_override_fails_loud(self, tmp_path, monkeypatch, bad_value):
        self._stub_git(monkeypatch)
        decisions = {
            "version_bump_final": {"disposition": "override", "value": bad_value}
        }
        result = merge_assemble.brief(repo_root=tmp_path, decisions=decisions)
        assert result.exit_code != merge_assemble.EXIT_OK
        assert "error" in result.decision_object

    def test_leading_zeros_in_override_are_normalized_not_passed_verbatim(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        decisions = {
            "version_bump_final": {"disposition": "override", "value": "v01.02.03"}
        }
        do = merge_assemble.brief(repo_root=tmp_path, decisions=decisions).decision_object
        assert do["artifact"]["release_tag_cut"] == "v1.2.3"
        d2 = next(d for d in do["directives"] if d["id"] == "d2")
        assert d2["args"] == ["cut-tag", "v1.2.3"]

    @pytest.mark.parametrize(
        "bad_entry",
        [
            {"disposition": "override"},
            {"disposition": "override", "value": 123},
            {"disposition": "override", "value": ["v1.0.0"]},
        ],
    )
    def test_malformed_override_shape_fails_loud(self, tmp_path, monkeypatch, bad_entry):
        self._stub_git(monkeypatch)
        decisions = {"version_bump_final": bad_entry}
        result = merge_assemble.brief(repo_root=tmp_path, decisions=decisions)
        assert result.exit_code != merge_assemble.EXIT_OK
        assert "error" in result.decision_object

    def test_confirmed_disposition_with_stray_value_key_is_inert(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        decisions = {
            "version_bump_final": {"disposition": "confirmed", "value": "v9.9.9"}
        }
        do = merge_assemble.brief(repo_root=tmp_path, decisions=decisions).decision_object
        assert do["artifact"]["release_tag_cut"] == "v1.0.1"
        d2 = next(d for d in do["directives"] if d["id"] == "d2")
        assert d2["args"] == ["cut-tag", "v1.0.1"]

    def test_malformed_override_via_apply_does_not_fire_d2(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "test-session")
        self._stub_git(monkeypatch)
        decisions = {"version_bump_final": "not-a-version"}
        exit_code, report = merge_apply.apply(
            repo_root=tmp_path, decisions=decisions, force=True
        )
        assert exit_code == merge_apply.APPLY_EXIT_TRANSPORT_FAIL
        assert "d2" not in report.get("landed", [])

    def test_override_respects_a_non_default_tag_prefix(self, tmp_path, monkeypatch):
        def _fake_run_git(args, cwd):
            if args[0] == "rev-list":
                return SimpleNamespace(returncode=0, stdout="0\t1\n", stderr="")
            if args[0] == "tag":
                return SimpleNamespace(returncode=0, stdout="rel-1.0.0\n", stderr="")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(merge_assemble, "_run_git", _fake_run_git)
        decisions = {
            "version_bump_final": {"disposition": "override", "value": "rel-2.5.0"}
        }
        do = merge_assemble.brief(
            repo_root=tmp_path, decisions=decisions, tag_prefix="rel-"
        ).decision_object
        assert do["artifact"]["release_tag_cut"] == "rel-2.5.0"
        d2 = next(d for d in do["directives"] if d["id"] == "d2")
        assert d2["args"] == ["cut-tag", "rel-2.5.0"]

        bad_decisions = {
            "version_bump_final": {"disposition": "override", "value": "v2.5.0"}
        }
        bad_result = merge_assemble.brief(
            repo_root=tmp_path, decisions=bad_decisions, tag_prefix="rel-"
        )
        assert bad_result.exit_code != merge_assemble.EXIT_OK


class TestVersionBumpDecline:
    def _stub_git(self, monkeypatch, *, rev_list_out="0\t1\n", tag_out="v1.0.0\n"):
        def _fake_run_git(args, cwd):
            if args[0] == "rev-list":
                return SimpleNamespace(returncode=0, stdout=rev_list_out, stderr="")
            if args[0] == "tag":
                return SimpleNamespace(returncode=0, stdout=tag_out, stderr="")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(merge_assemble, "_run_git", _fake_run_git)

    def test_decline_disposition_leaves_release_tag_cut_null(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        decisions = {"version_bump_final": {"disposition": "decline"}}
        do = merge_assemble.brief(repo_root=tmp_path, decisions=decisions).decision_object
        assert do["artifact"]["release_tag_cut"] is None

    def _stub_all_handlers(self, monkeypatch):
        for name in merge_apply._CLI_DISPATCH:
            monkeypatch.setitem(
                merge_apply._CLI_DISPATCH, name, lambda args, repo_root, _n=name: {"cli": _n}
            )

    def test_declined_run_halts_without_d2_and_reports_declined(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "test-session")
        self._stub_git(monkeypatch)
        self._stub_all_handlers(monkeypatch)
        decisions = {"version_bump_final": {"disposition": "decline"}}
        exit_code, report = merge_apply.apply(repo_root=tmp_path, decisions=decisions, force=True)
        assert exit_code == merge_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert "d2" not in report.get("landed", [])
        assert report.get("release_tag_cut") is None
        assert "version_bump_final" in report.get("declined_judgment_points", [])
        assert "version_bump_final" not in report.get("unresolved_judgment_points", [])
        # ship_verdict was never answered at all — it stays UNANSWERED, not
        assert "ship_verdict" in report.get("unresolved_judgment_points", [])

    def test_bare_string_decline_normalizes_to_disposition_not_override(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch)
        decisions = {"version_bump_final": "decline"}
        result = merge_assemble.brief(repo_root=tmp_path, decisions=decisions)
        do = result.decision_object
        assert do["decisions"]["version_bump_final"] == {"disposition": "decline"}
        assert do["artifact"]["release_tag_cut"] is None
        assert result.exit_code == merge_assemble.EXIT_OK
        assert "error" not in do

    def test_bare_decline_tuple_matches_declared_non_version_dispositions(self):
        # `_VERSION_BUMP_FINAL_BARE_
        judgment_points = merge_assemble.build_judgment_points()
        point = next(jp for jp in judgment_points if jp["id"] == "version_bump_final")
        non_version_values = {
            d["value"] for d in point["dispositions"] if "d2" not in (d.get("resolves") or [])
        }
        assert non_version_values == set(merge_assemble._VERSION_BUMP_FINAL_BARE_DECLINE)


class TestApplyForceBypass:
    def test_force_marks_node_gate_already_satisfied(self):
        directives = [
            {"id": "d0", "cli": "node-ceremony-gate", "args": [], "depends_on": None, "already_satisfied": False},
            {"id": "d1", "cli": "merge-recovery-and-tag-cut", "args": [], "depends_on": None, "already_satisfied": False},
        ]
        out = merge_apply._apply_force_bypass(directives, force=True)
        assert out[0]["already_satisfied"] is True
        assert out[1]["already_satisfied"] is False

    def test_no_force_leaves_directives_unchanged(self):
        directives = [
            {"id": "d0", "cli": "node-ceremony-gate", "args": [], "depends_on": None, "already_satisfied": False},
        ]
        out = merge_apply._apply_force_bypass(directives, force=False)
        assert out == directives


class TestApplyDispatchTable:
    def test_every_directive_cli_resolves_in_the_closed_table(self, tmp_path, monkeypatch):
        def _fake_run_git(args, cwd):
            if args[0] == "rev-list":
                return SimpleNamespace(returncode=0, stdout="0\t1\n", stderr="")
            if args[0] == "tag":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(merge_assemble, "_run_git", _fake_run_git)
        do = merge_assemble.brief(repo_root=tmp_path).decision_object
        for d in do["directives"]:
            assert d["cli"] in merge_apply._CLI_DISPATCH

    def test_apply_runs_end_to_end_with_stubbed_handlers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "test-session")

        def _fake_run_git(args, cwd):
            if args[0] == "rev-list":
                return SimpleNamespace(returncode=0, stdout="0\t0\n", stderr="")
            if args[0] == "tag":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(merge_assemble, "_run_git", _fake_run_git)

        for name in merge_apply._CLI_DISPATCH:
            monkeypatch.setitem(
                merge_apply._CLI_DISPATCH, name, lambda args, repo_root, _n=name: {"cli": _n}
            )

        exit_code, report = merge_apply.apply(repo_root=tmp_path, force=True)
        # so the run still reports HALTED_AT_JUDGMENT overall — but every
        assert exit_code == merge_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert "d0" in report["landed"]
        assert "d1" in report["landed"]
        assert "d2" not in report["landed"]

    def test_no_session_id_is_transport_fail(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        def _fake_run_git(args, cwd):
            return SimpleNamespace(returncode=0, stdout="0\t0\n", stderr="")

        monkeypatch.setattr(merge_assemble, "_run_git", _fake_run_git)
        exit_code, report = merge_apply.apply(repo_root=tmp_path)
        assert exit_code == merge_apply.APPLY_EXIT_TRANSPORT_FAIL
        assert "error" in report


class TestDispatchTierUGrant:
    def test_denied_check_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.session.grant_directive.run_grant_directive",
            lambda args, repo_root=None: (1, "check: gate denied -- no live grant"),
        )
        with pytest.raises(RuntimeError, match="gate denied"):
            merge_apply._dispatch_tier_u_grant(["check"], tmp_path)

    def test_granted_check_returns_ok_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.session.grant_directive.run_grant_directive",
            lambda args, repo_root=None: (0, ""),
        )
        result = merge_apply._dispatch_tier_u_grant(["check"], tmp_path)
        assert result["returncode"] == 0
        assert "degraded_reason" not in result

    def test_failed_grant_still_degrades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.session.grant_directive.run_grant_directive",
            lambda args, repo_root=None: (1, "grant: session id unresolvable"),
        )
        result = merge_apply._dispatch_tier_u_grant(["grant", "pm", "note"], tmp_path)
        assert result["returncode"] == 1
        assert "degraded_reason" in result

    def test_usage_error_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.session.grant_directive.run_grant_directive",
            lambda args, repo_root=None: (2, "grant directive: no verb"),
        )
        with pytest.raises(RuntimeError):
            merge_apply._dispatch_tier_u_grant([], tmp_path)

    def test_repo_root_threads_as_cwd(self, tmp_path, monkeypatch):
        seen = {}

        def _fake_run_grant_directive(args, repo_root=None):
            seen["repo_root"] = repo_root
            return (0, "")

        monkeypatch.setattr(
            "coordinator_core.session.grant_directive.run_grant_directive",
            _fake_run_grant_directive,
        )
        merge_apply._dispatch_tier_u_grant(["check"], tmp_path)
        assert seen["repo_root"] == str(tmp_path)
