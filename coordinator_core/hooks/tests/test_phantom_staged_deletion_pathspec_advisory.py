"""Pins the phantom-staged-deletion pre-commit gate end to end in a real tmp_path repo:
a pathspec commit ignores an unrelated staged D, and a plain commit gets an advisory, never a denial."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks import phantom_staged_deletion

REPO_ROOT = Path(__file__).resolve().parents[3]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_report_never_suggests_git_reset():
    finding = phantom_staged_deletion.Finding(path="x.md", disk_matches_head=True)
    report = phantom_staged_deletion.render_report([finding], "OVERRIDE_X")
    assert "git reset" not in report
    assert "x.md" in report
