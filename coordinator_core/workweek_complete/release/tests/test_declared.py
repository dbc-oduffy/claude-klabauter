"""declared_version against a bundle, a notes-only repo, a commit, and neither."""

import json
import subprocess

import pytest

from coordinator_core.win_portability import no_console_passthrough_kwargs
from coordinator_core.workweek_complete.release.declared import declared_version

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _bundle(root, version):
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "coordinator-claude", "metadata": {"version": version}})
    )
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "c", "version": version}))
    (root / "CHANGELOG.md").write_text(f"## [{version}] - 2026-01-01\n")


def _notes(root, *names):
    d = root / "archive" / "release-notes"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text("x")


def _commit(root):
    def g(*a):
        subprocess.run(["git", "-C", str(root), *a], check=True,
                       **no_console_passthrough_kwargs())
    g("init", "-q")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")


def test_bundle_reads_plugin_manifest(tmp_path):
    _bundle(tmp_path, "2.9.0")
    assert declared_version(tmp_path) == "2.9.0"


def test_notes_only_takes_highest_and_ignores_placeholder(tmp_path):
    _notes(tmp_path, "2026-01-01-v0.6.2.md", "2026-02-01-v0.6.10.md", "2026-03-01-vX.Y.Z.md")
    assert declared_version(tmp_path) == "0.6.10"


def test_treeish_reads_commit_not_worktree_bundle(tmp_path):
    _bundle(tmp_path, "1.0.0")
    _commit(tmp_path)
    _bundle(tmp_path, "9.9.9")
    assert declared_version(tmp_path) == "9.9.9"
    assert declared_version(tmp_path, treeish="HEAD") == "1.0.0"


def test_treeish_reads_commit_not_worktree_notes(tmp_path):
    _notes(tmp_path, "2026-01-01-v0.6.2.md")
    _commit(tmp_path)
    _notes(tmp_path, "2026-02-01-v0.7.0.md")
    assert declared_version(tmp_path) == "0.7.0"
    assert declared_version(tmp_path, treeish="HEAD") == "0.6.2"


def test_neither_returns_none(tmp_path):
    (tmp_path / "f").write_text("x")
    _commit(tmp_path)
    assert declared_version(tmp_path) is None
    assert declared_version(tmp_path, treeish="HEAD") is None
