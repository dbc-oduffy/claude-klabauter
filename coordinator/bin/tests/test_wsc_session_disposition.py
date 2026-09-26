from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

import pytest

from coordinator_core.claim_state import ClaimState

# spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is explicitly not
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "wsc_session_disposition", str(_BIN_DIR / "wsc-session-disposition.py")
    )
    spec = importlib.util.spec_from_loader("wsc_session_disposition", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


wsc = _load_cli_module()
wsc._bootstrap_engine_imports()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _init_repo_with_history(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-q")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("hello\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(work, "update-ref", "refs/remotes/origin/main", "HEAD")
    return work


def _commit_with_session_trailer(repo: Path, sid: str, message: str) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"{message}\n\nSession-Id: {sid}")


class TestSessionIdResolution(unittest.TestCase):
    def setUp(self):
        self._env_backup = {}
        for var in ("em_sid", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            self._env_backup[var] = None

    def tearDown(self):
        import os

        for var in ("em_sid", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(var, None)

    def test_em_sid_wins_over_everything(self, tmp_path_factory=None):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            os.environ["em_sid"] = "em-priority"
            os.environ["CLAUDE_SESSION_ID"] = "claude-session"
            os.environ["CLAUDE_CODE_SESSION_ID"] = "claude-code-session"
            self.assertEqual(wsc.resolve_session_id(repo), "em-priority")

    def test_claude_session_id_wins_over_claude_code_session_id(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            os.environ.pop("em_sid", None)
            os.environ["CLAUDE_SESSION_ID"] = "claude-session"
            os.environ["CLAUDE_CODE_SESSION_ID"] = "claude-code-session"
            self.assertEqual(wsc.resolve_session_id(repo), "claude-session")

    def test_claude_code_session_id_used_when_others_absent(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            os.environ.pop("em_sid", None)
            os.environ.pop("CLAUDE_SESSION_ID", None)
            os.environ["CLAUDE_CODE_SESSION_ID"] = "claude-code-session"
            self.assertEqual(wsc.resolve_session_id(repo), "claude-code-session")

    def test_sentinel_file_is_ignored_KS3(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            os.environ.pop("em_sid", None)
            os.environ.pop("CLAUDE_SESSION_ID", None)
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            sentinel_dir = repo / ".git" / "coordinator-sessions"
            sentinel_dir.mkdir(parents=True)
            (sentinel_dir / ".current-session-id").write_text("sentinel-sid\n")
            sid = wsc.resolve_session_id(repo)
            self.assertNotEqual(sid, "sentinel-sid")
            self.assertEqual(sid, "")

    def test_unresolved_reports_empty_not_fabricated_KS5(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            os.environ.pop("em_sid", None)
            os.environ.pop("CLAUDE_SESSION_ID", None)
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            sid = wsc.resolve_session_id(repo)
            self.assertEqual(sid, "")

    def test_cmd_resolve_refuses_unresolved_sid_KS5(self):
        import argparse
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            os.environ.pop("em_sid", None)
            os.environ.pop("CLAUDE_SESSION_ID", None)
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            args = argparse.Namespace(repo_root=str(repo), format="eval")
            rc = wsc._cmd_resolve(args)
            self.assertEqual(rc, wsc._SESSION_ID_UNRESOLVED)


class TestPrimaryScan(unittest.TestCase):
    def test_matches_claimed_by(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoffs = repo / "state" / "handoffs"
            handoffs.mkdir(parents=True)
            (handoffs / "2026-07-01_foo.md").write_text(
                "---\nclaimed_by: sid-123\npredecessor: none\n---\nbody\n"
            )
            result = wsc.primary_consumed_handoff(repo, "sid-123")
            self.assertEqual(result, "state/handoffs/2026-07-01_foo.md")

    def test_matches_legacy_consumed_by(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoffs = repo / "state" / "handoffs"
            handoffs.mkdir(parents=True)
            (handoffs / "2026-07-01_foo.md").write_text(
                "---\nconsumed_by: sid-legacy\npredecessor: none\n---\nbody\n"
            )
            result = wsc.primary_consumed_handoff(repo, "sid-legacy")
            self.assertEqual(result, "state/handoffs/2026-07-01_foo.md")

    def test_no_match_returns_none(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "state" / "handoffs").mkdir(parents=True)
            self.assertIsNone(wsc.primary_consumed_handoff(repo, "no-such-sid"))

    def test_missing_dir_returns_none(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertIsNone(wsc.primary_consumed_handoff(repo, "sid-123"))

    def test_paths_plural_matches_scalar_on_single_match(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoffs = repo / "state" / "handoffs"
            handoffs.mkdir(parents=True)
            (handoffs / "2026-07-01_foo.md").write_text(
                "---\nclaimed_by: sid-123\npredecessor: none\n---\nbody\n"
            )
            paths = wsc.primary_consumed_handoff_paths(repo, "sid-123")
            self.assertEqual(paths, ["state/handoffs/2026-07-01_foo.md"])
            self.assertEqual(wsc.primary_consumed_handoff(repo, "sid-123"), paths[0])

    def test_paths_plural_returns_all_sorted_matches(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoffs = repo / "state" / "handoffs"
            handoffs.mkdir(parents=True)
            (handoffs / "2026-07-02_second.md").write_text(
                "---\nclaimed_by: sid-multi\npredecessor: none\n---\nbody\n"
            )
            (handoffs / "2026-07-01_first.md").write_text(
                "---\nconsumed_by: sid-multi\npredecessor: none\n---\nbody\n"
            )
            paths = wsc.primary_consumed_handoff_paths(repo, "sid-multi")
            self.assertEqual(
                paths,
                [
                    "state/handoffs/2026-07-01_first.md",
                    "state/handoffs/2026-07-02_second.md",
                ],
            )
            self.assertEqual(wsc.primary_consumed_handoff(repo, "sid-multi"), paths[-1])

    def test_paths_plural_empty_when_missing_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertEqual(wsc.primary_consumed_handoff_paths(repo, "sid-123"), [])

    # --- HOLDER-MISMATCH PARTITION (2026-08-26) -------------------------

    def _scan_with_states(self, states):
        import tempfile
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoffs = repo / "state" / "handoffs"
            handoffs.mkdir(parents=True)
            for name in states:
                (handoffs / name).write_text("---\nkind: session-handoff\n---\nbody\n")

            def _fake(path, **_kwargs):
                return states[Path(path).name]

            with mock.patch.object(wsc, "resolve_claim_state", _fake):
                return wsc.primary_consumed_handoff_scan(repo, "sid-me")

    @staticmethod
    def _state(holder, mirror_holder):
        return ClaimState(
            holder=holder,
            claimed_at=None,
            source="ledger",
            disagreement=holder is not None and mirror_holder is None,
            ledger_holder=holder,
            mirror_holder=mirror_holder,
        )

    def test_ledger_claim_whose_mirror_names_a_peer_is_not_adopted(self):
        result = self._scan_with_states(
            {"peer-baton.md": self._state("sid-me", "sid-peer")}
        )
        self.assertEqual(result.adopted, [])
        self.assertEqual(result.mirror_conflicts, ["state/handoffs/peer-baton.md"])

    def test_ledger_claim_with_empty_mirror_is_still_adopted(self):
        result = self._scan_with_states(
            {"reverted.md": self._state("sid-me", None)}
        )
        self.assertEqual(result.adopted, ["state/handoffs/reverted.md"])
        self.assertEqual(result.mirror_conflicts, [])

    def test_agreeing_mirror_is_adopted(self):
        result = self._scan_with_states(
            {"mine.md": self._state("sid-me", "sid-me")}
        )
        self.assertEqual(result.adopted, ["state/handoffs/mine.md"])
        self.assertEqual(result.mirror_conflicts, [])

    def test_clean_candidate_survives_alongside_a_conflicted_one(self):
        result = self._scan_with_states(
            {
                "a-peer-baton.md": self._state("sid-me", "sid-peer"),
                "b-mine.md": self._state("sid-me", "sid-me"),
            }
        )
        self.assertEqual(result.adopted, ["state/handoffs/b-mine.md"])
        self.assertEqual(result.mirror_conflicts, ["state/handoffs/a-peer-baton.md"])

    def test_conflicted_candidate_is_absent_from_the_legacy_scalar_readers(self):
        import tempfile
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "state" / "handoffs").mkdir(parents=True)
            (repo / "state" / "handoffs" / "peer-baton.md").write_text("---\n---\n")
            with mock.patch.object(
                wsc, "resolve_claim_state", lambda p, **k: self._state("sid-me", "sid-peer")
            ):
                self.assertEqual(wsc.primary_consumed_handoff_paths(repo, "sid-me"), [])
                self.assertIsNone(wsc.primary_consumed_handoff(repo, "sid-me"))


class TestDetectorA(unittest.TestCase):
    def test_requires_both_consumer_and_predecessor(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            archive = repo / "archive" / "handoffs"
            archive.mkdir(parents=True)
            (archive / "no-predecessor.md").write_text("claimed_by: sid-a\n")
            (archive / "valid.md").write_text("claimed_by: sid-a\npredecessor: some-sha\n")
            result = wsc.detector_a(repo, "sid-a")
            self.assertEqual(result, "archive/handoffs/valid.md")

    def test_no_candidates_returns_none(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "archive" / "handoffs").mkdir(parents=True)
            self.assertIsNone(wsc.detector_a(repo, "sid-a"))


class TestParseScopePaths(unittest.TestCase):
    def test_extracts_quoted_and_bare_entries(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "handoff.md"
            f.write_text(
                "predecessor: none\n"
                "scope:\n"
                "  - 'docs/plans/foo.md'\n"
                '  - "coordinator/bin/tests"\n'
                "  - state/handoffs\n"
                "status: open\n"
            )
            self.assertEqual(
                wsc.parse_scope_paths(str(f)),
                ["docs/plans/foo.md", "coordinator/bin/tests", "state/handoffs"],
            )

    def test_no_scope_block_returns_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "handoff.md"
            f.write_text("predecessor: none\nstatus: open\n")
            self.assertEqual(wsc.parse_scope_paths(str(f)), [])


class TestResolveCrashRecovery(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_handoff(self, name: str, scope_lines: list[str]) -> str:
        f = self.repo_root / name
        body = "predecessor: none\nscope:\n" + "".join(f"  - {s}\n" for s in scope_lines)
        f.write_text(body)
        return str(f)

    def test_exact_file_match_resolves_single_hit(self):
        handoff = self._write_handoff("h1.md", ["coordinator/bin/foo.py"])
        diagnostics: list[str] = []
        result, status = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")], ["coordinator/bin/foo.py"], self.repo_root, diagnostics
        )
        # every downstream consumer expects WSC_CONSUMED_HANDOFF repo-relative.
        self.assertEqual(result, "h1.md")
        self.assertEqual(status, "crash-recovery")
        self.assertTrue(any("chain-terminal resolved by Detector C" in d for d in diagnostics))

    def test_directory_prefix_match_resolves_single_hit(self):
        handoff = self._write_handoff("h1.md", ["coordinator/bin/tests"])
        diagnostics: list[str] = []
        result, status = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")],
            ["coordinator/bin/tests/test_foo.py"],
            self.repo_root,
            diagnostics,
        )
        self.assertEqual(result, "h1.md")
        self.assertEqual(status, "crash-recovery")

    def test_no_intersection_is_true_negative(self):
        handoff = self._write_handoff("h1.md", ["some/unrelated/path.py"])
        diagnostics: list[str] = []
        result, status = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")], ["coordinator/bin/foo.py"], self.repo_root, diagnostics
        )
        self.assertIsNone(result)
        self.assertIsNone(status)
        self.assertTrue(any("did NOT intersect" in d for d in diagnostics))

    def test_multiple_matches_is_ambiguous(self):
        h1 = self._write_handoff("h1.md", ["coordinator/bin/foo.py"])
        h2 = self._write_handoff("h2.md", ["coordinator/bin/bar.py"])
        diagnostics: list[str] = []
        result, status = wsc._resolve_crash_recovery(
            [(h1, "dead-1"), (h2, "dead-2")],
            ["coordinator/bin/foo.py", "coordinator/bin/bar.py"],
            self.repo_root,
            diagnostics,
        )
        self.assertIsNone(result)
        self.assertEqual(status, "ambiguous")
        self.assertTrue(any("cannot disambiguate" in d for d in diagnostics))

    def test_empty_scope_never_counts_as_a_hit(self):
        handoff = self._write_handoff("h1.md", [])
        diagnostics: list[str] = []
        result, status = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")], ["coordinator/bin/foo.py"], self.repo_root, diagnostics
        )
        self.assertIsNone(result)
        self.assertIsNone(status)


    def test_directory_prefix_matching_many_committed_paths_counts_as_one_matched_entry(self):
        handoff = self._write_handoff("h1.md", ["coordinator/bin/tests"])
        diagnostics: list[str] = []
        outcome = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")],
            [
                "coordinator/bin/tests/test_foo.py",
                "coordinator/bin/tests/test_bar.py",
                "coordinator/bin/tests/test_baz.py",
            ],
            self.repo_root,
            diagnostics,
        )
        self.assertEqual(outcome[1], "crash-recovery")
        self.assertEqual(
            outcome.match_facts,
            {
                "matched_scope_entry_count": 1,
                "scope_size": 1,
                "single_match_kind": "prefix",
                "exact_match_count": 0,
            },
        )

    def test_match_facts_report_exact_kind_and_sizes_on_a_single_entry_scope(self):
        handoff = self._write_handoff("h1.md", ["coordinator/bin/foo.py"])
        diagnostics: list[str] = []
        outcome = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")], ["coordinator/bin/foo.py"], self.repo_root, diagnostics
        )
        self.assertEqual(
            outcome.match_facts,
            {
                "matched_scope_entry_count": 1,
                "scope_size": 1,
                "single_match_kind": "exact",
                "exact_match_count": 1,
            },
        )

    def test_match_facts_report_all_matched_entries_on_a_multi_entry_scope(self):
        handoff = self._write_handoff("h1.md", ["a.py", "b.py", "c.py"])
        diagnostics: list[str] = []
        outcome = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")], ["a.py", "b.py"], self.repo_root, diagnostics
        )
        self.assertEqual(outcome[1], "crash-recovery")
        self.assertEqual(outcome.match_facts["matched_scope_entry_count"], 2)
        self.assertEqual(outcome.match_facts["scope_size"], 3)
        self.assertIsNone(outcome.match_facts["single_match_kind"])
        self.assertEqual(outcome.match_facts["exact_match_count"], 2)

    def test_match_facts_exact_match_count_derived_from_a_mixed_exact_and_prefix_match(self):
        handoff = self._write_handoff("h1.md", ["tests/", "docs/", "a.py"])
        diagnostics: list[str] = []
        outcome = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")],
            ["tests/test_foo.py", "docs/readme.md", "a.py"],
            self.repo_root,
            diagnostics,
        )
        self.assertEqual(outcome[1], "crash-recovery")
        self.assertEqual(outcome.match_facts["matched_scope_entry_count"], 3)
        self.assertEqual(outcome.match_facts["scope_size"], 3)
        self.assertEqual(outcome.match_facts["exact_match_count"], 1)

    def test_match_facts_exact_match_count_zero_on_the_example_market_data_repo_shape(self):
        handoff = self._write_handoff(
            "h1.md",
            ["tests/", "docs/", "a.py", "b.py", "c.py", "d.py", "e.py"],
        )
        diagnostics: list[str] = []
        outcome = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")],
            ["tests/test_foo.py", "docs/readme.md"],
            self.repo_root,
            diagnostics,
        )
        self.assertEqual(outcome[1], "crash-recovery")
        self.assertEqual(outcome.match_facts["matched_scope_entry_count"], 2)
        self.assertEqual(outcome.match_facts["scope_size"], 7)
        self.assertEqual(outcome.match_facts["exact_match_count"], 0)

    def test_match_facts_one_exact_plus_two_prefix_in_a_7_entry_scope(self):
        handoff = self._write_handoff(
            "h1.md",
            ["tests/", "docs/", "a.py", "b.py", "c.py", "d.py", "e.py"],
        )
        diagnostics: list[str] = []
        outcome = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")],
            ["tests/test_foo.py", "docs/readme.md", "a.py"],
            self.repo_root,
            diagnostics,
        )
        self.assertEqual(outcome[1], "crash-recovery")
        self.assertEqual(outcome.match_facts["matched_scope_entry_count"], 3)
        self.assertEqual(outcome.match_facts["scope_size"], 7)
        self.assertEqual(outcome.match_facts["exact_match_count"], 1)
        self.assertTrue(wsc.is_coincidence_prone_detection(outcome.match_facts))

    def test_match_facts_is_none_on_the_ambiguous_multi_baton_path(self):
        h1 = self._write_handoff("h1.md", ["coordinator/bin/foo.py"])
        h2 = self._write_handoff("h2.md", ["coordinator/bin/bar.py"])
        diagnostics: list[str] = []
        outcome = wsc._resolve_crash_recovery(
            [(h1, "dead-1"), (h2, "dead-2")],
            ["coordinator/bin/foo.py", "coordinator/bin/bar.py"],
            self.repo_root,
            diagnostics,
        )
        self.assertEqual(outcome[1], "ambiguous")
        self.assertIsNone(outcome.match_facts)

    def test_match_facts_is_none_on_the_true_negative_path(self):
        handoff = self._write_handoff("h1.md", ["some/unrelated/path.py"])
        diagnostics: list[str] = []
        outcome = wsc._resolve_crash_recovery(
            [(handoff, "dead-sid")], ["coordinator/bin/foo.py"], self.repo_root, diagnostics
        )
        self.assertIsNone(outcome[1])
        self.assertIsNone(outcome.match_facts)

    def test_multiple_matches_narrowed_by_own_sid_consume_stamp(self):
        wsc._bootstrap_engine_imports()
        h1 = self.repo_root / "h1.md"
        h1.write_text(
            "claimed_by: this-session\npredecessor: none\nscope:\n  - coordinator/bin/foo.py\n"
        )
        h2 = self.repo_root / "h2.md"
        h2.write_text("predecessor: none\nscope:\n  - coordinator/bin/bar.py\n")
        diagnostics: list[str] = []
        result, status = wsc._resolve_crash_recovery(
            [(str(h1), "dead-1"), (str(h2), "dead-2")],
            ["coordinator/bin/foo.py", "coordinator/bin/bar.py"],
            self.repo_root,
            diagnostics,
            sid="this-session",
        )
        self.assertEqual(result, "h1.md")
        self.assertEqual(status, "crash-recovery")
        self.assertTrue(any("consume-stamp" in d for d in diagnostics))

    def test_multiple_matches_without_a_consume_stamp_still_ambiguous(self):
        wsc._bootstrap_engine_imports()
        h1 = self.repo_root / "h1.md"
        h1.write_text("predecessor: none\nscope:\n  - coordinator/bin/foo.py\n")
        h2 = self.repo_root / "h2.md"
        h2.write_text("predecessor: none\nscope:\n  - coordinator/bin/bar.py\n")
        diagnostics: list[str] = []
        result, status = wsc._resolve_crash_recovery(
            [(str(h1), "dead-1"), (str(h2), "dead-2")],
            ["coordinator/bin/foo.py", "coordinator/bin/bar.py"],
            self.repo_root,
            diagnostics,
            sid="this-session",
        )
        self.assertIsNone(result)
        self.assertEqual(status, "ambiguous")

    def test_baton_match_and_scope_entry_match_are_named_not_positional_tuples(self):
        scope_match = wsc.ScopeEntryMatch(scope_entry="a.py", hit_path="a.py", kind="exact")
        self.assertEqual(scope_match.scope_entry, "a.py")
        self.assertEqual(scope_match.kind, "exact")
        baton_match = wsc.BatonMatch(
            handoff_path="h1.md", dead_sid="dead-sid", matched_scope_entries=(scope_match,), scope_size=3
        )
        self.assertEqual(baton_match.matched_scope_entries, (scope_match,))
        self.assertEqual(baton_match.scope_size, 3)


class TestIsCoincidenceProneDetection(unittest.TestCase):

    def test_all_prefix_multi_match_is_coincidence_prone_at_any_count(self):
        self.assertTrue(
            wsc.is_coincidence_prone_detection(
                {"matched_scope_entry_count": 2, "scope_size": 7, "exact_match_count": 0}
            )
        )

    def test_single_exact_match_in_a_larger_scope_is_coincidence_prone(self):
        self.assertTrue(
            wsc.is_coincidence_prone_detection(
                {
                    "matched_scope_entry_count": 1,
                    "scope_size": 7,
                    "single_match_kind": "exact",
                    "exact_match_count": 1,
                }
            )
        )

    def test_single_prefix_match_in_a_1_entry_scope_is_coincidence_prone(self):
        self.assertTrue(
            wsc.is_coincidence_prone_detection(
                {
                    "matched_scope_entry_count": 1,
                    "scope_size": 1,
                    "single_match_kind": "prefix",
                    "exact_match_count": 0,
                }
            )
        )

    def test_scope_size_1_exact_match_is_not_coincidence_prone(self):
        self.assertFalse(
            wsc.is_coincidence_prone_detection(
                {
                    "matched_scope_entry_count": 1,
                    "scope_size": 1,
                    "single_match_kind": "exact",
                    "exact_match_count": 1,
                }
            )
        )

    def test_exact_match_count_absent_degrades_to_the_pre_fix_verdict(self):
        self.assertFalse(
            wsc.is_coincidence_prone_detection(
                {"matched_scope_entry_count": 2, "scope_size": 7}
            )
        )

    def test_empty_match_facts_is_not_coincidence_prone(self):
        self.assertFalse(wsc.is_coincidence_prone_detection({}))

    def test_one_exact_plus_two_prefix_in_a_7_entry_scope_is_coincidence_prone(self):
        self.assertTrue(
            wsc.is_coincidence_prone_detection(
                {
                    "matched_scope_entry_count": 3,
                    "scope_size": 7,
                    "exact_match_count": 1,
                }
            )
        )

    def test_two_exact_matches_in_a_7_entry_scope_is_not_coincidence_prone(self):
        self.assertFalse(
            wsc.is_coincidence_prone_detection(
                {
                    "matched_scope_entry_count": 2,
                    "scope_size": 7,
                    "exact_match_count": 2,
                }
            )
        )
        self.assertFalse(
            wsc.is_coincidence_prone_detection(
                {
                    "matched_scope_entry_count": 4,
                    "scope_size": 7,
                    "exact_match_count": 2,
                }
            )
        )


class TestLedgerFirstClaimStateMigration(unittest.TestCase):

    def _write_ledger_claim(self, common_dir, handoff_name: str, session_id: str, claimed_at: str = "") -> None:
        claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
        claim_dir.mkdir(parents=True, exist_ok=True)
        (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
        if claimed_at:
            (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")

    def test_detector_a_fires_for_a_desynced_baton(self):
        import tempfile

        from coordinator_core import claim_state as claim_state_mod

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q")
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-08-07_desynced.md"
            handoff.write_text("---\nstatus: open\npredecessor: some-sha\n---\nbody\n")

            self._write_ledger_claim(repo / ".git", handoff.name, "sid-ledger-only", "2026-08-07T10:00:00Z")

            with unittest.mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True):
                result = wsc.detector_a(repo, "sid-ledger-only")

            self.assertEqual(result, "archive/handoffs/2026-08-07_desynced.md")

    def test_spoof_guard_still_catches_a_ledger_only_foreign_claim(self):
        """The spoof-guard's first rung (claim-holder read) must still
        reject a handoff whose claim (ledger-only, mirror desynced) names a
        DIFFERENT live session — widening what the guard can see must not
        weaken what it refuses."""
        import tempfile

        from coordinator_core import claim_state as claim_state_mod

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q")
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-08-07_foreign.md"
            handoff.write_text("---\nstatus: open\npredecessor: some-sha\n---\nbody\n")

            self._write_ledger_claim(repo / ".git", handoff.name, "sid-foreign-live", "2026-08-07T10:00:00Z")

            with unittest.mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True):
                rejected, reason = wsc._foreign_consumer_guard(
                    repo, "archive/handoffs/2026-08-07_foreign.md", "sid-restorer"
                )

            self.assertTrue(rejected)
            self.assertIn("sid-foreign-live", reason)
            self.assertIn("restoration-commit spoof guard", reason)

    def test_spoof_guard_permits_own_ledger_only_claim(self):
        import tempfile

        from coordinator_core import claim_state as claim_state_mod

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q")
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-08-07_own.md"
            handoff.write_text("---\nstatus: open\npredecessor: some-sha\n---\nbody\n")

            self._write_ledger_claim(repo / ".git", handoff.name, "sid-owner", "2026-08-07T10:00:00Z")

            with unittest.mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True):
                rejected, reason = wsc._foreign_consumer_guard(
                    repo, "archive/handoffs/2026-08-07_own.md", "sid-owner"
                )

            self.assertFalse(rejected)


class TestResolveDispositionIntegration(unittest.TestCase):

    def test_single_session_when_nothing_matches(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(repo, "sid-nobody")
            self.assertEqual(disposition, "single-session")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])

    def test_plural_return_equals_primary_scan_matches_on_multi_consumed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoffs = repo / "state" / "handoffs"
            handoffs.mkdir(parents=True)
            (handoffs / "2026-07-02_second.md").write_text(
                "---\nclaimed_by: sid-multi\npredecessor: none\n---\nbody\n"
            )
            (handoffs / "2026-07-01_first.md").write_text(
                "---\nconsumed_by: sid-multi\npredecessor: none\n---\nbody\n"
            )
            expected = wsc.primary_consumed_handoff_paths(repo, "sid-multi")
            self.assertEqual(len(expected), 2)

            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(repo, "sid-multi")
            self.assertEqual(disposition, "predecessor-consumed")
            self.assertEqual(consumed_paths, expected)
            self.assertEqual(consumed, consumed_paths[0])

    def test_scalar_equals_plural_first_on_single_consumed_handoff(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoffs = repo / "state" / "handoffs"
            handoffs.mkdir(parents=True)
            (handoffs / "2026-07-01_solo.md").write_text(
                "---\nclaimed_by: sid-solo\npredecessor: none\n---\nbody\n"
            )
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(repo, "sid-solo")
            self.assertEqual(disposition, "predecessor-consumed")
            self.assertEqual(len(consumed_paths), 1)
            self.assertEqual(consumed, consumed_paths[0])

    def test_detector_b_resolves_chain_terminal_on_archived_handoff(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-01_predecessor.md"
            handoff.write_text("claimed_by: sid-shipper\npredecessor: some-sha\n")
            _git(repo, "add", "archive/handoffs/2026-07-01_predecessor.md")
            _commit_with_session_trailer(repo, "sid-shipper", "archive handoff")

            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(repo, "sid-shipper")
            self.assertEqual(disposition, "predecessor-consumed")
            self.assertEqual(consumed, "archive/handoffs/2026-07-01_predecessor.md")
            self.assertEqual(consumed_paths, ["archive/handoffs/2026-07-01_predecessor.md"])

    def test_foreign_consumer_guard_rejects_spoofed_restoration_commit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-01_other.md"
            # claimed_by names a DIFFERENT session than the one committing —
            handoff.write_text("---\nclaimed_by: sid-other-session\npredecessor: some-sha\n---\nbody\n")
            _git(repo, "add", "archive/handoffs/2026-07-01_other.md")
            _commit_with_session_trailer(repo, "sid-restorer", "restore handoff")

            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(repo, "sid-restorer")
            self.assertEqual(disposition, "single-session")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])
            self.assertTrue(any("restoration-commit spoof guard" in d for d in diagnostics))

    def test_sweep_attributed_commit_not_read_as_own_provenance(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-10_roadmap-qsub-03.md"
            handoff.write_text("---\npredecessor: none\n---\nbody\n")
            _git(repo, "add", "archive/handoffs/2026-07-10_roadmap-qsub-03.md")
            _commit_with_session_trailer(
                repo, "sid-sweeper", "fleet: archive 1 completed handoff(s)"
            )

            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-sweeper"
            )
            self.assertEqual(disposition, "single-session")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])
            self.assertTrue(
                any(
                    "automated bulk/housekeeping archival prefix" in d
                    for d in diagnostics
                ),
                diagnostics,
            )

    def test_boot_sweep_attributed_commit_also_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-10_other.md"
            handoff.write_text("---\npredecessor: none\n---\nbody\n")
            _git(repo, "add", "archive/handoffs/2026-07-10_other.md")
            _commit_with_session_trailer(
                repo, "sid-sweeper2", "session.boot_sweep: stamp 1 consumed handoff(s) metadata"
            )

            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-sweeper2"
            )
            self.assertEqual(disposition, "single-session")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])

    def test_origin_session_guard_rejects_unclaimed_foreign_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-10_foreign-origin.md"
            handoff.write_text(
                "---\npredecessor: none\norigin_session: sid-original-author\n---\nbody\n"
            )
            _git(repo, "add", "archive/handoffs/2026-07-10_foreign-origin.md")
            _commit_with_session_trailer(repo, "sid-restorer2", "ship and archive handoff")

            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-restorer2"
            )
            self.assertEqual(disposition, "single-session")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])
            self.assertTrue(
                any("origin_session (sid-original-author)" in d for d in diagnostics),
                diagnostics,
            )

    def test_touching_an_ownerless_looking_record_is_not_consuming_it(self):
        """2026-08-10 archive-leg touch-vs-consume incident (example-retrieval-repo memo
        `2026-08-10-example-retrieval-repo-em-wsc-archive-leg-infers-consumption-from-a-
        touch.md`), reproduced at the exact observed shape: a peer had staged
        the deletion of its own archived baton in the shared index, this
        session's `git commit --amend` swept that deletion in, and this
        session committed a RESTORE to re-track the peer's file.

        The record is a live peer's baton — but it presents as ownerless to
        both negative-evidence reads: its ledger claim is liveness-gated away
        by `resolve_claim_state`, its frontmatter mirror carries `status:
        claimed` with no `claimed_by:`, and it records `authoring_session:`
        (deliberately not consulted — path-shaped in this corpus) rather than
        `origin_session:`. The commit subject is ordinary prose, so the
        sweep-prefix filter never sees it.

        Before the fix both reads came back empty and the guard fell through
        to acceptance, resolving `predecessor-consumed` against a peer's
        baton. Ownership must be POSITIVELY named — absence of evidence is
        not evidence of ownership."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-08-10_144028_peer-baton.md"
            handoff.write_text(
                "---\nstatus: claimed\npredecessor: state/handoffs/2026-08-10-peer.md\n"
                "deployment_state: continued\n"
                "authoring_session: 9c0c419d-def6-4b98-90ba-42d2580e870a\n---\nbody\n"
            )
            _git(repo, "add", "archive/handoffs/2026-08-10_144028_peer-baton.md")
            _commit_with_session_trailer(
                repo,
                "sid-restorer3",
                "restore: re-track a peer's archived handoff my amend swept out",
            )

            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-restorer3"
            )
            self.assertEqual(disposition, "single-session")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])
            self.assertTrue(
                any("is not evidence of consuming it" in d for d in diagnostics),
                diagnostics,
            )

    def test_ownerless_rejection_does_not_subsume_the_sweep_subject_filter(self):
        """Negative-spec guard for the 2026-08-10 fix: the memo proposing it
        also proposed RETIRING the sweep-subject allowlist as redundant. It is
        not. Fixture is the reachable non-redundant case: a record this
        session ORIGINATED but never claimed (so Detector A, which keys on the
        claim stamp, misses it and Detector B runs), swept by a bulk `fleet:
        archive` commit. `origin_session` positively names this session, so
        the tightened ownership guard accepts — only the subject filter
        rejects, and a bulk sweep is still not a consume."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-10_swept-but-mine.md"
            handoff.write_text(
                "---\npredecessor: none\norigin_session: sid-sweeper3\n---\nbody\n"
            )
            _git(repo, "add", "archive/handoffs/2026-07-10_swept-but-mine.md")
            _commit_with_session_trailer(
                repo, "sid-sweeper3", "fleet: archive 1 completed handoff(s)"
            )

            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-sweeper3"
            )
            self.assertEqual(disposition, "single-session")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])
            self.assertTrue(
                any(
                    "automated bulk/housekeeping archival prefix" in d
                    for d in diagnostics
                ),
                diagnostics,
            )

    def test_legitimate_own_claim_and_origin_still_resolves_chain_terminal(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-01_own.md"
            handoff.write_text(
                "---\nclaimed_by: sid-owner\npredecessor: some-sha\n"
                "origin_session: sid-owner\n---\nbody\n"
            )
            _git(repo, "add", "archive/handoffs/2026-07-01_own.md")
            _commit_with_session_trailer(repo, "sid-owner", "ship and archive my own predecessor")

            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-owner"
            )
            self.assertEqual(disposition, "predecessor-consumed")
            self.assertEqual(consumed, "archive/handoffs/2026-07-01_own.md")
            self.assertEqual(consumed_paths, ["archive/handoffs/2026-07-01_own.md"])


