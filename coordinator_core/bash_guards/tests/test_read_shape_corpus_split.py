"""Tests for ``coordinator_core.bash_guards.read_shape_corpus_split`` -- the
C5 measurement (docs/plans/2026-08-22-a-bash-call-stops-costing-a-second-and-a-half.md).

Uses a small, hand-authored synthetic corpus throughout -- never the real
transcript corpus, which is real session data and is never committed to this
repo. These tests pin the MEASUREMENT MACHINERY (target-class recognition,
the decline-cause bucketing, the corpus reader/CLI reuse), not any particular
percentage against real session history.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.bash_guards import read_shape_corpus_split as split


class TestIsReadShaped:
    def test_recognizes_each_family(self):
        assert split.is_read_shaped("cat f.txt") == "cat"
        assert split.is_read_shaped("head -n 5 f.txt") == "head"
        assert split.is_read_shaped("tail f.txt") == "tail"
        assert split.is_read_shaped("sed -n '1,5p' f.txt") == "sed"
        assert split.is_read_shaped("ls -la") == "ls"

    def test_false_for_unrelated_command(self):
        assert split.is_read_shaped("git status") is None

    def test_false_when_read_command_is_receiving_end_of_pipe(self):
        # "cat" here is fed by the upstream command's stdout, not a file
        # operand -- its "input" does not exist independent of `echo hi`.
        assert split.is_read_shaped("echo hi | cat") is None

    def test_true_when_read_command_is_first_and_pipes_out(self):
        assert split.is_read_shaped("cat f.txt | wc -l") == "cat"

    def test_fails_closed_on_unparseable_command(self):
        assert split.is_read_shaped("cat 'unterminated") is None

    def test_not_first_command_after_semicolon_is_not_recognized(self):
        # `is_read_shaped` only looks at the FIRST segment, mirroring
        # plan_for's own precondition.
        assert split.is_read_shaped("echo hi; cat f.txt") is None


class TestDeclineCauseStructuralBuckets:
    def test_shell_construct_join(self):
        assert split.decline_cause("cat a.txt; echo done", "cat") == "shell_construct"

    def test_redirect_operand(self):
        assert split.decline_cause("cat a.txt > out.txt", "cat") == "redirect_or_substitution"

    def test_command_substitution_operand(self):
        assert split.decline_cause("cat $(echo a.txt)", "cat") == "redirect_or_substitution"

    def test_glob_operand(self):
        assert split.decline_cause("cat *.txt", "cat") == "glob_or_brace"

    def test_brace_operand(self):
        assert split.decline_cause("cat {a,b}.txt", "cat") == "glob_or_brace"

    def test_unmodelled_cat_flag(self):
        assert split.decline_cause("cat -n a.txt", "cat") == "unmodelled_flag"

    def test_unmodelled_ls_flag(self):
        assert split.decline_cause("ls -l", "ls") == "unmodelled_flag"

    def test_ls_allows_combined_a1_flags(self):
        # -1a / -a1 explicitly modelled -- must not bucket as unmodelled.
        assert split.decline_cause("ls -1a /does/not/exist", "ls") == "nonexistent_path"

    def test_head_tail_multiple_operands(self):
        assert split.decline_cause("head a.txt b.txt", "head") == "multiple_operands"

    def test_missing_operand_stdin_dash(self):
        assert split.decline_cause("cat -", "cat") == "missing_operand"

    def test_sed_without_dash_n_declines(self):
        assert split.decline_cause("sed '1,5p' a.txt", "sed") == "unmodelled_flag"

    def test_sed_program_shape_not_a_print_range(self):
        assert split.decline_cause("sed -n 's/a/b/' a.txt", "sed") == "sed_program_shape"

    def test_sed_trailing_q_declines(self):
        assert split.decline_cause("sed -n '20,40p;40q' a.txt", "sed") == "sed_program_shape"

    def test_sed_multiple_operands(self):
        assert split.decline_cause("sed -n '1,5p' a.txt b.txt", "sed") == "multiple_operands"


class TestDeclineCausePathChecks:
    def test_nonexistent_path(self):
        assert split.decline_cause("cat /definitely/not/a/real/path.txt", "cat") == "nonexistent_path"

    def test_directory_given_to_cat_is_not_a_regular_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        d = tmp_path / "adir"
        d.mkdir()
        assert split.decline_cause("cat adir", "cat") == "not_a_regular_file"

    def test_file_given_to_ls_is_not_a_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "a.txt"
        f.write_text("hi")
        assert split.decline_cause("ls a.txt", "ls") == "not_a_directory"

    def test_valid_existing_file_is_not_yet_implemented(self, tmp_path, monkeypatch):
        """A structurally well-formed, existing-file `cat` call is not
        answered today only because the read source (C1) has not landed --
        the bucket names that distinctly from every other decline cause."""
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "a.txt"
        f.write_text("hi")
        assert split.decline_cause("cat a.txt", "cat") == "not_yet_implemented"

    def test_bare_ls_of_cwd_is_not_yet_implemented(self):
        assert split.decline_cause("ls", "ls") == "not_yet_implemented"


class TestMeasureSplit:
    def test_counts_and_buckets_over_a_synthetic_corpus(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "a.txt"
        existing.write_text("hi")

        commands = [
            "cat a.txt",                        # read-shaped AND answered, once C3 wired recognition
            "cat -n a.txt",                     # read-shaped, unmodelled_flag
            "ls -l",                            # read-shaped, unmodelled_flag
            "grep -n foo a.txt",                # answered by plan_for's grep path, not read-shaped
            "git status",                       # not read-shaped at all
        ]
        report = split.measure_split(commands)

        assert report.corpus_size == 5
        assert report.read_shaped_count == 3
        # `cat a.txt` counts as ANSWERED here, and that is the point of AC7 rather than an
        # accident of ordering. C5 landed in the same wave as C1 but ahead of C3, so when
        # this case was written no read shape was wired into `plan_for` yet and every one
        # of them bucketed as `not_yet_implemented`. Once C3 wired recognition, the split
        # started reporting what is actually served -- which is the measurement AC7 asks
        # for. Pinning the pre-C3 numbers here would have made this test assert the
        # absence of the feature the plan exists to add.
        assert report.answered_count == 1
        assert report.remainder_count == 2
        assert report.cause_counts.get("not_yet_implemented", 0) == 0
        assert report.cause_counts["unmodelled_flag"] == 2

    def test_answered_count_reflects_real_plan_for_not_a_reimplementation(self, tmp_path, monkeypatch):
        """`measure_split` must call the SHIPPED `search.answer.plan_for`,
        not a re-derivation of its predicate -- proven here by monkeypatching
        the real function and observing the count move."""
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "a.txt"
        f.write_text("hi")

        from coordinator_core.bash_guards import read_shape_corpus_split as split_mod
        from coordinator_core.search import answer as answer_mod

        monkeypatch.setattr(answer_mod, "plan_for", lambda cmd: object())

        report = split_mod.measure_split(["cat a.txt"])
        assert report.answered_count == 1
        assert report.remainder_count == 0

    def test_zero_read_shaped_reports_zero_pct_not_a_crash(self):
        report = split.measure_split(["git status", "echo hi"])
        assert report.read_shaped_count == 0
        assert report.answered_pct_of_read_shaped == 0.0
        assert report.remainder_count == 0


class TestFormatReport:
    def test_report_names_every_family_and_cause(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        report = split.measure_split(["cat -n missing.txt", "ls -l"])
        text = split.format_report(report)
        assert "cat" in text
        assert "ls" in text
        assert "unmodelled_flag" in text


class TestMainCliFailsLoudWithoutACorpus:
    def test_main_returns_nonzero_when_no_corpus_available(self, monkeypatch, capsys):
        monkeypatch.delenv("COORDINATOR_GUARD_COVERAGE_CORPUS", raising=False)
        rc = split.main([])
        assert rc != 0
        captured = capsys.readouterr()
        assert "No corpus supplied" in captured.err

    def test_main_reports_against_a_real_file(self, tmp_path, capsys):
        p = tmp_path / "corpus.jsonl"
        p.write_text('{"c": "cat missing.txt"}\n{"c": "git status"}\n')
        rc = split.main(["--corpus", str(p)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "read_shaped" in out

    def test_main_honours_env_var_fallback(self, tmp_path, monkeypatch, capsys):
        p = tmp_path / "corpus.jsonl"
        p.write_text('{"c": "echo hi"}\n')
        monkeypatch.setenv("COORDINATOR_GUARD_COVERAGE_CORPUS", str(p))
        rc = split.main([])
        assert rc == 0

    def test_main_fails_loud_not_a_traceback_on_nonexistent_explicit_corpus(self, tmp_path, capsys):
        missing = tmp_path / "does-not-exist.jsonl"
        rc = split.main(["--corpus", str(missing)])
        assert rc == 2
        captured = capsys.readouterr()
        assert "does not exist" in captured.err
        assert "No corpus supplied" in captured.err
