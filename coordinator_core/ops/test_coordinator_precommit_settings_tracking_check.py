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


# --- 2026-08-11: glob lens (`_TRACKED_SIDECAR_GLOBS`) coverage --------------
#
# Reproduces the five paths from the example-doctrine-repo cross-repo memo
# (2026-08-11-example-doctrine-repo-em-two-gaps-that-let-machine-local-files-stay-tracked.md
# § "1.") that were tracked in a live `~/.claude` while this guard's original
# exact-basename lens ran clean — three of them byte-identical to files the
# exact lens DOES protect, the other two plugin-manifest snapshots nested
# alongside them in the same backup directory.

_GLOB_EVIDENCE_PATHS = (
    "settings.json.bak-2026-08-07-dead-hooks",
    "_prag-fix-backup-20260720T144452Z/.claude.settings.json",
    "_prag-fix-backup-20260720T144452Z/.claude.settings.local.json",
    "_prag-fix-backup-20260720T144452Z/plugins.installed_plugins.json",
    "_prag-fix-backup-20260720T144452Z/plugins.known_marketplaces.json",
)


def test_glob_lens_catches_each_evidenced_backup_path(tmp_path):
    for rel in _GLOB_EVIDENCE_PATHS:
        meta, home = _make_meta_repo(tmp_path / rel.replace("/", "_").lstrip("."))
        target = meta / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("machine-local\n", encoding="utf-8")
        _git(["add", "-f", rel], meta)

        result = _run(meta, home)

        assert result.returncode == 1, f"{rel} should have blocked the commit"
        assert "BLOCKED" in result.stderr
        assert rel in result.stderr
        assert "[glob match]" in result.stderr


def test_glob_lens_leaves_an_unrelated_nested_json_clean(tmp_path):
    """Negative control for the glob lens: a normal JSON file at depth,
    with no settings/plugin-manifest-shaped basename, must pass clean —
    otherwise the glob lens would prove nothing by blocking everything."""
    meta, home = _make_meta_repo(tmp_path)
    nested = meta / "docs" / "notes" / "example.json"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("{}\n", encoding="utf-8")
    _git(["add", "docs/notes/example.json"], meta)

    result = _run(meta, home)

    assert result.returncode == 0


def test_exact_lens_still_reports_exact_match_tag(tmp_path):
    meta, home = _make_meta_repo(tmp_path)
    (meta / "settings.json").write_text("{}\n", encoding="utf-8")
    _git(["add", "-f", "settings.json"], meta)

    result = _run(meta, home)

    assert result.returncode == 1
    assert "[exact match]" in result.stderr