class TestResolveDispositionEnvOverride(unittest.TestCase):
    """WSC_DISPOSITION / WSC_CONSUMED_HANDOFF escalate-only override — see
    resolve_disposition's own docstring for the design. Every test isolates
    env via unittest.mock.patch.dict(os.environ, ..., clear=False) with an
    explicit removal of both vars first, so no case leaks into another."""

    def setUp(self):
        import os

        self._backup = {
            k: os.environ.pop(k, None) for k in ("WSC_DISPOSITION", "WSC_CONSUMED_HANDOFF")
        }

    def tearDown(self):
        import os

        for k, v in self._backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_canonical_token_override_wins_over_single_session_detector_result(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            os.environ["WSC_DISPOSITION"] = "predecessor-consumed"
            os.environ["WSC_CONSUMED_HANDOFF"] = "state/handoffs/some.md"
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-nobody"
            )
            self.assertEqual(disposition, "predecessor-consumed")
            self.assertEqual(consumed, "state/handoffs/some.md")
            self.assertEqual(consumed_paths, ["state/handoffs/some.md"])
            self.assertTrue(any("WSC_DISPOSITION" in d and "override" in d for d in diagnostics))

    def test_legacy_chain_terminal_alias_accepted_and_normalised(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            os.environ["WSC_DISPOSITION"] = " Chain-Terminal "
            os.environ["WSC_CONSUMED_HANDOFF"] = "state/handoffs/legacy.md"
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-nobody"
            )
            self.assertEqual(disposition, "predecessor-consumed")
            self.assertEqual(consumed, "state/handoffs/legacy.md")
            self.assertEqual(consumed_paths, ["state/handoffs/legacy.md"])

    def test_single_session_downgrade_refused_when_detector_positively_finds_a_consume(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoffs = repo / "state" / "handoffs"
            handoffs.mkdir(parents=True)
            (handoffs / "2026-07-01_solo.md").write_text(
                "---\nclaimed_by: sid-solo\npredecessor: none\n---\nbody\n"
            )
            os.environ["WSC_DISPOSITION"] = "single-session"
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-solo"
            )
            self.assertEqual(disposition, "predecessor-consumed")
            self.assertEqual(len(consumed_paths), 1)
            self.assertTrue(
                any("cannot downgrade" in d for d in diagnostics),
                diagnostics,
            )

    def test_single_session_downgrade_refused_and_chain_runs_normally_on_true_negative(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            os.environ["WSC_DISPOSITION"] = "single-session"
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-nobody"
            )
            self.assertEqual(disposition, "single-session")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])
            self.assertTrue(any("cannot downgrade" in d for d in diagnostics))

    def test_unrecognised_value_ignored_with_warn(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            os.environ["WSC_DISPOSITION"] = "bogus-value"
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-nobody"
            )
            self.assertEqual(disposition, "single-session")
            self.assertTrue(
                any("unrecognised WSC_DISPOSITION" in d for d in diagnostics), diagnostics
            )

    def test_consumed_handoff_alone_does_not_flip_disposition(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            os.environ["WSC_CONSUMED_HANDOFF"] = "state/handoffs/orphan.md"
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-nobody"
            )
            self.assertEqual(disposition, "single-session")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])
            self.assertTrue(
                any("WSC_CONSUMED_HANDOFF is set without WSC_DISPOSITION" in d for d in diagnostics),
                diagnostics,
            )

    def test_scalar_equals_plural_first_on_override_path(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            os.environ["WSC_DISPOSITION"] = "predecessor-consumed"
            os.environ["WSC_CONSUMED_HANDOFF"] = "state/handoffs/override.md"
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-nobody"
            )
            self.assertEqual(len(consumed_paths), 1)
            self.assertEqual(consumed, consumed_paths[0])

    def test_scalar_and_plural_empty_when_override_positive_but_no_handoff_given(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            os.environ["WSC_DISPOSITION"] = "predecessor-consumed"
            disposition, consumed, diagnostics, consumed_paths = wsc.resolve_disposition(
                repo, "sid-nobody"
            )
            self.assertEqual(disposition, "predecessor-consumed")
            self.assertEqual(consumed, "")
            self.assertEqual(consumed_paths, [])


class TestDispositionResolutionDetectionRecord(unittest.TestCase):
    """`resolve_disposition` carries a STRUCTURED detection record
    (`.detection`) alongside its historical 4-tuple, so no consumer has to
    re-derive "which leg decided, and how sure was it" by substring-matching
    the free-text diagnostics — the coupling that made
    `coordinator_core/workstream_complete`'s `_session_shape_is_uncertain`
    silently stop firing on every Detector C crash-recovery attribution."""

    def setUp(self):
        import os

        self._backup = {
            k: os.environ.pop(k, None) for k in ("WSC_DISPOSITION", "WSC_CONSUMED_HANDOFF")
        }

    def tearDown(self):
        import os

        for k, v in self._backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_return_is_still_unpackable_as_exactly_four_values(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            result = wsc.resolve_disposition(repo, "sid-nobody")
            disposition, consumed, diagnostics, consumed_paths = result
            self.assertEqual(len(result), 4)
            self.assertEqual(tuple(result), (disposition, consumed, diagnostics, consumed_paths))
            self.assertEqual(list(result), [disposition, consumed, diagnostics, consumed_paths])
            self.assertEqual(result.disposition, disposition)

    def test_env_override_leg_is_named_in_the_detection_record(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            os.environ["WSC_DISPOSITION"] = "predecessor-consumed"
            os.environ["WSC_CONSUMED_HANDOFF"] = "state/handoffs/some.md"
            result = wsc.resolve_disposition(repo, "sid-nobody")
            self.assertEqual(result.detection["deciding_leg"], "env-override")
            self.assertIsNone(result.detection["detector_c_status"])

    def test_live_consume_leg_is_named_in_the_detection_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            handoffs = repo / "state" / "handoffs"
            handoffs.mkdir(parents=True)
            (handoffs / "2026-07-01_solo.md").write_text(
                "---\nclaimed_by: sid-solo\npredecessor: none\n---\nbody\n"
            )
            result = wsc.resolve_disposition(repo, "sid-solo")
            self.assertEqual(result.detection["deciding_leg"], "live-consume")

    def test_unresolved_chain_reports_the_none_leg(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            result = wsc.resolve_disposition(repo, "sid-nobody")
            self.assertEqual(result.disposition, "single-session")
            self.assertEqual(result.detection["deciding_leg"], "none")

    def test_every_deciding_leg_emitted_is_a_member_of_the_closed_enum(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            result = wsc.resolve_disposition(repo, "sid-nobody")
            self.assertIn(result.detection["deciding_leg"], wsc.DECIDING_LEGS)

    def test_crash_recovery_status_is_the_detector_c_leg_signal(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            handoff = repo_root / "h1.md"
            handoff.write_text("predecessor: none\nscope:\n  - coordinator_core/\n")
            diagnostics: list[str] = []
            result, status = wsc._resolve_crash_recovery(
                [(str(handoff), "dead-sid")],
                ["coordinator_core/workstream_complete/__init__.py"],
                repo_root,
                diagnostics,
            )
            self.assertEqual(result, "h1.md")
            self.assertEqual(status, "crash-recovery")


class TestResolveDispositionDetectorCLegWiring(unittest.TestCase):

    def test_detector_c_leg_populates_match_facts_on_the_real_detection_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            touched = repo / "coordinator" / "bin" / "widget.py"
            touched.parent.mkdir(parents=True)
            touched.write_text("# widget\n")
            _git(repo, "add", "coordinator/bin/widget.py")
            _commit_with_session_trailer(repo, "sid-crashy", "touch widget")

            stale_handoff = repo / "state" / "handoffs" / "2026-07-01_dead.md"
            stale_handoff.parent.mkdir(parents=True, exist_ok=True)
            stale_handoff.write_text("predecessor: none\nscope:\n  - coordinator/bin\n")

            fake_cli = repo / "fake-session-claim-cli"
            fake_cli.write_text("#!/bin/sh\nexit 0\n")

            with unittest.mock.patch.object(
                wsc, "find_session_claim_cli", return_value=fake_cli
            ), unittest.mock.patch.object(
                wsc,
                "list_stale_claim_handoffs",
                return_value=([(str(stale_handoff), "dead-sid")], 0),
            ):
                result = wsc.resolve_disposition(repo, "sid-crashy")

            self.assertEqual(result.disposition, "single-session")
            self.assertEqual(result.detection["deciding_leg"], "detector-c")
            self.assertEqual(result.detection["detector_c_status"], "crash-recovery")
            self.assertEqual(result.detection["matched_scope_entry_count"], 1)
            self.assertEqual(result.detection["scope_size"], 1)
            self.assertEqual(result.detection["single_match_kind"], "prefix")

    def test_archive_leg_carries_no_detector_c_or_match_facts_on_the_real_detection_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-01_predecessor.md"
            handoff.write_text("claimed_by: sid-shipper\npredecessor: some-sha\n")
            _git(repo, "add", "archive/handoffs/2026-07-01_predecessor.md")
            _commit_with_session_trailer(repo, "sid-shipper", "archive handoff")

            result = wsc.resolve_disposition(repo, "sid-shipper")

            self.assertEqual(result.disposition, "predecessor-consumed")
            self.assertEqual(result.detection["deciding_leg"], "archive")
            self.assertIsNone(result.detection["detector_c_status"])
            self.assertNotIn("matched_scope_entry_count", result.detection)
            self.assertNotIn("scope_size", result.detection)
            self.assertNotIn("single_match_kind", result.detection)


class TestSessionShapeGateRoundTrip(unittest.TestCase):
    """SessionShapeGate (coordinator_core/workstream_complete/__init__.py)
    gains consumed_handoff_paths ALONGSIDE the existing scalar
    consumed_handoff — assert both fields round-trip through the NamedTuple
    without the scalar being dropped.

    This class covers
    the NamedTuple's field shape only (a hand-authored `detection` dict goes
    in, the same dict comes back out). It does NOT cover
    `compute_session_shape_gate`'s wiring (whether a real
    `DispositionResolution.detection` actually reaches this field) — that is
    covered by `TestResolveDispositionDetectorCLegWiring` above plus
    `coordinator_core/workstream_complete/`'s own test suite. Do not read
    this class as end-to-end coverage of the package boundary."""

    def test_scalar_and_plural_fields_both_round_trip(self):
        from coordinator_core.workstream_complete import SessionShapeGate

        gate = SessionShapeGate(
            sid="sid-1",
            disposition="chain-terminal",
            consumed_handoff="state/handoffs/a.md",
            diagnostics=[],
            consumed_handoff_paths=("state/handoffs/a.md", "state/handoffs/b.md"),
            detection={
                "deciding_leg": "detector-c",
                "detector_c_status": "crash-recovery",
                "matched_scope_entry_count": 1,
                "scope_size": 4,
                "single_match_kind": "exact",
            },
        )
        self.assertEqual(gate.detection["deciding_leg"], "detector-c")
        self.assertEqual(gate.detection["matched_scope_entry_count"], 1)
        self.assertEqual(gate.detection["scope_size"], 4)
        self.assertEqual(gate.detection["single_match_kind"], "exact")
        self.assertEqual(gate.consumed_handoff, "state/handoffs/a.md")
        self.assertEqual(
            gate.consumed_handoff_paths,
            ("state/handoffs/a.md", "state/handoffs/b.md"),
        )
        self.assertEqual(gate.consumed_handoff, gate.consumed_handoff_paths[0])


class TestFindSessionClaimCli(unittest.TestCase):
    """home-resolution-lint bare_home_or_chain fix (2026-07-29):
    `find_session_claim_cli`'s settings-home-shim fallback rung must resolve
    via USERPROFILE when CLAUDE_HOME/HOME are both absent -- the
    PowerShell/cmd.exe native-Windows condition."""

    def test_settings_home_fallback_uses_userprofile_when_home_absent(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            userprofile_home = tmp_path / "winhome"
            cli = (
                userprofile_home
                / ".coordinator-claude-settings"
                / "bin"
                / "session-claim-cli"
            )
            cli.parent.mkdir(parents=True)
            cli.write_text("#!/bin/sh\nexit 0\n")
            cli.chmod(0o755)

            env_backup = {
                k: os.environ.get(k)
                for k in ("COORDINATOR_SETTINGS_HOME", "CLAUDE_HOME", "HOME", "USERPROFILE")
            }
            os.environ.pop("COORDINATOR_SETTINGS_HOME", None)
            os.environ.pop("CLAUDE_HOME", None)
            os.environ.pop("HOME", None)
            os.environ["USERPROFILE"] = str(userprofile_home)
            real_access = os.access
            real_is_file = Path.is_file
            sibling_path = _BIN_DIR / "session-claim-cli.py"

            def _fake_access(path, mode):
                if str(path) == str(sibling_path):
                    return False
                return real_access(path, mode)

            def _fake_is_file(self):
                if str(self) == str(sibling_path):
                    return False
                return real_is_file(self)

            try:
                with unittest.mock.patch("os.access", _fake_access), \
                        unittest.mock.patch.object(Path, "is_file", _fake_is_file):
                    found = wsc.find_session_claim_cli()
            finally:
                for k, v in env_backup.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

            self.assertEqual(found, cli)


class TestMemoPredecessorLeg(unittest.TestCase):
    """docs/plans/2026-08-05-memo-predecessor-representable-outcome.md chunk
    C5: the memo-predecessor leg's precedence against Detector A/B ("archive"
    leg) and Detector C, the AC6 `cd272f17` reproduction, AC4's settled-fact
    detection shape, AC7's unabsorbed single-session path, and the
    `picked_up_at` temporal gate + numeric-sid quoting corner cases.

    Authored from the plan's Acceptance Criteria and its "THE PRECEDENCE
    CONTRACT" Execution Notes table, not from `find_memo_predecessor`'s own
    implementation — see this file's module docstring discipline note in the
    dispatch brief for why."""

    def _stamp_session_start(self, repo: Path, sid: str) -> None:
        common = _git(repo, "rev-parse", "--git-common-dir").stdout.strip()
        common_dir = Path(common)
        if not common_dir.is_absolute():
            common_dir = repo / common_dir
        claim_dir = common_dir / "coordinator-sessions" / sid
        claim_dir.mkdir(parents=True, exist_ok=True)

    def _write_memo(
        self,
        repo: Path,
        dirname: str,
        name: str,
        picked_up_by: str,
        picked_up_at: str,
        *,
        quote_picked_up_by: bool = False,
    ) -> Path:
        d = repo / "cross-repo" / dirname
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        pub = f"'{picked_up_by}'" if quote_picked_up_by else picked_up_by
        f.write_text(
            "---\n"
            "realized_by: state/sizings/example.yaml\n"
            f"picked_up_at: '{picked_up_at}'\n"
            f"picked_up_by: {pub}\n"
            "---\nbody\n"
        )
        return f

    def test_ac3_archive_leg_wins_over_memo_predecessor(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-01_predecessor.md"
            handoff.write_text("claimed_by: sid-shipper\npredecessor: some-sha\n")
            _git(repo, "add", "archive/handoffs/2026-07-01_predecessor.md")
            _commit_with_session_trailer(repo, "sid-shipper", "archive handoff")

            self._stamp_session_start(repo, "sid-shipper")
            self._write_memo(
                repo, "archive", "2026-07-01-shipper-pickup.md",
                "sid-shipper", "2020-01-01T00:00:00Z",
            )

            result = wsc.resolve_disposition(repo, "sid-shipper")

            self.assertEqual(result.disposition, "predecessor-consumed")
            self.assertNotEqual(result.disposition, "memo-predecessor")
            self.assertEqual(result.consumed_handoff, "archive/handoffs/2026-07-01_predecessor.md")

    def test_ac3_foreign_consumer_guard_rejection_plus_memo_resolves_memo_predecessor(self):
        """AC3's second sub-test as literally written ("a Detector B hit
        REJECTED by `_foreign_consumer_guard`, plus a picked-up memo, still
        emits the existing... WARN") does NOT hold against the actual C2
        implementation, verified empirically rather than assumed: once
        `find_memo_predecessor` returns a match, `resolve_disposition`
        returns `memo-predecessor` at the `if memo_path:` branch (wsc-
        session-disposition.py ~L1219) BEFORE the `shipped_by_me` WARN check
        is ever reached (~L1232) — the WARN branch is structurally
        unreachable whenever a memo matches. This is also the reading THE
        PRECEDENCE CONTRACT actually specifies: the archive leg only wins
        when Detector A/B set `arch`; here the foreign-consumer guard
        rejects the candidate, so `arch` is falsy, and the memo leg — not
        having been preempted by anything — decides. See this chunk's
        report for the discrepancy against the plan's literal AC3 prose."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            archive_dir = repo / "archive" / "handoffs"
            archive_dir.mkdir(parents=True)
            handoff = archive_dir / "2026-07-01_other.md"
            handoff.write_text(
                "---\nclaimed_by: sid-other-session\npredecessor: some-sha\n---\nbody\n"
            )
            _git(repo, "add", "archive/handoffs/2026-07-01_other.md")
            _commit_with_session_trailer(repo, "sid-restorer", "restore handoff")

            self._stamp_session_start(repo, "sid-restorer")
            self._write_memo(
                repo, "inbox", "memo-restorer.md", "sid-restorer", "2020-01-01T00:00:00Z"
            )

            result = wsc.resolve_disposition(repo, "sid-restorer")

            self.assertEqual(result.disposition, "memo-predecessor")
            self.assertTrue(
                any("restoration-commit spoof guard" in d for d in result.diagnostics),
                result.diagnostics,
            )
            self.assertFalse(
                any(
                    "archived a handoff this run" in d and "resolved single-session" in d
                    for d in result.diagnostics
                ),
                result.diagnostics,
            )
            # AC3's operator-facing REQUIREMENT survives its literal wording:
            self.assertTrue(
                any(
                    "archived a handoff this run" in d and "resolved memo-predecessor" in d
                    for d in result.diagnostics
                ),
                result.diagnostics,
            )

    def test_ac3_detector_c_clean_non_coincidence_prone_match_wins_over_memo(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            touched = repo / "coordinator" / "bin" / "widget.py"
            touched.parent.mkdir(parents=True)
            touched.write_text("# widget\n")
            _git(repo, "add", "coordinator/bin/widget.py")
            _commit_with_session_trailer(repo, "sid-crashy2", "touch widget")

            stale_handoff = repo / "state" / "handoffs" / "2026-07-01_dead.md"
            stale_handoff.parent.mkdir(parents=True, exist_ok=True)
            stale_handoff.write_text(
                "predecessor: none\nscope:\n  - coordinator/bin/widget.py\n"
            )

            fake_cli = repo / "fake-session-claim-cli"
            fake_cli.write_text("#!/bin/sh\nexit 0\n")

            self._stamp_session_start(repo, "sid-crashy2")
            self._write_memo(
                repo, "inbox", "memo-crashy2.md", "sid-crashy2", "2020-01-01T00:00:00Z"
            )

            with unittest.mock.patch.object(
                wsc, "find_session_claim_cli", return_value=fake_cli
            ), unittest.mock.patch.object(
                wsc,
                "list_stale_claim_handoffs",
                return_value=([(str(stale_handoff), "dead-sid")], 0),
            ):
                result = wsc.resolve_disposition(repo, "sid-crashy2")

            self.assertEqual(result.disposition, "predecessor-consumed")
            self.assertNotEqual(result.disposition, "memo-predecessor")
            self.assertEqual(result.detection["deciding_leg"], "detector-c")
            self.assertEqual(result.detection["matched_scope_entry_count"], 1)
            self.assertEqual(result.detection["scope_size"], 1)
            self.assertEqual(result.detection["single_match_kind"], "exact")

    def test_ac6_cd272f17_reproduction_coincidence_prone_match_loses_to_memo(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            touched = repo / "coordinator_core" / "workstream_complete" / "__init__.py"
            touched.parent.mkdir(parents=True)
            touched.write_text("# init\n")
            _git(repo, "add", "coordinator_core/workstream_complete/__init__.py")
            _commit_with_session_trailer(repo, "cd272f17", "touch workstream_complete")

            stale_handoff = (
                repo / "archive" / "handoffs" / "2026-07" / "2026-07-17_160001_roadmap-sat-02.md"
            )
            stale_handoff.parent.mkdir(parents=True, exist_ok=True)
            stale_handoff.write_text(
                "---\npredecessor: none\nscope:\n  - coordinator_core/\n---\nbody\n"
            )

            fake_cli = repo / "fake-session-claim-cli"
            fake_cli.write_text("#!/bin/sh\nexit 0\n")

            self._stamp_session_start(repo, "cd272f17")
            self._write_memo(
                repo, "archive", "2026-07-17-roadmap-sat-02-pickup.md",
                "cd272f17", "2020-01-01T00:00:00Z",
            )

            with unittest.mock.patch.object(
                wsc, "find_session_claim_cli", return_value=fake_cli
            ), unittest.mock.patch.object(
                wsc,
                "list_stale_claim_handoffs",
                return_value=([(str(stale_handoff), "dead-sid")], 0),
            ):
                result = wsc.resolve_disposition(repo, "cd272f17")

            self.assertEqual(result.disposition, "memo-predecessor")
            self.assertNotEqual(result.disposition, "predecessor-consumed")
            self.assertEqual(result.detection["deciding_leg"], "memo-predecessor")
            self.assertEqual(
                result.detection["memo_path"],
                "cross-repo/archive/2026-07-17-roadmap-sat-02-pickup.md",
            )
            # memo-predecessor detection record as DIAGNOSTICS ONLY.
            self.assertEqual(result.detection["detector_c_status"], "crash-recovery")
            self.assertEqual(result.detection["matched_scope_entry_count"], 1)
            self.assertEqual(result.detection["scope_size"], 1)
            self.assertEqual(result.detection["single_match_kind"], "prefix")
            self.assertEqual(result.consumed_handoff, "")
            self.assertEqual(result.consumed_handoff_paths, [])

    def test_example_market_data_repo_all_prefix_multi_match_loses_to_memo(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            for rel in ("tests/test_foo.py", "docs/readme.md"):
                touched = repo / rel
                touched.parent.mkdir(parents=True, exist_ok=True)
                touched.write_text("# touched\n")
                _git(repo, "add", rel)
            _commit_with_session_trailer(repo, "sid-mi", "touch tests and docs")

            stale_handoff = repo / "state" / "handoffs" / "2026-08-05_example_market_data_repo.md"
            stale_handoff.parent.mkdir(parents=True, exist_ok=True)
            stale_handoff.write_text(
                "predecessor: none\nscope:\n"
                "  - tests/\n  - docs/\n  - a.py\n  - b.py\n  - c.py\n  - d.py\n  - e.py\n"
            )

            fake_cli = repo / "fake-session-claim-cli"
            fake_cli.write_text("#!/bin/sh\nexit 0\n")

            self._stamp_session_start(repo, "sid-mi")
            self._write_memo(
                repo, "inbox", "memo-mi.md", "sid-mi", "2020-01-01T00:00:00Z"
            )

            with unittest.mock.patch.object(
                wsc, "find_session_claim_cli", return_value=fake_cli
            ), unittest.mock.patch.object(
                wsc,
                "list_stale_claim_handoffs",
                return_value=([(str(stale_handoff), "dead-sid")], 0),
            ):
                result = wsc.resolve_disposition(repo, "sid-mi")

            self.assertEqual(result.disposition, "memo-predecessor")
            self.assertNotEqual(result.disposition, "predecessor-consumed")
            self.assertEqual(result.detection["deciding_leg"], "memo-predecessor")
            self.assertEqual(result.detection["matched_scope_entry_count"], 2)
            self.assertEqual(result.detection["scope_size"], 7)
            self.assertEqual(result.detection["exact_match_count"], 0)

    def test_ac4_memo_predecessor_is_a_settled_fact_not_uncertain(self):
        import tempfile

        from coordinator_core.workstream_complete import _session_shape_is_uncertain

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            self._stamp_session_start(repo, "sid-memo-only")
            self._write_memo(
                repo, "inbox", "memo-only.md", "sid-memo-only", "2020-01-01T00:00:00Z"
            )

            result = wsc.resolve_disposition(repo, "sid-memo-only")

            self.assertEqual(result.disposition, "memo-predecessor")
            self.assertEqual(result.detection["deciding_leg"], "memo-predecessor")
            self.assertEqual(result.detection["memo_path"], "cross-repo/inbox/memo-only.md")
            self.assertEqual(result.consumed_handoff, "")
            self.assertEqual(result.consumed_handoff_paths, [])
            self.assertFalse(_session_shape_is_uncertain(result.detection))

    def test_ac7_no_memo_and_no_handoff_evidence_still_resolves_single_session(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            result = wsc.resolve_disposition(repo, "sid-nobody-memo")

            self.assertEqual(result.disposition, "single-session")
            self.assertEqual(result.consumed_handoff, "")
            self.assertEqual(result.consumed_handoff_paths, [])
            self.assertEqual(result.detection["deciding_leg"], "none")

    def test_temporal_gate_rejects_a_memo_picked_up_after_session_start(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            self._stamp_session_start(repo, "sid-late-pickup")
            self._write_memo(
                repo, "inbox", "memo-late.md", "sid-late-pickup", "2099-01-01T00:00:00Z"
            )

            result = wsc.resolve_disposition(repo, "sid-late-pickup")

            self.assertEqual(result.disposition, "single-session")
            self.assertNotEqual(result.disposition, "memo-predecessor")
            self.assertEqual(result.detection["deciding_leg"], "none")
            self.assertNotIn("memo_path", result.detection)

    def test_quoted_numeric_sid_still_matches_picked_up_by(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            self._stamp_session_start(repo, "123456")
            self._write_memo(
                repo,
                "inbox",
                "memo-numeric-sid.md",
                "123456",
                "2020-01-01T00:00:00Z",
                quote_picked_up_by=True,
            )

            result = wsc.resolve_disposition(repo, "123456")

            self.assertEqual(result.disposition, "memo-predecessor")
            self.assertEqual(result.detection["deciding_leg"], "memo-predecessor")
            self.assertEqual(result.detection["memo_path"], "cross-repo/inbox/memo-numeric-sid.md")

    def test_unresolvable_session_start_with_no_memo_is_byte_identical_to_head(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))

            with unittest.mock.patch.object(
                wsc, "_resolve_memo_session_start_time"
            ) as mock_resolve_start:
                result = wsc.resolve_disposition(repo, "sid-no-memo-anywhere")

            mock_resolve_start.assert_not_called()
            self.assertEqual(result.disposition, "single-session")
            self.assertEqual(result.detection["deciding_leg"], "none")
            self.assertNotIn("memo_path", result.detection)
            self.assertFalse(
                any("memo-predecessor leg did not fire" in msg for msg in result.diagnostics)
            )


class SessionClaimCliArgvTests(unittest.TestCase):

    def test_py_sibling_routes_through_the_interpreter_on_windows(self):
        with unittest.mock.patch.object(wsc.os, "name", "nt"):
            argv = wsc._session_claim_cli_argv(Path("/bin/session-claim-cli.py"))
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("session-claim-cli.py"))

    def test_extensioned_cmd_path_is_invoked_directly_on_windows(self):
        with unittest.mock.patch.object(wsc.os, "name", "nt"):
            argv = wsc._session_claim_cli_argv(Path("/bin/session-claim-cli.cmd"))
        self.assertEqual(len(argv), 1)
        self.assertTrue(argv[0].endswith("session-claim-cli.cmd"))

    def test_posix_installed_bare_name_also_routes_through_the_interpreter(self):
        with unittest.mock.patch.object(wsc.os, "name", "posix"):
            argv = wsc._session_claim_cli_argv(Path("/bin/session-claim-cli"))
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("session-claim-cli"))


class TestSessionIdentityImport(unittest.TestCase):

    def test_import_resolves_to_the_engine_leaf_module(self):
        self.assertIs(
            wsc.session_deliverable_ids,
            __import__(
                "coordinator_core.workstream_complete.session_identity",
                fromlist=["session_deliverable_ids"],
            ).session_deliverable_ids,
        )

    def test_resolve_disposition_does_not_walk_history_for_a_diagnostic(self):
        import tempfile

        calls = []
        real = wsc.session_deliverable_ids

        def _spy(repo_root, session_id):
            calls.append(session_id)
            return real(repo_root, session_id)

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            _git(
                repo,
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "chunk work\n\nSession-Id: sid-dlv\nDeliverable-Id: dlv-c1-probe",
            )
            with unittest.mock.patch.object(wsc, "session_deliverable_ids", _spy):
                wsc.resolve_disposition(repo, "sid-dlv")

        self.assertEqual(
            calls,
            [],
            "resolve_disposition walked history via session_deliverable_ids; the "
            "diagnostic-only call was gravestoned for costing 296.9ms of unbounded "
            "`git log` on the close path (see the gravestone in "
            "coordinator/bin/wsc-session-disposition.py)",
        )

    def test_disposition_diagnostics_carry_no_deliverable_id_note(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            _commit_with_session_trailer(repo, "sid-no-dlv", "chunk work, no deliverable id")
            result = wsc.resolve_disposition(repo, "sid-no-dlv")
        self.assertFalse(
            any("Deliverable-Id" in line for line in result.diagnostics),
            f"expected no Deliverable-Id NOTE, got: {result.diagnostics!r}",
        )


class TestCoincidenceProneCrashRecoveryIsNotAdopted(unittest.TestCase):
    """C4 (2026-08-20-wsc-identity-gates-key-on-the-deliverable, item 1
    AC1): `resolve_disposition` no longer adopts a coincidence-prone
    Detector C crash-recovery match as "predecessor-consumed" when no
    memo-predecessor corroborates it — it falls through to the shared
    "single-session" return instead. Drives the observed shape verbatim:
    `matched_scope_entry_count: 1`, `scope_size: 9`, `single_match_kind:
    "exact"` (so `exact_match_count == 1` and `scope_size >= 2` —
    coincidence-prone per `is_coincidence_prone_detection`), no memo.

    Does NOT touch `is_coincidence_prone_detection` or `detector_c`'s
    matching logic — see the AMENDMENT BREADCRUMB on `resolve_disposition`
    for why this is the caller's adoption, not Detector C's matching."""

    def setUp(self):
        import os

        self._backup = {
            k: os.environ.pop(k, None) for k in ("WSC_DISPOSITION", "WSC_CONSUMED_HANDOFF")
        }

    def tearDown(self):
        import os

        for k, v in self._backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _build_repo_with_coincidence_prone_match(self, tmp: str) -> Path:
        repo = _init_repo_with_history(Path(tmp))
        touched = repo / "coordinator" / "bin" / "widget.py"
        touched.parent.mkdir(parents=True)
        touched.write_text("# widget\n")
        _git(repo, "add", "coordinator/bin/widget.py")
        _commit_with_session_trailer(repo, "sid-coincidence", "touch widget")

        stale_handoff = repo / "state" / "handoffs" / "2026-07-01_dead.md"
        stale_handoff.parent.mkdir(parents=True, exist_ok=True)
        scope_lines = "\n".join(f"  - unrelated/path-{i}.py" for i in range(8))
        stale_handoff.write_text(
            "predecessor: none\nscope:\n"
            "  - coordinator/bin/widget.py\n" + scope_lines + "\n"
        )
        return repo, stale_handoff

    def test_coincidence_prone_match_does_not_resolve_predecessor_consumed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo, stale_handoff = self._build_repo_with_coincidence_prone_match(tmp)
            fake_cli = repo / "fake-session-claim-cli"
            fake_cli.write_text("#!/bin/sh\nexit 0\n")

            with unittest.mock.patch.object(
                wsc, "find_session_claim_cli", return_value=fake_cli
            ), unittest.mock.patch.object(
                wsc,
                "list_stale_claim_handoffs",
                return_value=([(str(stale_handoff), "dead-sid")], 0),
            ):
                result = wsc.resolve_disposition(repo, "sid-coincidence")

            self.assertEqual(result.detection.get("matched_scope_entry_count"), 1)
            self.assertEqual(result.detection.get("scope_size"), 9)
            self.assertEqual(result.detection.get("single_match_kind"), "exact")
            self.assertNotEqual(result.disposition, "predecessor-consumed")
            self.assertEqual(result.disposition, "single-session")
            self.assertEqual(result.consumed_handoff, "")

    def test_downgrade_path_still_flags_chain_end_as_uncertain(self):
        import tempfile

        from coordinator_core.workstream_complete import _session_shape_is_uncertain

        with tempfile.TemporaryDirectory() as tmp:
            repo, stale_handoff = self._build_repo_with_coincidence_prone_match(tmp)
            fake_cli = repo / "fake-session-claim-cli"
            fake_cli.write_text("#!/bin/sh\nexit 0\n")

            with unittest.mock.patch.object(
                wsc, "find_session_claim_cli", return_value=fake_cli
            ), unittest.mock.patch.object(
                wsc,
                "list_stale_claim_handoffs",
                return_value=([(str(stale_handoff), "dead-sid")], 0),
            ):
                result = wsc.resolve_disposition(repo, "sid-coincidence")

            self.assertEqual(result.disposition, "single-session")
            self.assertEqual(result.detection.get("deciding_leg"), "detector-c")
            self.assertEqual(result.detection.get("detector_c_status"), "crash-recovery")
            self.assertTrue(
                _session_shape_is_uncertain(result.detection),
                f"expected the downgraded single-session shape to still read as uncertain "
                f"(chain-end coverage must not be silently skipped), got detection: "
                f"{result.detection!r}",
            )


class TestDeliverableIdJoinPreferredOverScopePath(unittest.TestCase):

    def test_ac2_real_baton_with_matching_id_wins_over_scope_sharing_stranger(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            touched = repo / "coordinator_core" / "authz" / "classification.py"
            touched.parent.mkdir(parents=True)
            touched.write_text("# classification\n")
            _git(repo, "add", "coordinator_core/authz/classification.py")
            _git(
                repo,
                "commit",
                "-q",
                "-m",
                "chunk work\n\nSession-Id: sid-c5\nDeliverable-Id: "
                "dlv-one-warm-attempt-for-every-cli-not-one-p-bd161b",
            )

            real_predecessor = repo / "state" / "handoffs" / "2026-08-20_102353_a-refusal-cannot-exit-zero.md"
            real_predecessor.parent.mkdir(parents=True, exist_ok=True)
            real_predecessor.write_text(
                "---\n"
                "predecessor: none\n"
                "deliverable_id: \"dlv-one-warm-attempt-for-every-cli-not-one-p-bd161b\"\n"
                "scope:\n"
                "  - coordinator_core/authz/some_other_file.py\n"
                "---\n"
            )

            stranger = repo / "state" / "handoffs" / "2026-08-20-sat-06-cockpit-consumption-seam.md"
            stranger.write_text(
                "---\n"
                "predecessor: none\n"
                "scope:\n"
                "  - coordinator_core/authz/classification.py\n"
                "---\n"
            )

            fake_cli = repo / "fake-session-claim-cli"
            fake_cli.write_text("#!/bin/sh\nexit 0\n")

            with unittest.mock.patch.object(
                wsc, "find_session_claim_cli", return_value=fake_cli
            ), unittest.mock.patch.object(
                wsc,
                "list_stale_claim_handoffs",
                return_value=(
                    [(str(real_predecessor), "dead-real"), (str(stranger), "dead-stranger")],
                    0,
                ),
            ):
                result = wsc.resolve_disposition(repo, "sid-c5")

            self.assertEqual(result.disposition, "predecessor-consumed")
            self.assertEqual(
                result.consumed_handoff,
                "state/handoffs/2026-08-20_102353_a-refusal-cannot-exit-zero.md",
            )
            self.assertNotIn(
                "state/handoffs/2026-08-20-sat-06-cockpit-consumption-seam.md",
                result.consumed_handoff,
            )
            self.assertEqual(result.detection["deciding_leg"], "detector-c")
            self.assertTrue(
                any("deliverable_id join" in line for line in result.diagnostics),
                f"expected a deliverable_id-join NOTE, got: {result.diagnostics!r}",
            )

    def test_ambiguous_session_side_falls_back_to_scope_path(self):
        """The session's own commits carry two CONFLICTING Deliverable-Id
        trailer values — the join must not pick either baton; it reports
        and falls through to `None`, letting `detector_c` run the scope-path
        leg instead."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            _git(repo, "commit", "-q", "--allow-empty", "-m", "w1\n\nSession-Id: sid-ambig\nDeliverable-Id: dlv-a")
            _git(repo, "commit", "-q", "--allow-empty", "-m", "w2\n\nSession-Id: sid-ambig\nDeliverable-Id: dlv-b")

            baton = repo / "state" / "handoffs" / "baton-a.md"
            baton.parent.mkdir(parents=True, exist_ok=True)
            baton.write_text('---\ndeliverable_id: "dlv-a"\n---\n')

            diagnostics: list[str] = []
            outcome = wsc._resolve_deliverable_id_join(
                repo, "sid-ambig", [(str(baton), "dead-sid")], diagnostics
            )
            self.assertIsNone(outcome)
            self.assertTrue(
                any("conflicting Deliverable-Id" in line for line in diagnostics),
                f"expected an ambiguity NOTE, got: {diagnostics!r}",
            )

    def test_ambiguous_baton_side_falls_back_to_scope_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            _git(repo, "commit", "-q", "--allow-empty", "-m", "w1\n\nSession-Id: sid-dupe\nDeliverable-Id: dlv-shared")

            baton1 = repo / "state" / "handoffs" / "baton-1.md"
            baton2 = repo / "state" / "handoffs" / "baton-2.md"
            baton1.parent.mkdir(parents=True, exist_ok=True)
            baton1.write_text('---\ndeliverable_id: "dlv-shared"\n---\n')
            baton2.write_text('---\ndeliverable_id: "dlv-shared"\n---\n')

            diagnostics: list[str] = []
            outcome = wsc._resolve_deliverable_id_join(
                repo, "sid-dupe", [(str(baton1), "dead-1"), (str(baton2), "dead-2")], diagnostics
            )
            self.assertIsNone(outcome)
            self.assertTrue(
                any("2 stale batons" in line for line in diagnostics),
                f"expected an ambiguous-baton NOTE, got: {diagnostics!r}",
            )

    def test_no_trailer_resolves_falls_back_to_scope_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_repo_with_history(Path(tmp))
            _commit_with_session_trailer(repo, "sid-no-dlv-join", "plain commit")

            baton = repo / "state" / "handoffs" / "baton.md"
            baton.parent.mkdir(parents=True, exist_ok=True)
            baton.write_text('---\ndeliverable_id: "dlv-whatever"\n---\n')

            diagnostics: list[str] = []
            outcome = wsc._resolve_deliverable_id_join(
                repo, "sid-no-dlv-join", [(str(baton), "dead-sid")], diagnostics
            )
            self.assertIsNone(outcome)


if __name__ == "__main__":
    unittest.main()
