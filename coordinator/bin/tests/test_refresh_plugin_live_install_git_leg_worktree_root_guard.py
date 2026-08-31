"""test_refresh_plugin_live_install_git_leg_worktree_root_guard — pytest coverage
for the git-managed leg's work-tree-root refusal in refresh-plugin-live-install.py.

Spec backlink: cross-repo/archive/2026-08-18-doe-claude-em-refresh-git-leg-can-
detach-an-unrelated-repo.md. The git-managed leg (`_handle_default`) trusted a
registry-supplied `live_path` without verifying it is the ROOT of its own
work-tree. A stale registry row pointing `live_path` at a directory nested
INSIDE an unrelated repo (e.g. `~/.claude/plugins/<name>`, itself just a plain
directory inside the operator's `~/.claude` git checkout) satisfies the
pre-existing containment guard (`_resolve_contained_live_path`, which only
checks "under the managed plugins dir") while still routing this leg's
`git fetch`/`git checkout` at the ENCLOSING repo -- which is exactly what
detached the operator's `~/.claude` off its working branch.

Negative-spec: a `live_path` that IS its own work-tree root must NOT be
refused by this guard (distinguishes "refuses nesting" from "refuses git
entirely").
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    """Load refresh-plugin-live-install.py by file path (hyphenated name bypass)."""
    spec = importlib.util.spec_from_file_location(
        "refresh_plugin_live_install",
        _BIN_DIR / "refresh-plugin-live-install.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(args, cwd):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        **_mod._no_console_kwargs(),
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "t@example.com"], path)
    _git(["config", "user.name", "t"], path)
    (path / "seed.txt").write_text("seed")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "seed"], path)


def test_git_worktree_root_matches_own_root(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    assert _mod._git_worktree_root(repo) == repo.resolve(strict=True)


def test_git_worktree_root_of_nested_dir_is_enclosing_root(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    nested = repo / "plugins" / "my-plugin"
    nested.mkdir(parents=True)

    assert _mod._git_worktree_root(nested) == repo.resolve(strict=True)


def test_git_leg_refuses_when_live_path_is_nested_inside_unrelated_repo(tmp_path, monkeypatch):
    """The regression case: live_path is a plain directory nested inside a
    LARGER, unrelated repo (mirrors ~/.claude containing plugins/<name>).
    The git-managed leg must refuse before any fetch/checkout, and the
    enclosing repo's branch must stay untouched.

    The real `check-plugin-drift.py` drift probe is bypassed (forced
    `.exists() -> False`) so the leg falls through to the plain
    `_check_clean_tree` path instead of spawning a subprocess that would
    refuse for an UNRELATED reason ("my-plugin" not registered) and mask
    whether this guard specifically is what refuses.
    """
    real_exists = Path.exists

    def _fake_exists(self):
        if self.name == "check-plugin-drift.py":
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _fake_exists)

    enclosing_repo = tmp_path / "claude-home"
    _init_repo(enclosing_repo)
    _git(["checkout", "-b", "machine-a"], enclosing_repo)

    # A real origin with a divergent main, so an UNGUARDED leg's
    # fetch+checkout would actually succeed and move HEAD -- proves the
    # assertions below exercise the guard, not an incidental "no remote"
    # failure.
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], tmp_path)
    _git(["remote", "add", "origin", str(origin)], enclosing_repo)
    _git(["push", "origin", "machine-a:main"], enclosing_repo)
    (enclosing_repo / "seed.txt").write_text("advanced")
    _git(["commit", "-am", "advance machine-a past main"], enclosing_repo)

    before_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], enclosing_repo).stdout.strip()
    before_sha = _git(["rev-parse", "HEAD"], enclosing_repo).stdout.strip()
    assert before_branch == "machine-a"

    plugins_dir = enclosing_repo / "plugins"
    live_path = plugins_dir / "my-plugin"
    live_path.mkdir(parents=True)
    source_path = tmp_path / "source-checkout"
    source_path.mkdir()

    rc = _mod._handle_default(
        "my-plugin",
        str(source_path),
        str(live_path),
        "",
        "origin/main",
        "my-dist",
        False,
        False,
        plugins_dir,
        tmp_path / "snapshots",
        tmp_path / "refresh-log",
    )

    assert rc == 1
    after_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], enclosing_repo).stdout.strip()
    after_sha = _git(["rev-parse", "HEAD"], enclosing_repo).stdout.strip()
    assert after_branch == before_branch
    assert after_sha == before_sha


def test_git_leg_worktree_root_guard_allows_live_path_that_is_its_own_root(tmp_path, monkeypatch):
    """Negative-spec companion: a live_path that IS a git work-tree root of
    its own must clear this specific guard (may still fail later for other
    reasons, e.g. no origin remote -- this only proves the new refusal does
    not fire)."""
    real_exists = Path.exists

    def _fake_exists(self):
        if self.name == "check-plugin-drift.py":
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _fake_exists)

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    live_path = plugins_dir / "my-plugin"
    _init_repo(live_path)
    source_path = tmp_path / "source-checkout"
    source_path.mkdir()

    rc = _mod._handle_default(
        "my-plugin",
        str(source_path),
        str(live_path),
        "",
        "origin/main",
        "my-dist",
        False,
        False,
        plugins_dir,
        tmp_path / "snapshots",
        tmp_path / "refresh-log",
    )

    # Fails later (no origin remote configured), NOT on the work-tree-root
    # guard -- proves the guard is scoped to nesting, not to git generally.
    assert rc == 1
    result = _mod._git_worktree_root(live_path)
    assert result == live_path.resolve(strict=True)
