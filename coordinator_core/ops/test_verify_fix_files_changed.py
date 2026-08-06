"""
Tests for coordinator_core.ops.verify_fix_files_changed — settlement B9
(bug_sweep.verify_fix_files_changed).

Covers the pure set-difference contract over a real (tmp_path throwaway) git
repo, the structured-error premises (missing / malformed / shape-invalid
manifest, not-a-repo git failure), and the CC-4 double-invocation proof.
Git is exercised ONLY inside tmp_path throwaway repos — never against the
working repo.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops import verify_fix_files_changed as mod


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )  # popup-safe-env-suppressed


@pytest.fixture
def repo(tmp_path):
    """Throwaway git repo with two committed files; a.py then gets an
    uncommitted working-tree edit, b.py stays clean."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@test.invalid")
    _git(root, "config", "user.name", "test")
    (root / "a.py").write_text("original a\n", encoding="utf-8")
    (root / "b.py").write_text("original b\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "seed")
    (root / "a.py").write_text("fixed a\n", encoding="utf-8")
    return root


def _manifest(tmp_path, entries):
    path = tmp_path / "phase2-fix-now.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_claimed_no_diff_is_the_zero_diff_cohort(repo, tmp_path):
    manifest = _manifest(
        tmp_path, [{"file": "a.py"}, {"file": "b.py"}, {"file": "b.py"}]
    )
    result = mod.verify_fix_files_changed(str(manifest), repo_root=repo)
    # a.py has a working-tree diff; b.py was claimed fixed but is untouched.
    assert result == {"claimed_no_diff": ["b.py"]}


def test_all_claims_backed_by_diffs_returns_empty(repo, tmp_path):
    manifest = _manifest(tmp_path, [{"file": "a.py"}])
    result = mod.verify_fix_files_changed(str(manifest), repo_root=repo)
    assert result == {"claimed_no_diff": []}


def test_empty_manifest_returns_empty(repo, tmp_path):
    manifest = _manifest(tmp_path, [])
    result = mod.verify_fix_files_changed(str(manifest), repo_root=repo)
    assert result == {"claimed_no_diff": []}


def test_double_invocation_identical_results(repo, tmp_path):
    """CC-4: pure read — two back-to-back calls with identical inputs against
    an unchanged working tree return identical results."""
    manifest = _manifest(tmp_path, [{"file": "a.py"}, {"file": "b.py"}])
    first = mod.verify_fix_files_changed(str(manifest), repo_root=repo)
    second = mod.verify_fix_files_changed(str(manifest), repo_root=repo)
    assert first == second == {"claimed_no_diff": ["b.py"]}


def test_missing_manifest_raises_structured_error(repo, tmp_path):
    with pytest.raises(mod.FixManifestError, match="cannot read"):
        mod.verify_fix_files_changed(str(tmp_path / "absent.json"), repo_root=repo)


def test_malformed_json_raises_structured_error(repo, tmp_path):
    path = tmp_path / "phase2-fix-now.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(mod.FixManifestError, match="not valid JSON"):
        mod.verify_fix_files_changed(str(path), repo_root=repo)


def test_non_array_manifest_raises_structured_error(repo, tmp_path):
    path = tmp_path / "phase2-fix-now.json"
    path.write_text('{"file": "a.py"}', encoding="utf-8")
    with pytest.raises(mod.FixManifestError, match="JSON array"):
        mod.verify_fix_files_changed(str(path), repo_root=repo)


def test_entry_without_string_file_key_raises_structured_error(repo, tmp_path):
    manifest = _manifest(tmp_path, [{"file": "a.py"}, {"path": "b.py"}])
    with pytest.raises(mod.FixManifestError, match="entry 1"):
        mod.verify_fix_files_changed(str(manifest), repo_root=repo)


def test_not_a_git_repo_raises_structured_error(tmp_path):
    manifest = _manifest(tmp_path, [{"file": "a.py"}])
    bare_dir = tmp_path / "not-a-repo"
    bare_dir.mkdir()
    with pytest.raises(mod.FixManifestError, match="git diff"):
        mod.verify_fix_files_changed(str(manifest), repo_root=bare_dir)


def test_op_registered_and_handler_contract(repo, tmp_path):
    handler = get_op_handler("bug_sweep.verify_fix_files_changed")
    assert handler is not None
    with pytest.raises(mod.FixManifestError, match="phase2_fix_now_path"):
        handler({}, None)
    manifest = _manifest(tmp_path, [{"file": "b.py"}])
    result = handler({"phase2_fix_now_path": str(manifest)}, repo)
    assert result == {"claimed_no_diff": ["b.py"]}
