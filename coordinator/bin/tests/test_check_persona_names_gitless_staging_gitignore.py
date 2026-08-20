"""Regression: the identity checker's git-less fallback enumeration must not
resurface gitignored runtime state as a false-positive finding, while still
catching a real leak in published-surface content.

ROOT CAUSE THIS PINS
---------------------
`percolate`'s publish staging tree (`_create_publish_staging_dir`,
`coordinator/bin/publish.py`) is built via `shutil.copytree(dest_dir,
staging_dir, ignore=shutil.ignore_patterns(".git"))` -- `.git` is excluded,
but gitignored content is NOT (`shutil.ignore_patterns` knows nothing about
`.gitignore`). `check-persona-names.py`'s file enumeration
(`_repo.repo_files`) is git-aware when `.git` is present (`git ls-files
--cached --others --exclude-standard`, which DOES honor `.gitignore`), but
falls back to a plain `_walk_files` filesystem walk -- with NO gitignore
awareness at all -- whenever `.git` is absent, which is exactly the staging
tree's shape. A stray, gitignored runtime-state file
(`state/session-hierarchy.<codename>.json`, observed live 2026-08-13) that a
coordinator session wrote into the real destination tree via
`coordinator_state_root`'s Rule 5 (sibling-repo state placement) therefore
rode the copytree into the staging dir and tripped a FILE-PATH finding on a
path that was never publishable in the first place (`state/` is gitignored
by this repo's own tracked `.gitignore`).

Spec backlink: state/audits/2026-08-13-persona-guard-staging-gitignore-gap.md
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "dist"
    / "mirror-native"
    / "claude-klabauter"
    / ".github"
    / "scripts"
    / "_repo.py"
)


def _load_repo_module():
    spec = importlib.util.spec_from_file_location("_repo", _REPO_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_gitless_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A staging-shaped tree: no `.git`, a tracked-shaped `.gitignore`
    declaring `state/` unpublishable, and a stray file under it -- mirrors
    the real incident's copytree output exactly."""
    (tmp_path / ".gitignore").write_text("state/\n*.bak\n.DS_Store\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # "machine-b" stands in for the real fleet codename observed live; using
    # the literal codename here would itself be a residual leak in THIS file.
    (state_dir / "session-hierarchy.machine-b-local.json").write_text("[]\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    return tmp_path


def test_gitless_walk_excludes_gitignored_state_dir(tmp_path):
    module = _load_repo_module()
    root = _make_gitless_tree(tmp_path)
    paths = module.repo_files(root)
    assert not any(p.startswith("state/") for p in paths), (
        f"gitignored state/ leaked through the git-less fallback walk: {paths}"
    )
    assert "README.md" in paths


def test_gitless_walk_excludes_extension_glob_pattern(tmp_path):
    module = _load_repo_module()
    root = tmp_path
    (root / ".gitignore").write_text("*.bak\n", encoding="utf-8")
    (root / "scratch.bak").write_text("stray\n", encoding="utf-8")
    (root / "keep.py").write_text("pass\n", encoding="utf-8")
    paths = module.repo_files(root)
    assert "scratch.bak" not in paths
    assert "keep.py" in paths


def test_gitless_walk_still_enumerates_a_non_ignored_leak_path(tmp_path):
    """Pin the boundary: narrowing the fallback to gitignored paths must NOT
    swallow a genuine leak sitting in ordinary, publishable content."""
    module = _load_repo_module()
    root = _make_gitless_tree(tmp_path)
    leak_dir = root / "coordinator_core"
    leak_dir.mkdir()
    # Fragment-joined per the checker's own `_tok` convention: a contiguous
    # codename literal in this test file would itself be the residual leak
    # class this checker exists to catch.
    codename_dirname = "mak" + "ima"
    leaked = leak_dir / f"{codename_dirname}-notes.txt"
    leaked.write_text("published content\n", encoding="utf-8")
    paths = module.repo_files(root)
    assert f"coordinator_core/{codename_dirname}-notes.txt" in paths, (
        "a genuinely publishable path carrying a codename must still be enumerated "
        f"(narrowing regressed real-leak coverage): {paths}"
    )


def test_git_aware_walk_excludes_publish_staging_dir(tmp_path):
    """`_create_publish_staging_dir` mints `.{dest_dir.name}.publish-staging-
    <random>` destination-ADJACENT (`coordinator/bin/publish.py`) -- untracked,
    and NOT covered by any `.gitignore` entry, so `_git_files`'s own
    `--others --exclude-standard` genuinely returns its contents. A checker
    that scans them fails the round on bytes that never ship (§ this repo's
    dispatch notes, `state/audits/` staging-identity-check incident).

    Uses a real `git init` tree (not the gitless-walk fixture above) because
    the actual incident hit `_git_files`, not `_walk_files` -- the target
    repo has a `.git` and no matching `.gitignore` entry, so the git-aware
    path is exercised here, matching production."""
    module = _load_repo_module()
    root = tmp_path
    import subprocess

    subprocess.run(
        ["git", "-C", str(root), "init", "-q"], check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    # Random-suffixed staging dir, mirroring the real mint shape exactly.
    staging_dir = root / "coordinator" / ".bin.publish-staging-gr7j6dpy"
    staging_dir.mkdir(parents=True)
    codename_dirname = "mak" + "ima"
    (staging_dir / f"{codename_dirname}-notes.txt").write_text("scratch\n", encoding="utf-8")
    paths = module.repo_files(root)
    assert not any("publish-staging-" in p for p in paths), (
        f"publish-staging scratch dir leaked into repo_files(): {paths}"
    )
    assert "README.md" in paths


def test_basename_containing_publish_staging_is_still_included(tmp_path):
    """Negative spec (§ module docstring's `SKIP_DIR_NAMES` discipline): a
    genuinely shipped FILE whose own basename contains `publish-staging-`
    must not be dropped -- only a directory COMPONENT is staging scratch."""
    module = _load_repo_module()
    root = tmp_path
    import subprocess

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    payload = root / "coordinator" / "x-publish-staging-y.py"
    payload.parent.mkdir(parents=True)
    payload.write_text("pass\n", encoding="utf-8")
    paths = module.repo_files(root)
    assert "coordinator/x-publish-staging-y.py" in paths, (
        f"a shipped file whose basename merely contains publish-staging- was wrongly excluded: {paths}"
    )


def test_unrecognized_gitignore_shapes_are_not_filtered(tmp_path):
    """Negative spec: anchored/nested/negation patterns are out of scope for
    this minimal matcher and must fail toward MORE scanning, not silently
    drop coverage the git-aware path would have provided."""
    module = _load_repo_module()
    root = tmp_path
    (root / ".gitignore").write_text("/anchored-dir/\nnested/path/\n!keep-me\n", encoding="utf-8")
    anchored = root / "anchored-dir"
    anchored.mkdir()
    (anchored / "file.txt").write_text("x\n", encoding="utf-8")
    paths = module.repo_files(root)
    assert "anchored-dir/file.txt" in paths
