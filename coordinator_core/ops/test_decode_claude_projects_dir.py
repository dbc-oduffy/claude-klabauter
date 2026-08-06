"""Tests for coordinator_core.ops.decode_claude_projects_dir.

Golden-oracle parity fixtures were captured 2026-07-16 by running the bash
original against a real ~/.claude/projects/ tree and two negative corpora
(missing dir, dir with zero decodable entries). See module docstring
Negative-spec for the documented divergences (line order; zero-candidates
stderr text).

Port of: decode-claude-projects-dir.sh (example-doctrine-repo b5a4192c, 2026-07-20)
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.decode_claude_projects_dir import _decode_one, _run, main


def test_decode_one_basic_drive_prefix():
    assert _decode_one("X--coordinator-claude") == ("coordinator-claude", "x:/coordinator-claude")


def test_decode_one_drops_subdir_suffix():
    assert _decode_one("X---repo-name--tasks--foo") == ("repo-name", "x:/repo-name")


def test_decode_one_claude_central_double_dash_form():
    assert _decode_one("C--Users-example-operator--claude") == ("claude-central", "c:/Users/example-operator/.claude")


def test_decode_one_claude_central_triple_dash_form():
    assert _decode_one("C---Users--example-operator---claude") == ("claude-central", "c:/Users/example-operator/.claude")


def test_decode_one_skips_garbled_entries():
    assert _decode_one("tmp") is None
    assert _decode_one("tmp-foo") is None
    assert _decode_one("some-smoketest") is None
    assert _decode_one("--Users-example-operator---claude") is None
    assert _decode_one("AppData-Local-Temp-foo") is None


def test_decode_one_skips_no_drive_prefix():
    assert _decode_one("no-drive-prefix-here") is None


def test_decode_one_skips_short_shortname():
    # rest="-foo" after a single-letter split -> shortname length < 2
    assert _decode_one("X--a") is None


def test_run_projects_dir_not_found(tmp_path):
    missing = tmp_path / "nonexistent"
    stdout_lines, stderr_lines, exit_code = _run(str(missing))
    assert exit_code == 1
    assert stdout_lines == []
    assert any("projects dir not found" in line for line in stderr_lines)


def test_run_zero_candidates_reproduces_oracle_crash_contract(tmp_path):
    # Empty dir (no subdirs at all) -> golden oracle crashes with "unbound
    # variable" under set -u before reaching its intended WARNING/exit-2
    # branch. This port reproduces exit 1 / no WARNING text, not the
    # bash-specific crash message (see module Negative-spec).
    empty = tmp_path / "empty"
    empty.mkdir()
    stdout_lines, stderr_lines, exit_code = _run(str(empty))
    assert exit_code == 1
    assert stdout_lines == []
    assert not any("WARNING" in line for line in stderr_lines)


def test_run_zero_candidates_all_filtered_reproduces_oracle_crash_contract(tmp_path):
    root = tmp_path / "skipall"
    (root / "tmp").mkdir(parents=True)
    stdout_lines, stderr_lines, exit_code = _run(str(root))
    assert exit_code == 1
    assert stdout_lines == []


def test_run_positive_corpus_dedupe_and_content(tmp_path):
    for name in (
        "X--coordinator-claude",
        "X--example-sim-repo",
        "C--Users-example-operator--claude",
        "X---repo-name--tasks--sub",
        "X---repo-name",  # first-encoding-wins: this one should be dropped by suffix-collapse rule only if seen first differs
        "tmp",
        "some-smoketest",
    ):
        (tmp_path / name).mkdir()

    stdout_lines, stderr_lines, exit_code = _run(str(tmp_path))
    assert exit_code == 0
    joined = "\n".join(stdout_lines)
    assert "coordinator-claude\tx:/coordinator-claude\tX--coordinator-claude" in joined
    assert "example-sim-repo\tx:/example-sim-repo\tX--example-sim-repo" in joined
    assert "claude-central\tc:/Users/example-operator/.claude\tC--Users-example-operator--claude" in joined
    assert any(line.startswith("repo-name\t") for line in stdout_lines)
    # dedupe: only one repo-name line survives (first-encoding-wins)
    assert sum(1 for line in stdout_lines if line.startswith("repo-name\t")) == 1
    assert any("candidate(s) emitted" in line for line in stderr_lines)


def test_run_output_deterministically_sorted_by_shortname(tmp_path):
    for name in ("X--zzz-repo", "X--aaa-repo", "X--mmm-repo"):
        (tmp_path / name).mkdir()
    stdout_lines, _, exit_code = _run(str(tmp_path))
    assert exit_code == 0
    shortnames = [line.split("\t", 1)[0] for line in stdout_lines]
    assert shortnames == sorted(shortnames)


def test_main_defaults_to_home_claude_projects(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    projects = fake_home / ".claude" / "projects"
    projects.mkdir(parents=True)
    (projects / "X--somerepo").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    rc = main([])
    assert rc == 0
