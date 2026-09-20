"""Regression coverage for `maximalist._scaffold_root_is_claude_home`.

state/bug-backlog/2026-08-28-step-7-scaffolds-claude-home-around-a-guard-
that-would-refuse-it.yaml: Step 7 (scaffold-canonical-structure) calls
`scaffold_canonical_structure` natively, in-process, against
`os.path.join(claude_home_dir, ".claude")` -- which is Claude Home itself
on an ordinary run. `guard_repo_setup_claude_home_refusal` denies exactly
this write for a Bash-invoked scaffold, but never runs on this native path,
so the guard's protection was unreachable by construction.

Fixture-only: no real `~/.claude`, no install run, no real settings tree --
`env` is a plain dict pointed at a `tmp_path` fixture directory.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.install.maximalist import _scaffold_root_is_claude_home


def test_scaffold_root_matching_claude_home_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    claude_home = home / ".claude"
    claude_home.mkdir()

    env = {"HOME": str(home)}

    assert _scaffold_root_is_claude_home(str(claude_home), env) is True


def test_scaffold_root_under_a_different_tree_is_not_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    doe_clone = tmp_path / "doe-clone"
    doe_clone.mkdir()

    env = {"HOME": str(home)}

    assert _scaffold_root_is_claude_home(str(doe_clone), env) is False


def test_unresolvable_claude_home_is_not_refused(tmp_path: Path) -> None:
    doe_clone = tmp_path / "doe-clone"
    doe_clone.mkdir()

    assert _scaffold_root_is_claude_home(str(doe_clone), {}) is False
