"""Behavioral tests for coordinator/bin/coordinator-precommit-settings-tracking-check
— specifically the 2026-07-28 addition of the `.coordinator-hooks-enabled`/
`.coordinator-hooks-disabled`/`.coordinator-content-root-last-seen` machine-
local markers to `_TRACKED_SIDECAR_PATHS`.

`~/.claude` is the repo that was actually corrupted in the 2026-07-28
incident, is not onboarded via repo-setup, and this pre-commit gate chain is
the only mechanism that reaches it at all — so a new machine-local marker
that this gate doesn't know about is exactly as dangerous as the original
three it was built to catch (tracking it into git defeats the entire
per-machine-consent design those markers exist for). This file exercises
the NEW coverage only; the original three-path coverage is pre-existing
behavior this dispatch does not change.

Runs the script as a real subprocess (matches
test_coordinator_postsync_marker_resync_check.py's posture: prove it
actually executes and blocks/passes, not just that the source text mentions
the right filename).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "coordinator"
    / "bin"
    / "coordinator-precommit-settings-tracking-check"
)

_NEW_MARKERS = (
    ".coordinator-hooks-disabled",
    ".coordinator-hooks-enabled",
    ".coordinator-content-root-last-seen",
)


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_meta_repo(tmp_path: Path) -> tuple[Path, Path]:
    fake_home = tmp_path / "fakehome"
    meta = fake_home / ".claude"
    meta.mkdir(parents=True)
    _git(["init", "-q"], meta)
    _git(["config", "user.email", "test@example.com"], meta)
    _git(["config", "user.name", "Test"], meta)
    return meta, fake_home


def _run(meta_repo: Path, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(_SCRIPT)], cwd=str(meta_repo), capture_output=True, text=True, env=env
    )


def test_script_present_on_disk():
    assert _SCRIPT.is_file()


def test_staging_each_new_marker_blocks(tmp_path):
    for marker in _NEW_MARKERS:
        meta, home = _make_meta_repo(tmp_path / marker.lstrip("."))
        (meta / marker).write_text("machine-local\n", encoding="utf-8")
        _git(["add", "-f", marker], meta)

        result = _run(meta, home)

        assert result.returncode == 1, f"{marker} should have blocked the commit"
        assert "BLOCKED" in result.stderr
        assert marker in result.stderr


def test_unrelated_staged_file_is_clean(tmp_path):
    meta, home = _make_meta_repo(tmp_path)
    (meta / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], meta)

    result = _run(meta, home)

    assert result.returncode == 0


def test_override_bypasses_the_new_markers_too(tmp_path):
    meta, home = _make_meta_repo(tmp_path)
    (meta / ".coordinator-hooks-enabled").write_text("", encoding="utf-8")
    _git(["add", "-f", ".coordinator-hooks-enabled"], meta)

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["COORDINATOR_OVERRIDE_PRECOMMIT_SETTINGS_TRACKING"] = "1"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)], cwd=str(meta), capture_output=True, text=True, env=env
    )

    assert result.returncode == 0
    assert "skipped" in result.stderr
