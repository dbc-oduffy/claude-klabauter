"""
Tests for coordinator_core.ops.coordinator_complete_entry.

Independently re-derives parity against the bash oracle's own documented
contract (arg validation table, exit-code table, frontmatter field order,
idempotency guard, degrade-on-absence for the rollup-sentence PATH-first
test shim) rather than merely re-asserting the module's own transcription.

Port of: coordinator-complete-entry.sh (example-doctrine-repo a1a568d2, 2026-07-22)
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from coordinator_core.ops import coordinator_complete_entry as m
from coordinator_core.testing.doe_root import resolve_doe_root


def _git(args, cwd):
    result = subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    return repo


def _run(argv, cwd=None):
    buf_out, buf_err = io.StringIO(), io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        if cwd is not None:
            import os

            old = os.getcwd()
            os.chdir(cwd)
            try:
                rc = m.main(argv)
            finally:
                os.chdir(old)
        else:
            rc = m.main(argv)
    return rc, buf_out.getvalue(), buf_err.getvalue()


# ---------------------------------------------------------------------------
# Arg validation
# ---------------------------------------------------------------------------


class TestArgValidation:
    def test_missing_sid(self):
        rc, _, err = _run(["--disposition", "single-session"])
        assert rc == 1
        assert "--sid is required" in err

    def test_missing_disposition(self):
        rc, _, err = _run(["--sid", "abc123"])
        assert rc == 1
        assert "--disposition is required" in err

    def test_invalid_disposition(self):
        rc, _, err = _run(["--sid", "abc123", "--disposition", "bogus"])
        assert rc == 1
        assert "must be one of" in err and "bogus" in err

    def test_chain_terminal_requires_consumed_handoff(self):
        rc, _, err = _run(["--sid", "abc123", "--disposition", "chain-terminal"])
        assert rc == 1
        assert "--consumed-handoff is required" in err

    def test_memo_predecessor_accepted_at_parse_time_without_consumed_handoff(self):
        # Unlike predecessor-consumed/chain-terminal, memo-predecessor never
        # requires --consumed-handoff — the memo leg carries no consumed
        # handoff path (docs/plans/2026-08-05-memo-predecessor-representable-
        # outcome.md § Fix-locus discrimination).
        parsed, early_exit = m._parse_args(
            ["--sid", "abc123", "--disposition", "memo-predecessor"]
        )
        assert early_exit is None
        assert parsed["disposition"] == "memo-predecessor"
        assert parsed["consumed_handoff"] == ""

    def test_invalid_nature(self):
        rc, _, err = _run(
            ["--sid", "abc123", "--disposition", "single-session", "--nature", "bogus"]
        )
        assert rc == 1
        assert "not a valid completion nature" in err

    def test_valid_natures_accepted_at_parse_time(self):
        # Parse-level acceptance only (full run needs a git repo).
        for nature in ("roadmap", "bugfix", "tech-debt", "infra"):
            parsed, early_exit = m._parse_args(
                ["--sid", "abc123", "--disposition", "single-session", "--nature", nature]
            )
            assert early_exit is None
            assert parsed["nature_val"] == nature

    def test_governing_plan_slug_path_traversal_rejected(self):
        rc, _, err = _run(
            [
                "--sid",
                "abc123",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "../../etc/passwd",
            ]
        )
        assert rc == 1
        assert "invalid characters" in err

    def test_governing_plan_slug_leading_dot_rejected(self):
        rc, _, err = _run(
            [
                "--sid",
                "abc123",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                ".hidden",
            ]
        )
        assert rc == 1

    def test_governing_plan_slug_date_prefixed_accepted(self):
        parsed, early_exit = m._parse_args(
            [
                "--sid",
                "abc123",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "2026-07-01_164217_wsc-step-2.6-chain",
            ]
        )
        assert early_exit is None
        assert parsed["governing_plan_slug"] == "2026-07-01_164217_wsc-step-2.6-chain"

    def test_unknown_option(self):
        rc, _, err = _run(["--bogus-flag"])
        assert rc == 1
        assert "unknown option" in err

    def test_help(self):
        rc, out, _ = _run(["--help"])
        assert rc == 0
        assert "Usage: coordinator-complete-entry" in out

    def test_not_in_git_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "git_root", lambda: None)
        rc, _, err = _run(["--sid", "abc123", "--disposition", "single-session"])
        assert rc == 2
        assert "not inside a git repository" in err


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


class TestWritePath:
    def test_single_session_basic_write(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        # Single-session LoE is now native (no doe_bin dependency) — force its
        # sole degrade path (session id unresolvable) so this fixture still
        # exercises the fully-null LoE block.
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        rc, out, err = _run(["--sid", "session-abcdef", "--disposition", "single-session"], cwd=repo)
        assert rc == 0
        entry_path = out.strip()
        assert entry_path.endswith("-adhoc-abcdef.md")
        text = Path(entry_path).read_text(encoding="utf-8")
        assert 'title: "PLACEHOLDER' in text
        assert "nature: null" in text
        assert "nature_inferred: true" in text
        assert "commits: []" in text
        assert "status: pending-release" in text
        assert "chain_terminal: false" in text
        assert 'authored_by: "session-abcdef"' in text
        # Null LoE block (both siblings unresolvable in this fixture).
        assert "agent_dispatches: null" in text
        assert "opus_dispatches: null" in text
        assert "em_tokens: null" in text
        assert "tshirt: null" in text
        assert "<!-- PROSE:" in text
        assert "<!-- NATURE-INFER:" in text
        assert "Residue: nature" in err

    def test_nature_provided_no_infer_residue(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)

        rc, out, err = _run(
            ["--sid", "session-abcdef", "--disposition", "single-session", "--nature", "bugfix"],
            cwd=repo,
        )
        assert rc == 0
        text = Path(out.strip()).read_text(encoding="utf-8")
        assert "nature: bugfix" in text
        assert "nature_inferred: false" in text
        assert "<!-- NATURE-INFER:" not in text
        assert "Residue: prose" in err
        assert "Residue: nature" not in err

    def test_chain_terminal_write_sets_chain_terminal_true(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        handoff = repo / "handoff.md"
        handoff.write_text("---\n---\n", encoding="utf-8")

        rc, out, err = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "chain-terminal",
                "--consumed-handoff",
                str(handoff),
            ],
            cwd=repo,
        )
        assert rc == 0
        text = Path(out.strip()).read_text(encoding="utf-8")
        assert "chain_terminal: true" in text

    def test_governing_plan_slug_drives_filename_and_chain_field(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)

        rc, out, _ = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "2026-07-06-my-plan",
            ],
            cwd=repo,
        )
        assert rc == 0
        entry_path = out.strip()
        assert "2026-07-06-my-plan-abcdef.md" in entry_path
        text = Path(entry_path).read_text(encoding="utf-8")
        assert 'chain: "2026-07-06-my-plan"' in text

    def test_sid_shorter_than_six_chars_uses_whole_sid(self, tmp_path, monkeypatch):
        # Oracle: SID6="${SID: -6}" — bash returns the whole string when shorter
        # than 6 chars; Python's SID[-6:] matches this exactly.
        repo = _make_repo(tmp_path)

        rc, out, _ = _run(["--sid", "abc", "--disposition", "single-session"], cwd=repo)
        assert rc == 0
        assert out.strip().endswith("-adhoc-abc.md")


# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------


class TestIdempotencyGuard:
    def test_existing_chain_entry_stands_down(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)

        completed = repo / "archive" / "completed" / "2026-07"
        completed.mkdir(parents=True)
        existing = completed / "2026-07-01-my-plan-abc123.md"
        existing.write_text('---\nchain: "2026-07-06-my-plan"\n---\n', encoding="utf-8")

        rc, out, err = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "2026-07-06-my-plan",
            ],
            cwd=repo,
        )
        assert rc == 0
        assert out.strip() == str(existing)
        assert "stand-down" in err
        # No new file should have been written.
        assert len(list(completed.glob("*.md"))) == 1

    def test_stand_down_refuses_a_live_foreign_holders_entry(self, tmp_path, monkeypatch):
        """Regression for the session-shape-misdetection hazard: a misdetected
        session must not stand down onto (and later have
        d-reconcile-completion-commits write into) a LIVE PEER session's
        completion entry, keyed on `authored_by`, not `chain_slug`."""
        repo = _make_repo(tmp_path)

        completed = repo / "archive" / "completed" / "2026-07"
        completed.mkdir(parents=True)
        existing = completed / "2026-07-01-my-plan-peer12.md"
        existing.write_text(
            '---\nchain: "2026-07-06-my-plan"\nauthored_by: "peer-session-peer12"\n---\n',
            encoding="utf-8",
        )

        import coordinator_core.session.liveness as _liveness_mod

        monkeypatch.setattr(_liveness_mod, "session_live", lambda sid, cwd=None: sid == "peer-session-peer12")

        rc, out, err = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "2026-07-06-my-plan",
            ],
            cwd=repo,
        )
        assert rc == 1
        assert str(existing) not in out
        assert "foreign" in err.lower() or "LIVE" in err
        # No new file should have been written, and the foreign entry is untouched.
        assert len(list(completed.glob("*.md"))) == 1
        assert 'authored_by: "peer-session-peer12"' in existing.read_text(encoding="utf-8")

    def test_stand_down_proceeds_when_foreign_holder_is_dead(self, tmp_path, monkeypatch):
        """Terminal-safe: a foreign `authored_by` whose session is NOT live
        must still stand down normally (ambiguity/absence-of-evidence
        proceeds, per the guard's own docstring)."""
        repo = _make_repo(tmp_path)

        completed = repo / "archive" / "completed" / "2026-07"
        completed.mkdir(parents=True)
        existing = completed / "2026-07-01-my-plan-dead12.md"
        existing.write_text(
            '---\nchain: "2026-07-06-my-plan"\nauthored_by: "dead-session-dead12"\n---\n',
            encoding="utf-8",
        )

        import coordinator_core.session.liveness as _liveness_mod

        monkeypatch.setattr(_liveness_mod, "session_live", lambda sid, cwd=None: False)

        rc, out, err = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "2026-07-06-my-plan",
            ],
            cwd=repo,
        )
        assert rc == 0
        assert out.strip() == str(existing)
        assert "stand-down" in err

    def test_legacy_entries_excluded_from_idempotency_scan(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)

        legacy = repo / "archive" / "completed" / "legacy"
        legacy.mkdir(parents=True)
        (legacy / "2026-07.md").write_text('chain: "2026-07-06-my-plan"\n', encoding="utf-8")

        rc, out, _ = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "2026-07-06-my-plan",
            ],
            cwd=repo,
        )
        assert rc == 0
        # A NEW entry was written (legacy/ hit does not stand down the run).
        assert "2026-07-06-my-plan-abcdef.md" in out.strip()


