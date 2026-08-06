"""Tests for coordinator_core.merge_assemble + coordinator_core.merge_assemble.
apply — the merge-ceremony computed skill (B4 chunk C6).

Scope: `brief()`'s compute-only outputs (directive shape/order, judgment
residue, branch-state trichotomy, version-bump proposal) and `apply()`'s
composition of `apply_base` (closed dispatch table resolution, the
`--force` node-ceremony bypass expressed via `already_satisfied`). Does NOT
invoke a real `node`/git subprocess for a directive handler — those are
monkeypatched; `coordinator_core.contract.test_apply_base` already covers
the generic directive-execution engine this module composes.

Spec backlink: docs/plans/2026-07-24-computed-skills-b4-baton-branch-lifecycle.md, chunk C6
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import merge_assemble
from coordinator_core.merge_assemble import apply as merge_apply


# ---------------------------------------------------------------------------
# compute_branch_state
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# compute_version_bump_proposal
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# brief()
# ---------------------------------------------------------------------------

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
        # Canonical 8-key envelope (Review: code-reviewer — Finding 1):
        # branch_state/release_tag_cut/version_bump live in `artifact`;
        # gate_verdicts lives in `gates` — a relocation, not a redesign.
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
        # A repo that carries no plugin-ecosystem suite has nothing for this
        # gate to run; dispatching anyway is a MODULE_NOT_FOUND abort on the
        # ceremony's FIRST hard gate, which wedges the whole run.
        self._stub_git(monkeypatch)
        assert not merge_assemble.node_ceremony_gate_entrypoint(tmp_path).exists()
        gate = merge_assemble.brief(repo_root=tmp_path).decision_object["directives"][0]
        assert gate["id"] == "d0"
        assert gate["already_satisfied"] is True
        assert "coordinator/tests/plugin-ecosystem/run.js" in gate["skipped_reason"]

    def test_node_ceremony_gate_is_not_satisfied_by_a_directory_at_the_entrypoint(
        self, tmp_path, monkeypatch
    ):
        # is_file(), not exists() — a directory named run.js is not a runnable
        # suite, and treating it as present would restore the abort.
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
        # order_by_depends_on requires every depends_on directive-id (not a
        # judgment-point id) to itself be present in the same directives
        # list — d7's depends_on=["d2"] must resolve.
        self._stub_git(monkeypatch)
        do = merge_assemble.brief(repo_root=tmp_path).decision_object
        directive_ids = {d["id"] for d in do["directives"]}
        for d in do["directives"]:
            for dep in d.get("depends_on") or []:
                if dep.startswith("d") and dep[1:].isdigit():
                    assert dep in directive_ids


# ---------------------------------------------------------------------------
# version_bump_final override — the real defect this chunk fixes: the
# judgment point offers an override the pre-fix data model had no channel
# for, so resolving it the only way it permitted always froze the PATCH
# proposal into d2's cut-tag args regardless of PM intent.
# ---------------------------------------------------------------------------

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
            "v١.٢.٣",  # non-ASCII (Arabic-Indic) digits — str.isdigit() is
            # True for these too, so a bare isdigit() check alone would
            # silently accept them (Review: code-reviewer — Finding: non-
            # ASCII-digit override).
            "v1.2. 3",  # embedded whitespace
            " v1.2.3",  # leading whitespace
            "v1.2.3 ",  # trailing whitespace
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
        # "v01.02.03" passes the digit check, but the RAW string must never
        # reach `git tag` — the reconstructed-from-parsed-ints tag is what
        # `cut_tag_input`/d2's `cut-tag` arg carries. (Review: code-reviewer
        # — Finding: leading-zeros-passed-verbatim.)
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
            {"disposition": "override"},  # `value` key absent entirely
            {"disposition": "override", "value": 123},  # non-string value
            {"disposition": "override", "value": ["v1.0.0"]},  # non-string value
        ],
    )
    def test_malformed_override_shape_fails_loud(self, tmp_path, monkeypatch, bad_entry):
        self._stub_git(monkeypatch)
        decisions = {"version_bump_final": bad_entry}
        result = merge_assemble.brief(repo_root=tmp_path, decisions=decisions)
        assert result.exit_code != merge_assemble.EXIT_OK
        assert "error" in result.decision_object

    def test_confirmed_disposition_with_stray_value_key_is_inert(self, tmp_path, monkeypatch):
        # `confirmed` short-circuits on `disposition != "override"` before
        # ever reading `value` — a stray `value` key must not change the
        # outcome (Review: code-reviewer — probed-and-confirmed-OK item,
        # now covered by an explicit test).
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

        # Wrong prefix for the override value is rejected even though it
        # would parse fine under the default "v" prefix.
        bad_decisions = {
            "version_bump_final": {"disposition": "override", "value": "v2.5.0"}
        }
        bad_result = merge_assemble.brief(
            repo_root=tmp_path, decisions=bad_decisions, tag_prefix="rel-"
        )
        assert bad_result.exit_code != merge_assemble.EXIT_OK


# ---------------------------------------------------------------------------
# apply() — force bypass + dispatch-table composition (no real subprocess)
# ---------------------------------------------------------------------------

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
        # d2/d4/d5 depend on judgment points with no `decisions` supplied,
        # so the run still reports HALTED_AT_JUDGMENT overall — but every
        # OTHER directive (including the forced d0) still lands.
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
