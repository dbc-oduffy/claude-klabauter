"""
Tests for coordinator_core.orient_assemble.readers_health_reaper's
`_read_reaper_dry_run` — the in-process replacement for the deleted
`_REAP_SUBPROCESS_EXCEPTION` subprocess call.

Spec backlink: docs/plans/2026-08-26-two-callers-want-two-numbers-not-a-1301-line-cli.md
chunk C2.

Negative-spec:
    - Does NOT spawn a subprocess anywhere in this module — `_read_reaper_dry_run`
      calls `reap_in_flight_claims.survey()` directly, in-process. Every test here
      patches `_reap_survey` rather than touching the filesystem corpus `survey()`
      itself reads.
    - Does NOT re-implement the deleted `_REAP_WOULD_RELEASE_RE` /
      `_REAP_WOULD_RECLAIM_RE` prose-parsing contract — `survey()` returns integers
      directly, so there is no stdout to regex-match.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from unittest import mock

from pathlib import Path

from coordinator_core.orient_assemble import readers_health_reaper as rhr
from coordinator_core.ops.reap_in_flight_claims import SurveyResult

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_two_integer_contract_produces_expected_directive():
    fake_result = SurveyResult(would_release=2, would_reclaim=3, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result) as survey_mock:
        result = rhr._read_reaper_dry_run()

    survey_mock.assert_called_once_with(rhr._CLAUDE_KLABAUTER_ROOT)
    assert len(result.directives) == 1
    directive = result.directives[0]
    assert directive["id"] == "d-reaper-orphaned-handoffs"
    assert directive["cli"] == "reap-orphaned-in-flight-handoffs"
    assert directive["args"] == []
    assert "2 orphaned in_flight handoff(s) would be released" in directive["detail"]
    assert "3 orphaned in_flight handoff(s) would be reclaimed as shipped" in directive["detail"]
    assert not result.judgment_points


def test_zero_directive_case_returns_empty_reader_result():
    fake_result = SurveyResult(would_release=0, would_reclaim=0, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result):
        result = rhr._read_reaper_dry_run()

    assert result.directives == []
    assert result.judgment_points == []


def test_no_subprocess_created_on_this_path():
    fake_result = SurveyResult(would_release=1, would_reclaim=0, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result):
        with mock.patch("subprocess.run") as subprocess_run_mock:
            rhr._read_reaper_dry_run()

    subprocess_run_mock.assert_not_called()


def test_reader_goes_quiet_rather_than_killing_orientation(monkeypatch):
    """`orient_assemble.__init__` runs every reader's `collect()` in a bare loop
    with no per-reader guard, so a raise here takes down the whole orientation
    assemble. survey() walks ~2000 corpus files on a box with dozens of
    concurrent handoff writers, so an OSError mid-scan is ordinary. The reader
    must go quiet, not propagate."""
    def _boom(_repo_root):
        raise OSError("handoff vanished mid-scan")

    monkeypatch.setattr(rhr, "_reap_survey", _boom)
    result = rhr._read_reaper_dry_run()
    assert result.directives == []
    assert result.judgment_points == []


def test_an_unexpected_raise_is_not_swallowed_by_the_reader(monkeypatch):
    """The reader absorbs `OSError` -- the vanishing-file race it is built for
    -- and nothing else.

    This case asserts the inverse of the one above, and replaces an earlier
    test that pinned a bare `except Exception` here. Keeping orientation
    alive across a raising reader is still required, but it is asserted
    against `orient_assemble.brief()` in
    `test_brief_survives_a_raising_reader` -- the seam that owns the loop.
    Holding it here as well is what forced the clause wide enough to hide a
    real `ModuleNotFoundError` in this very module."""
    def _boom(_repo_root):
        raise RuntimeError("survey blew up")

    monkeypatch.setattr(rhr, "_reap_survey", _boom)
    with pytest.raises(RuntimeError, match="survey blew up"):
        rhr.collect("day")


def test_probe_subcommands_are_callable_in_a_clean_interpreter():
    """The loaded probes CLI reaches `coordinator/bin/lib` on its own.

    Its subcommands `import lib`, which resolves only when `coordinator/bin`
    is on `sys.path`. Both sanctioned entry paths arrange that for
    themselves -- a directly-run script gets its own dir as `sys.path[0]`,
    and the warm door calls `_ensure_bin_dir_importable()` -- but this reader
    loads the CLI by file location, which is neither. The reader must
    therefore bootstrap it, and this runs in a SUBPROCESS because inside a
    pytest session some unrelated import has usually put the directory on
    `sys.path` already, which is exactly the ambient luck that hid the
    original defect.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import coordinator_core.orient_assemble.readers_health_reaper as r;"
            "print(r._health_probes.cmd_working_repo_registration([]))",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, proc.stderr
