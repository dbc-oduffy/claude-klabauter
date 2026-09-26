"""Tests for ``coordinator_core.bash_guards._guard_coverage`` -- the
standing, re-runnable per-guard coverage measurement built for BX-11 / AC-6
(docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md,
DoE-claude).

Uses a small, hand-authored synthetic corpus throughout -- never the real
62,487-command transcript corpus behind the 2026-07-28 baseline, which is
real session data and is never committed to this repo. These tests pin the
MEASUREMENT MACHINERY's correctness (target-class predicates, the
stateless-vs-stateful measurement split, the corpus reader), not the
baseline percentages themselves -- the baseline is a fixed historical
reference (``BASELINE_PCT``), not something a unit test re-derives.
"""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.bash_guards import _guard_coverage as cov
from coordinator_core.bash_guards import dispatch_checks as guard


class TestTargetClassPredicates:
    def test_is_find_invocation_true_for_find_command(self):
        assert cov.is_find_invocation("find / -name x") is True

    def test_is_find_invocation_true_when_find_not_first_command(self):
        assert cov.is_find_invocation("echo hi; find . -name x") is True

    def test_is_find_invocation_false_when_find_only_quoted(self):
        assert cov.is_find_invocation('echo "please find the file"') is False

    def test_is_find_invocation_false_for_unrelated_command(self):
        assert cov.is_find_invocation("git status") is False

    def test_is_find_invocation_fails_closed_on_unparseable_command(self):
        assert cov.is_find_invocation("find 'unterminated") is False

    def test_is_leading_cd_true_for_cd_prefixed_command(self):
        assert cov.is_leading_cd("cd /repo && git log -1") is True

    def test_is_leading_cd_false_when_cd_not_first(self):
        assert cov.is_leading_cd("git status; cd /repo") is False

    def test_is_leading_cd_false_for_unrelated_command(self):
        assert cov.is_leading_cd("ls -la") is False

    def test_is_leading_cd_fails_closed_on_unparseable_command(self):
        assert cov.is_leading_cd("cd 'unterminated") is False


class TestCorpusReader:
    def test_iter_corpus_commands_reads_c_field(self, tmp_path):
        p = tmp_path / "corpus.jsonl"
        p.write_text('{"c": "git status"}\n{"c": "ls -la"}\n')
        assert list(cov.iter_corpus_commands(str(p))) == ["git status", "ls -la"]

    def test_iter_corpus_commands_accepts_command_key_fallback(self, tmp_path):
        p = tmp_path / "corpus.jsonl"
        p.write_text('{"command": "pwd"}\n')
        assert list(cov.iter_corpus_commands(str(p))) == ["pwd"]

    def test_iter_corpus_commands_skips_malformed_lines(self, tmp_path):
        p = tmp_path / "corpus.jsonl"
        p.write_text('not json\n{"c": "pwd"}\n\n')
        assert list(cov.iter_corpus_commands(str(p))) == ["pwd"]

    def test_iter_corpus_commands_falls_back_past_a_null_c_value(self, tmp_path):
        p = tmp_path / "corpus.jsonl"
        p.write_text('{"c": null, "command": "ls -la"}\n')
        assert list(cov.iter_corpus_commands(str(p))) == ["ls -la"]

    def test_iter_corpus_commands_falls_back_past_a_null_command_value_to_cmd(self, tmp_path):
        p = tmp_path / "corpus.jsonl"
        p.write_text('{"command": null, "cmd": "pwd"}\n')
        assert list(cov.iter_corpus_commands(str(p))) == ["pwd"]


class TestMeasureRunawayFind:
    def test_target_class_and_fired_counts(self):
        commands = [
            "find / -name x",
            "find coordinator -name x",
            "git status",
        ]
        result = cov.measure_runaway_find(commands)
        assert result.guard == "check_runaway_find"
        assert result.target_class_size == 2
        assert result.fired_count == 1
        assert result.measured_pct == pytest.approx(50.0)
        assert result.baseline_pct == cov.BASELINE_PCT["check_runaway_find"]

    def test_bare_home_anchor_is_now_caught(self):
        assert guard.check_runaway_find("find ~ -name x") is not None

    def test_home_anchor_variants_are_caught(self):
        assert guard.check_runaway_find("find ~/ -name x") is not None
        assert guard.check_runaway_find("find ~/subdir -name x") is None

    def test_home_env_var_token_deliberately_not_caught(self):
        assert guard.check_runaway_find("find $HOME -name x") is None

    def test_zero_target_class_reports_zero_pct_not_a_crash(self):
        result = cov.measure_runaway_find(["git status", "ls -la"])
        assert result.target_class_size == 0
        assert result.fired_count == 0
        assert result.measured_pct == 0.0


class TestMeasureOfferGitC:
    def test_target_class_and_fired_counts(self):
        commands = [
            "cd /repo && git log -1",
            "cd /repo && ls -la",
            "git status",
        ]
        result = cov.measure_offer_git_c(commands)
        assert result.guard == "check_offer_git_c"
        assert result.target_class_size == 2
        assert result.fired_count == 1
        assert result.measured_pct == pytest.approx(50.0)


class TestMeasureAllAndReport:
    def test_measure_all_returns_one_result_per_guard(self):
        results = cov.measure_all(["echo hi", "find / -name x", "cd /repo && git log -1"])
        assert {r.guard for r in results} == {
            "check_runaway_find",
            "check_offer_git_c",
        }

    def test_format_report_names_every_guard(self):
        results = cov.measure_all(["echo hi"])
        report = cov.format_report(results)
        for name in ("check_runaway_find", "check_offer_git_c"):
            assert name in report


class TestMainCliFailsLoudWithoutACorpus:
    def test_main_returns_nonzero_when_no_corpus_available(self, monkeypatch, capsys):
        monkeypatch.delenv("COORDINATOR_GUARD_COVERAGE_CORPUS", raising=False)
        rc = cov.main([])
        assert rc != 0
        captured = capsys.readouterr()
        assert "No corpus supplied" in captured.err

    def test_main_reports_against_a_real_file(self, tmp_path, capsys):
        p = tmp_path / "corpus.jsonl"
        p.write_text('{"c": "echo hi"}\n{"c": "find / -name x"}\n')
        rc = cov.main(["--corpus", str(p)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "check_runaway_find" in out

    def test_main_honours_env_var_fallback(self, tmp_path, monkeypatch, capsys):
        p = tmp_path / "corpus.jsonl"
        p.write_text('{"c": "echo hi"}\n')
        monkeypatch.setenv("COORDINATOR_GUARD_COVERAGE_CORPUS", str(p))
        rc = cov.main([])
        assert rc == 0

    def test_main_fails_loud_not_a_traceback_on_nonexistent_explicit_corpus(self, tmp_path, capsys):
        missing = tmp_path / "does-not-exist.jsonl"
        rc = cov.main(["--corpus", str(missing)])
        assert rc == 2
        captured = capsys.readouterr()
        assert "does not exist" in captured.err
        assert "No corpus supplied" in captured.err
