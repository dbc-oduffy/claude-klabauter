"""`lint-frontmatter --file` must survive an off-mount target on Windows.

Regression: `_run_single_file_check` computed `os.path.relpath(resolved,
repo_root)` unguarded. On Windows that raises `ValueError: path is on mount
'C:', start on mount 'X:'` whenever the linted file and the repo live on
different drives — the routine case for a repo on X: and a scratch file under
the default TEMP on C:, which surfaced as a bare traceback rather than a
diagnostic.

Windows is first-class here (project CLAUDE.md § Runtime conventions), so this
is break-class rather than cosmetic. The test is skipped off-Windows, where
`relpath` spans mounts without raising and there is nothing to regress.
"""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.frontmatter import schema_validate

_HANDOFF_FM = """---
title: "Cross mount probe"
created: 2026-08-11
branch: "work/probe"
status: open
predecessor: none
kind: session-handoff
handoff_phase: continuation
deployment_state: ready_to_fire
category: infra
summary: "Off-mount lint probe"
pickup_ready: true
deliverable_id: null
initiative: null
---

# Cross mount probe

Body.
"""


def _other_mount_dir(tmp_path) -> str | None:
    """A directory on a DIFFERENT drive than `tmp_path`, or None if unavailable."""
    here_drive = os.path.splitdrive(str(tmp_path))[0].upper()
    for candidate in (os.path.dirname(os.path.abspath(__file__)), os.getcwd(), sys.prefix):
        drive = os.path.splitdrive(os.path.abspath(candidate))[0].upper()
        if drive and drive != here_drive:
            return os.path.abspath(candidate)
    return None


def test_single_file_check_relpath_valueerror_reports_instead_of_raising(tmp_path, capsys, monkeypatch):
    """Host-independent coverage of the `except ValueError` branch itself.

    The two `skipif(os.name != "nt")` tests below only run on a genuinely
    two-drive Windows box, so on a single-drive CI runner they are silently
    skipped and the branch goes unexercised anywhere. This test forces the
    same `os.path.relpath` raise via monkeypatch so the guard's behavior is
    pinned on any host, independent of real disk topology. Review: reviewer
    flagged (P2) that the guard's raising-relpath path had no host-independent
    coverage — the two-drive tests remain as the additional real-topology check.
    """
    target = tmp_path / "cross-mount-handoff.md"
    target.write_text(_HANDOFF_FM, encoding="utf-8")

    def _raising_relpath(path, start):
        raise ValueError("path is on mount 'C:', start on mount 'X:'")

    monkeypatch.setattr(schema_validate.os.path, "relpath", _raising_relpath)

    rc = schema_validate._run_single_file_check(str(tmp_path), str(target), False)

    assert rc == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "\\" not in out.split(":", 1)[-1].split(" valid")[0], out
    assert "valid" in out, out


def test_relpath_valueerror_missing_file_still_reports_not_found(tmp_path, capsys, monkeypatch):
    """Host-independent coverage of the not-found guard under the same raise."""

    def _raising_relpath(path, start):
        raise ValueError("path is on mount 'C:', start on mount 'X:'")

    monkeypatch.setattr(schema_validate.os.path, "relpath", _raising_relpath)

    rc = schema_validate._run_single_file_check(
        str(tmp_path), str(tmp_path / "does-not-exist.md"), False
    )

    assert rc == 2
    assert "file not found" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="relpath only raises across mounts on Windows")
def test_single_file_check_on_a_different_mount_reports_instead_of_raising(tmp_path, capsys):
    repo_root = _other_mount_dir(tmp_path)
    if repo_root is None:
        pytest.skip("no second drive available on this box to build a cross-mount pair")

    target = tmp_path / "cross-mount-handoff.md"
    target.write_text(_HANDOFF_FM, encoding="utf-8")

    # Pre-fix this raised ValueError out of the CLI entrypoint.
    rc = schema_validate._run_single_file_check(repo_root, str(target), False)

    assert rc == 0, capsys.readouterr()
    out = capsys.readouterr().out
    # The off-mount path is rendered forward-slashed, and the record still
    # resolves its schema via `kind` even though path-keyed matching cannot.
    assert "\\" not in out.split(":", 1)[-1].split(" valid")[0], out
    assert "valid" in out, out


@pytest.mark.skipif(os.name != "nt", reason="relpath only raises across mounts on Windows")
def test_cross_mount_missing_file_still_reports_not_found(tmp_path, capsys):
    """The guard must not mask the ordinary not-found diagnostic (exit 2)."""
    repo_root = _other_mount_dir(tmp_path)
    if repo_root is None:
        pytest.skip("no second drive available on this box to build a cross-mount pair")

    rc = schema_validate._run_single_file_check(
        repo_root, str(tmp_path / "does-not-exist.md"), False
    )

    assert rc == 2
    assert "file not found" in capsys.readouterr().err