# ---------------------------------------------------------------------------
# Idempotent-preserving rewrite (state/bug-backlog/2026-07-28-workstream-
# complete-apply-re-scaffolds-t-e925d597e0af.yaml) -- an adhoc (no
# --governing-plan-slug) entry reproduces the incident exactly: it has no
# chain slug, so TestIdempotencyGuard's chain-scan stand-down never
# intervenes, and re-running this CLI on the SAME day for the SAME session
# targets the identical filename every time.
# ---------------------------------------------------------------------------


class TestIdempotentPreservingRewrite:
    def _write_first_pass(self, repo, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        rc, out, _ = _run(["--sid", "session-abcdef", "--disposition", "single-session"], cwd=repo)
        assert rc == 0
        return Path(out.strip())

    def test_fully_authored_entry_survives_a_rerun_unchanged(self, tmp_path, monkeypatch):
        # Without the fix, this re-run silently reverts title/nature/prose
        # back to placeholder text -- the exact incident this test pins.
        repo = _make_repo(tmp_path)
        entry_path = self._write_first_pass(repo, monkeypatch)

        authored = (
            "---\n"
            'title: "Ported the widget frobnicator to native Python"\n'
            "created: 2026-07-01\n"
            "nature: bugfix\n"
            "nature_inferred: false\n"
            "commits: []\n"
            "status: pending-release\n"
            "chain_terminal: false\n"
            'authored_by: "session-abcdef"\n'
            "loe:\n"
            "  agent_dispatches: null\n"
            "  opus_dispatches: null\n"
            "  em_tokens: null\n"
            "  tshirt: null\n"
            "---\n"
            "\n"
            "Replaced the shelled-out widget frobnicator with a native port. "
            "No behavior change; verified against the parity suite.\n"
        )
        entry_path.write_text(authored, encoding="utf-8")

        rc, out, err = _run(["--sid", "session-abcdef", "--disposition", "single-session"], cwd=repo)
        assert rc == 0
        assert out.strip() == str(entry_path)
        assert "already fully authored" in err

        text = entry_path.read_text(encoding="utf-8")
        assert 'title: "Ported the widget frobnicator to native Python"' in text
        assert "nature: bugfix" in text
        assert "PLACEHOLDER" not in text
        assert "<!-- PROSE:" not in text
        assert "Replaced the shelled-out widget frobnicator" in text

    def test_partially_authored_entry_preserves_authored_fields_only(self, tmp_path, monkeypatch):
        # Robustness to an EM who authored only SOME of the three fields:
        # title is authored, nature/prose are still placeholder -- the
        # authored title must survive, and nature/prose must still be
        # regenerated (not left as stale garbage) rather than the whole
        # file being either fully clobbered or fully frozen.
        repo = _make_repo(tmp_path)
        entry_path = self._write_first_pass(repo, monkeypatch)

        text = entry_path.read_text(encoding="utf-8")
        assert 'title: "PLACEHOLDER' in text
        partially_authored = text.replace(
            'title: "PLACEHOLDER — replace with past-tense workstream title"',
            'title: "Ported the widget frobnicator to native Python"',
        )
        entry_path.write_text(partially_authored, encoding="utf-8")

        rc, out, err = _run(["--sid", "session-abcdef", "--disposition", "single-session"], cwd=repo)
        assert rc == 0

        rewritten = entry_path.read_text(encoding="utf-8")
        assert 'title: "Ported the widget frobnicator to native Python"' in rewritten
        assert "nature: null" in rewritten
        assert "<!-- PROSE:" in rewritten
        assert "Residue:" in err
        assert "nature" in err
        assert "prose" in err
        # A partially-authored entry is NOT the fully-authored stand-down
        # case -- this pass genuinely rewrote the file (residue reported).
        assert "already fully authored" not in err


# ---------------------------------------------------------------------------
# Legacy-monolith migrate delegation
# ---------------------------------------------------------------------------


class TestLegacyMigrateDelegation:
    def test_migrate_invoked_when_monolith_present(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        completed = repo / "archive" / "completed"
        completed.mkdir(parents=True)
        from datetime import date

        yyyymm = date.today().strftime("%Y-%m")
        (completed / f"{yyyymm}.md").write_text("# monolith\n", encoding="utf-8")

        calls = []
        monkeypatch.setattr(m._migrate_mod, "main", lambda argv: calls.append(argv) or 0)

        rc, _, _ = _run(["--sid", "session-abcdef", "--disposition", "single-session"], cwd=repo)
        assert rc == 0
        assert calls == [["--root", str(repo)]]

    def test_migrate_skipped_via_override_env(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        completed = repo / "archive" / "completed"
        completed.mkdir(parents=True)
        from datetime import date

        yyyymm = date.today().strftime("%Y-%m")
        (completed / f"{yyyymm}.md").write_text("# monolith\n", encoding="utf-8")
        monkeypatch.setenv("COORDINATOR_OVERRIDE_LEGACY_MONOLITH", "1")

        calls = []
        monkeypatch.setattr(m._migrate_mod, "main", lambda argv: calls.append(argv) or 0)

        rc, _, _ = _run(["--sid", "session-abcdef", "--disposition", "single-session"], cwd=repo)
        assert rc == 0
        assert calls == []


# ---------------------------------------------------------------------------
# Chain-terminal LoE — in-process call to the already-ported aggregator
# ---------------------------------------------------------------------------


class TestChainTerminalLoe:
    def test_loe_block_filled_from_aggregator_stdout(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        handoff = repo / "handoff.md"
        handoff.write_text("---\n---\n", encoding="utf-8")

        def _fake_agg_main(argv):
            # The fixed prefix is exact; any `--closing-*` tail is the
            # chain-terminal session naming itself so the aggregate can
            # attribute the row it has not appended yet (`_closing_session_argv`).
            assert argv[:4] == ["--terminal-handoff", str(handoff), "--format", "yaml-frontmatter"]
            assert all(a.startswith("--closing-") or not a.startswith("--") for a in argv[4:])
            print("loe:\n  agent_dispatches: 26\n  opus_dispatches: 4\n  em_tokens: null\n  tshirt: \"L\"")
            return 0

        monkeypatch.setattr(m._agg_loe_mod, "main", _fake_agg_main)

        rc, out, _ = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "chain-terminal",
                "--consumed-handoff",
                str(handoff),
            ],
            cwd=repo,
        )
        assert rc == 0
        text = Path(out.strip()).read_text(encoding="utf-8")
        assert "agent_dispatches: 26" in text
        assert "opus_dispatches: 4" in text
        assert 'tshirt: "L"' in text

    def test_loe_degrades_to_null_on_aggregator_nonzero(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        handoff = repo / "handoff.md"
        handoff.write_text("---\n---\n", encoding="utf-8")

        monkeypatch.setattr(m._agg_loe_mod, "main", lambda argv: 1)

        rc, out, _ = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "chain-terminal",
                "--consumed-handoff",
                str(handoff),
            ],
            cwd=repo,
        )
        assert rc == 0
        text = Path(out.strip()).read_text(encoding="utf-8")
        assert "agent_dispatches: null" in text


# ---------------------------------------------------------------------------
# memo-predecessor LoE routing (C4, docs/plans/2026-08-05-memo-predecessor-
# representable-outcome.md § AC8) — the memo leg carries no consumed
# handoff to aggregate a chain LoE from, so it must route to the SAME
# native single-session tally as `single-session`, never to
# `_chain_terminal_loe` and never to the bare `_NULL_LOE_BLOCK` fallback.
# ---------------------------------------------------------------------------


class TestMemoPredecessorLoe:
    def test_resolve_loe_block_routes_memo_predecessor_to_native_single_session(self, monkeypatch):
        calls = []
        monkeypatch.setattr(m, "_native_single_session_loe", lambda: calls.append("single") or "loe:\n  tshirt: \"S\"")
        monkeypatch.setattr(m, "_chain_terminal_loe", lambda consumed_handoff: calls.append("chain") or "")

        result = m._resolve_loe_block("memo-predecessor", "")

        assert calls == ["single"]
        assert result == 'loe:\n  tshirt: "S"'

    def test_resolve_loe_block_memo_predecessor_never_calls_chain_terminal_loe_even_with_a_path(self, monkeypatch):
        # consumed_handoff is contractually "" on the memo leg, but this
        # guards the routing decision itself (keyed on disposition, not on
        # consumed_handoff truthiness) against a future caller that passes
        # a non-empty value by mistake.
        calls = []
        monkeypatch.setattr(m, "_native_single_session_loe", lambda: calls.append("single") or "loe:\n  tshirt: \"M\"")
        monkeypatch.setattr(m, "_chain_terminal_loe", lambda consumed_handoff: calls.append("chain") or "loe:\n  tshirt: \"L\"")

        result = m._resolve_loe_block("memo-predecessor", "some/handoff.md")

        assert calls == ["single"]
        assert result == 'loe:\n  tshirt: "M"'

    def test_resolve_loe_block_memo_predecessor_degrades_to_null_when_native_empty(self, monkeypatch):
        monkeypatch.setattr(m, "_native_single_session_loe", lambda: "")

        result = m._resolve_loe_block("memo-predecessor", "")

        assert result == m._NULL_LOE_BLOCK


# ---------------------------------------------------------------------------
# Rollup-sentence resolution (AC4)
# ---------------------------------------------------------------------------


class TestRollupSentence:
    def test_no_governing_plan_slug_no_sentence(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        rc, out, _ = _run(["--sid", "session-abcdef", "--disposition", "single-session"], cwd=repo)
        assert rc == 0
        text = Path(out.strip()).read_text(encoding="utf-8")
        assert "advances initiative" not in text

    def test_sentence_included_when_plan_has_deliverable_id(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        plans_dir = repo / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "2026-07-06-my-plan.md").write_text(
            '---\ndeliverable_id: "d-001"\n---\n# Plan\n', encoding="utf-8"
        )

        def _fake_render_main(argv):
            assert argv[0] == "d-001"
            print("advances initiative Widget Rollout (d-001)")
            return 0

        # Isolate the in-process fallback branch — force the PATH-first shim
        # rung to report absent, so this test exercises ONLY the in-process
        # call (the PATH-first branch itself is covered separately below by
        # test_path_first_shim_preferred_over_in_process).
        monkeypatch.setattr(m.shutil, "which", lambda name: None)
        monkeypatch.setattr(m._render_rollup_mod, "main", _fake_render_main)

        rc, out, _ = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "2026-07-06-my-plan",
            ],
            cwd=repo,
        )
        assert rc == 0
        text = Path(out.strip()).read_text(encoding="utf-8")
        assert "advances initiative Widget Rollout (d-001)" in text

    def test_render_failure_never_aborts_write(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(m.shutil, "which", lambda name: None)
        plans_dir = repo / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "2026-07-06-my-plan.md").write_text(
            '---\ndeliverable_id: "d-001"\n---\n# Plan\n', encoding="utf-8"
        )

        def _raise(argv):
            raise RuntimeError("boom")

        monkeypatch.setattr(m._render_rollup_mod, "main", _raise)

        rc, out, _ = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "2026-07-06-my-plan",
            ],
            cwd=repo,
        )
        assert rc == 0
        assert Path(out.strip()).is_file()

    def test_path_first_shim_preferred_over_in_process(self, tmp_path, monkeypatch):
        """Oracle parity: PATH-first `command -v coordinator-render-rollup.sh`
        wins over the in-process call — preserves example-doctrine-repo's own test-shim
        mechanism (Test A relies on exactly this: a stub executable
        PATH-prepended ahead of the real sibling).

        Port of: test-complete-entry-rollup.sh (example-doctrine-repo 432e3285, 2026-07-22)
        """
        repo = _make_repo(tmp_path)
        plans_dir = repo / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "2026-07-06-my-plan.md").write_text(
            '---\ndeliverable_id: "d-001"\n---\n# Plan\n', encoding="utf-8"
        )

        shim = tmp_path / "coordinator-render-rollup.sh"
        shim.write_text(
            "#!/usr/bin/env bash\necho 'advances initiative Shimmed Initiative (d-001)'\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)

        # Review: code-reviewer (Finding 1 consolidation) — `_which_render_rollup_shim`
        # delegates to `coordinator_core.launchable.which_path_ordered`, which walks
        # `os.environ["PATH"]` directly rather than calling `shutil.which` (that's the
        # whole point of the fix: `shutil.which` never finds a `.sh`-suffixed name on
        # Windows). Monkeypatching `shutil.which` no longer intercepts the lookup;
        # prepend the shim's directory onto the real `PATH` instead, matching how a
        # test actually stages this shim in production.
        monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

        def _should_not_be_called(argv):
            raise AssertionError("in-process render module must not be called when a PATH shim exists")

        monkeypatch.setattr(m._render_rollup_mod, "main", _should_not_be_called)

        rc, out, _ = _run(
            [
                "--sid",
                "session-abcdef",
                "--disposition",
                "single-session",
                "--governing-plan-slug",
                "2026-07-06-my-plan",
            ],
            cwd=repo,
        )
        assert rc == 0
        text = Path(out.strip()).read_text(encoding="utf-8")
        assert "advances initiative Shimmed Initiative (d-001)" in text


# ---------------------------------------------------------------------------
# Idempotency-guard primary signal — native records-query (was: node
# query-records.js subprocess; repointed 2026-07-22 onto
# coordinator_core.ops.ceremony.records_query.query_records — see this
# module's own docstring negative-spec for the parity notes).
# ---------------------------------------------------------------------------


class TestQueryRecordsExistingPath:
    def test_finds_matching_completion_record(self, tmp_path):
        repo = _make_repo(tmp_path)
        completed = repo / "archive" / "completed" / "2026-07"
        completed.mkdir(parents=True)
        (completed / "2026-07-01-my-plan-abc123.md").write_text(
            '---\ntitle: "My Plan shipped"\nnature: roadmap\n'
            'chain: "2026-07-06-my-plan"\ncommits:\n  - "abc123"\n---\n',
            encoding="utf-8",
        )

        result = m._query_records_existing_path(str(repo), "2026-07-06-my-plan")
        assert result is not None
        assert "My Plan shipped" in result
        assert "roadmap" in result
        assert "2026-07-06-my-plan" in result
        assert "abc123" in result

    def test_no_matching_chain_degrades_to_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        completed = repo / "archive" / "completed" / "2026-07"
        completed.mkdir(parents=True)
        (completed / "2026-07-01-other-plan-abc123.md").write_text(
            '---\ntitle: "Unrelated"\nnature: roadmap\nchain: "some-other-chain"\n---\n',
            encoding="utf-8",
        )

        assert m._query_records_existing_path(str(repo), "2026-07-06-my-plan") is None

    def test_no_completed_dir_degrades_to_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert m._query_records_existing_path(str(repo), "2026-07-06-my-plan") is None

    def test_query_records_exception_degrades_to_none(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)

        def _raise(record_type, worktree_root, **kwargs):
            raise SystemExit(1)

        monkeypatch.setattr(m, "query_records", _raise)
        assert m._query_records_existing_path(str(repo), "2026-07-06-my-plan") is None


class TestNativeSingleSessionLoe:
    """2026-07-21: coordinator-session-loe.sh retired — single-session LoE is
    now a fully native, in-process reimplementation. No subprocess, no bash;
    these tests exercise the port directly against real
    dispatched-agents.txt fixtures and env-var tokens instead of mocking a
    subprocess call."""

    def test_no_session_id_degrades_to_empty(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        import os

        old = os.getcwd()
        os.chdir(repo)
        try:
            assert m._native_single_session_loe() == ""
        finally:
            os.chdir(old)

    def test_no_dispatched_agents_file_renders_null_block(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-abc123")
        import os

        old = os.getcwd()
        os.chdir(repo)
        try:
            result = m._native_single_session_loe()
        finally:
            os.chdir(old)
        # All four fields emit the sentinel BARE. A quoted `tshirt: "null"`
        # is YAML string "null", which the completion-entry schema rejects
        # (XS|S|M|L|XL or null) — it shipped a schema-violating scaffold into
        # every no-dispatch session's entry until 2026-07-28. The sibling
        # scaffold path (`ops/ceremony/completion_entry.py`) always emitted it
        # bare; this pins the two paths to agree.
        assert result == (
            "loe:\n"
            "  agent_dispatches: null\n"
            "  opus_dispatches: null\n"
            "  em_tokens: null\n"
            "  tshirt: null"
        )

    def test_counts_dispatches_and_computes_tshirt(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-abc123")
        sdir = repo / ".git" / "coordinator-sessions" / "sess-abc123"
        sdir.mkdir(parents=True)
        # 6 lines total, 1 with an opus model field -> agent_dispatches=6,
        # opus_dispatches=1. Neither XL/L/M's higher ad/od thresholds are met
        # (ad<15, od<2), but S's ad>=5 threshold IS met by ad=6 -> tier S.
        (sdir / "dispatched-agents.txt").write_text(
            "agent1\topus\t111\n"
            "agent2\tsonnet\t111\n"
            "agent3\tsonnet\t111\n"
            "agent4\tsonnet\t111\n"
            "agent5\tsonnet\t111\n"
            "agent6\tsonnet\t111\n",
            encoding="utf-8",
        )
        import os

        old = os.getcwd()
        os.chdir(repo)
        try:
            result = m._native_single_session_loe()
        finally:
            os.chdir(old)
        assert "  agent_dispatches: 6\n" in result
        assert "  opus_dispatches: 1\n" in result
        assert 'tshirt: "S"' in result

    def test_empty_dispatched_agents_file_renders_zero_not_null(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-abc123")
        sdir = repo / ".git" / "coordinator-sessions" / "sess-abc123"
        sdir.mkdir(parents=True)
        (sdir / "dispatched-agents.txt").write_text("", encoding="utf-8")
        import os

        old = os.getcwd()
        os.chdir(repo)
        try:
            result = m._native_single_session_loe()
        finally:
            os.chdir(old)
        # present-but-empty file -> 0, not null; XS tier (ad>=0) always fires.
        assert "  agent_dispatches: 0\n" in result
        assert "  opus_dispatches: 0\n" in result
        assert 'tshirt: "XS"' in result

    def test_em_tokens_from_env_vars(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-abc123")
        monkeypatch.setenv("CLAUDE_SESSION_INPUT_TOKENS", "700000")
        monkeypatch.setenv("CLAUDE_SESSION_OUTPUT_TOKENS", "50000")
        import os

        old = os.getcwd()
        os.chdir(repo)
        try:
            result = m._native_single_session_loe()
        finally:
            os.chdir(old)
        assert "  em_tokens: 750000\n" in result
        assert 'tshirt: "L"' in result  # 750000 >= L's 600000 threshold


# ---------------------------------------------------------------------------
# Byte-parity against the example-doctrine-repo oracle trampoline (best-effort — skipped when
# the oracle script isn't reachable from this checkout, e.g. a claude-klabauter-only CI
# runner with no example-doctrine-repo sibling clone).
#
# 2026-07-22: repointed from coordinator-complete-entry.sh (retired by example-doctrine-repo's
# de-bash wave, commit 6fb5fb37) to coordinator-complete-entry.py — the .sh
# file no longer exists on disk; only .py and .cmd siblings remain. The .py
# oracle is invoked via `[sys.executable, oracle_path, ...]`, never a bash/sh
# spawn — it is a python3 script, not a shell script, and a shell spawn would
# feed it as source and fail immediately.
# ---------------------------------------------------------------------------


def _find_oracle() -> Path | None:
    doe_root = resolve_doe_root()
    if not doe_root:
        return None
    candidate = Path(doe_root) / "coordinator" / "bin" / "coordinator-complete-entry.py"
    return candidate if candidate.is_file() else None


@pytest.mark.skipif(_find_oracle() is None, reason="example-doctrine-repo oracle script not found as a sibling checkout")
@pytest.mark.real_home
class TestOracleParity:
    def test_basic_frontmatter_shape_matches_oracle(self, tmp_path, monkeypatch):
        oracle = _find_oracle()
        repo = _make_repo(tmp_path)

        oracle_result = subprocess.run(
            [sys.executable, str(oracle), "--sid", "session-abcdef", "--disposition", "single-session"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert oracle_result.returncode == 0
        oracle_path = oracle_result.stdout.strip()
        oracle_text = Path(oracle_path).read_text(encoding="utf-8")
        # Reset the repo so the port writes into a clean, distinct filename slot
        # is unnecessary (different sid) — use a different sid to avoid clobber.
        rc, out, _ = _run(["--sid", "session-ghijkl", "--disposition", "single-session"], cwd=repo)
        assert rc == 0
        port_text = Path(out.strip()).read_text(encoding="utf-8")

        # Field-order and field-presence parity (values differ: sid, filename).
        oracle_fields = [ln.split(":", 1)[0] for ln in oracle_text.splitlines() if ":" in ln and not ln.startswith("#")]
        port_fields = [ln.split(":", 1)[0] for ln in port_text.splitlines() if ":" in ln and not ln.startswith("#")]
        assert oracle_fields == port_fields
