"""Characterization tests for coordinator_core.ops.scope_warning_resolve.

Ports the bash oracle 1:1 (9 cases), plus last-field/pipe-anchoring and
no-op-detection cases the bash suite implied but didn't directly assert.

Port of: test-scope-warning-resolve.sh (DoE 67202df6, 2026-07-16)
Spec backlink: DoE scratch/subagent-sandbox/bash-to-python-engine-migration/
    recipe-small-bin-clis-gate-aging-scope-soak-scope-warning.md § 3
"""
from __future__ import annotations

import os

import pytest

from coordinator_core.ops.scope_warning_resolve import VALID_RESOLUTIONS, main, resolve

_LOG_LINES = [
    "2026-04-27T10:00:00Z | test-session-1 | foreign-staged | plugins/foo.sh | owner:orphan | pending-resolution\n",
    "2026-04-27T10:01:00Z | test-session-1 | foreign-staged | plugins/bar.sh | owner:session abc-456 | pending-resolution\n",
    "2026-04-27T10:02:00Z | test-session-1 | foreign-staged | plugins/baz.sh | owner:orphan | pending-resolution\n",
]


def _make_repo(tmp_path, session_id: str = "test-session-1"):
    sess_dir = tmp_path / ".git" / "coordinator-sessions" / session_id
    sess_dir.mkdir(parents=True)
    log = sess_dir / "scope-warnings.log"
    log.write_text("".join(_LOG_LINES), encoding="utf-8")
    return str(tmp_path), log


# ---------------------------------------------------------------------------
# Ported cases 1-6 — happy-path resolutions + sibling-line isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line_no,resolution",
    [(1, "legitimate-mine"), (2, "not-mine-unstaged"), (3, "not-mine-committed-anyway"),
     (1, "orphan-claimed"), (1, "orphan-rejected")],
)
def test_1_to_5_resolution_written_to_target_line(tmp_path, line_no, resolution):
    root, log = _make_repo(tmp_path)
    text, rc = resolve("test-session-1", str(line_no), resolution, git_root=root)
    assert rc == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    assert resolution in lines[line_no - 1]


def test_6_sibling_lines_untouched(tmp_path):
    root, log = _make_repo(tmp_path)
    resolve("test-session-1", "1", "legitimate-mine", git_root=root)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert "pending-resolution" in lines[1]
    assert "pending-resolution" in lines[2]


# ---------------------------------------------------------------------------
# Ported cases 7-9 — error paths
# ---------------------------------------------------------------------------


def test_7_line_number_beyond_file_length_exits_nonzero(tmp_path):
    root, _log = _make_repo(tmp_path)
    text, rc = resolve("test-session-1", "99", "legitimate-mine", git_root=root)
    assert rc != 0


def test_8_invalid_resolution_code_exits_nonzero(tmp_path):
    root, _log = _make_repo(tmp_path)
    text, rc = resolve("test-session-1", "1", "not-a-real-resolution", git_root=root)
    assert rc != 0


def test_9_missing_log_file_exits_nonzero(tmp_path):
    (tmp_path / ".git" / "coordinator-sessions" / "test-session-1").mkdir(parents=True)
    text, rc = resolve("test-session-1", "1", "legitimate-mine", git_root=str(tmp_path))
    assert rc != 0


# ---------------------------------------------------------------------------
# Additional characterization — validation order, pipe-anchoring, no-op
# ---------------------------------------------------------------------------


def test_line_number_format_rejected_before_other_checks(tmp_path):
    root, _log = _make_repo(tmp_path)
    # both an invalid line-number format AND an invalid resolution — format
    # check must win (matches bash validation order: format before allowlist)
    text, rc = resolve("test-session-1", "0", "not-a-real-resolution", git_root=root)
    assert rc == 1


def test_not_in_git_repo_exits_1(capsys):
    text, rc = resolve("sess", "1", "legitimate-mine", git_root="")
    captured = capsys.readouterr()
    assert rc == 1
    assert "not inside a git repository" in captured.err


def test_last_field_anchoring_survives_embedded_pipes(tmp_path):
    # A field earlier in the line containing a literal '|' character must
    # not mis-anchor the last-field replace — $NF in awk / fields[-1] in
    # Python both anchor on the LAST '|'-delimited segment regardless of
    # how many appear earlier.
    root, log = _make_repo(tmp_path)
    log.write_text(
        "ts | sess | foreign-staged | msg with a | pipe in it | owner:orphan | pending-resolution\n",
        encoding="utf-8",
    )
    text, rc = resolve("test-session-1", "1", "legitimate-mine", git_root=root)
    assert rc == 0
    result = log.read_text(encoding="utf-8")
    assert result.strip().endswith("legitimate-mine")
    assert "msg with a | pipe in it" in result


def test_noop_when_content_already_matches(tmp_path):
    root, log = _make_repo(tmp_path)
    resolve("test-session-1", "1", "legitimate-mine", git_root=root)
    before = log.read_text(encoding="utf-8")
    text, rc = resolve("test-session-1", "1", "legitimate-mine", git_root=root)
    after = log.read_text(encoding="utf-8")
    assert rc == 0
    assert text == ""  # no "Resolved:" stdout on the no-op path
    assert before == after


def test_valid_resolutions_allowlist_is_exact():
    assert VALID_RESOLUTIONS == (
        "legitimate-mine",
        "not-mine-unstaged",
        "not-mine-committed-anyway",
        "orphan-claimed",
        "orphan-rejected",
    )


def test_main_argc_mismatch_exits_1(capsys):
    rc = main(["only-one-arg"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "expected 3 arguments, got 1" in captured.err
    assert "Usage: scope-warning-resolve" in captured.err


def test_main_happy_path(tmp_path, monkeypatch, capsys):
    import subprocess

    root, log = _make_repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.chdir(root)
    rc = main(["test-session-1", "1", "legitimate-mine"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Resolved: line 1" in captured.out
