"""
coordinator_core.ops.ceremony.tests.test_commit_v2_guard_class_relay

Tests for the guard-class-relay step wired into `ceremony.commit_v2` (C2 of
docs/plans/2026-08-29-a-guard-class-flip-announces-itself.md).

Coverage:
  - no-guard-changed path does NO work: assert on the ABSENCE of a git
    call/parse for the relay step, not on elapsed time (a timing assertion
    on a shared, heavily loaded box is a flake generator, and wall clock is
    not the measured axis per this repo's own doctrine).
  - guard-changed path stages exactly one transition per flipped module,
    and a non-flipping guard-path change (e.g. touching MATCHERS only)
    reports no transition.
  - a failure inside the relay step degrades to a `skips` entry and NEVER
    propagates -- `committed`/`sha` are unaffected.
  - `commit_v2` commit semantics (committed, sha, staged_preferred,
    worktree_over_staged, warnings) are unchanged by the relay step's
    presence.

EMISSION IS WIRED, and this paragraph replaces a stale "KNOWN GAP" note that
outlived the gap it described. `commit_v2._guard_class_relay_step` calls
`guard_class_relay.stage_class_transition_memo` for each detected transition,
which composes and stages a draft through the in-process `memo.draft` op --
no subprocess, no CLI, no opt-in flag. The call site in `_handler` is
unconditional after `commit_paths` lands the commit, so an ordinary commit
reaches it. `test_class_flip_stages_a_real_memo_draft_on_disk` below exercises
that path end to end against a throwaway repo.

Do not assert against any pre-existing file in the working repo's
`state/memo-outbox/`: a file's presence there proves a file exists, never that
this code staged it. Every emission assertion builds its own repo under
`tmp_path` and checks a draft that did not exist a moment earlier.

All git operations run against a throwaway repo created fresh under
`tmp_path` — never the working repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2

# Spawns real external `git` processes; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_GUARD_PATH = "coordinator_core/write_guards/some_guard.py"


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", "--", rel_path], repo)
    _git(["commit", "-q", "-m", f"seed {rel_path}"], repo)


def _write(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _head_sha(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _call(repo: Path, params: dict) -> dict:
    # Scope common_dir: the handler receives repo_root = the .git directory,
    # mirroring commit_exec_bit's own test precedent.
    return commit_v2._handler(params, repo_root=repo / ".git")


def _guard_source(cls: str) -> str:
    return f'CLASS = "{cls}"\nMATCHERS = ["Write"]\nPRIORITY = 100\n'


# ---------------------------------------------------------------------------
# no-guard-changed path does no work
# ---------------------------------------------------------------------------


def test_no_guard_path_in_pathspec_returns_empty_relay_with_no_git_read(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "hello\n")

    _write(repo, "README.md", "hello again\n")

    calls = {"read_tree_spine": 0}
    real_read_tree_spine = commit_v2.read_tree_spine

    def _spy(*args, **kwargs):
        calls["read_tree_spine"] += 1
        return real_read_tree_spine(*args, **kwargs)

    monkeypatch.setattr(commit_v2, "read_tree_spine", _spy)

    result = _call(repo, {"paths": ["README.md"], "message": "touch readme"})

    assert result["committed"] is True
    assert result["guard_class_relay"] == {"transitions": [], "skips": []}
    # No path under _GUARD_MODULE_DIR -- the pre-commit spine read for the
    # relay step must never fire (C2 negative spec / brightline budget).
    assert calls["read_tree_spine"] == 0


def test_guard_module_paths_filters_non_write_guards_paths():
    guard_paths = commit_v2._guard_module_paths(
        ["README.md", _GUARD_PATH, "coordinator_core/write_guards/not_python.txt"],
        [],
    )
    assert guard_paths == [_GUARD_PATH]


# ---------------------------------------------------------------------------
# guard-changed path stages exactly one transition per flipped module
# ---------------------------------------------------------------------------


def test_class_flip_in_guard_module_is_detected_as_a_transition(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, _GUARD_PATH, _guard_source("hard-deny"))

    _write(repo, _GUARD_PATH, _guard_source("advisory"))

    result = _call(repo, {"paths": [_GUARD_PATH], "message": "flip class"})

    assert result["committed"] is True
    relay = result["guard_class_relay"]
    assert relay["skips"] == []
    assert len(relay["transitions"]) == 1
    entry = relay["transitions"][0]
    assert entry["module"] == _GUARD_PATH
    assert entry["old_class"] == "hard-deny"
    assert entry["new_class"] == "advisory"
    assert entry["sha"] == result["sha"] == _head_sha(repo)


def test_non_class_guard_module_edit_reports_no_transition(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, _GUARD_PATH, _guard_source("hard-deny"))

    # Change MATCHERS only -- CLASS is unchanged, so no transition.
    new_source = 'CLASS = "hard-deny"\nMATCHERS = ["Write", "Edit"]\nPRIORITY = 100\n'
    _write(repo, _GUARD_PATH, new_source)

    result = _call(repo, {"paths": [_GUARD_PATH], "message": "widen matchers"})

    assert result["committed"] is True
    assert result["guard_class_relay"] == {"transitions": [], "skips": []}


def test_deleted_guard_module_reports_no_transition(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, _GUARD_PATH, _guard_source("hard-deny"))
    (repo / _GUARD_PATH).unlink()

    result = _call(
        repo, {"paths": [], "deleted_paths": [_GUARD_PATH], "message": "delete guard"}
    )

    assert result["committed"] is True
    # Wholesale delete -- new_source is None, never a transition.
    assert result["guard_class_relay"] == {"transitions": [], "skips": []}


# ---------------------------------------------------------------------------
# a failure inside the relay step degrades to a skips entry, never raises,
# and never touches committed/sha
# ---------------------------------------------------------------------------


def test_relay_step_failure_degrades_to_skip_and_never_fails_the_commit(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_file(repo, _GUARD_PATH, _guard_source("hard-deny"))
    _write(repo, _GUARD_PATH, _guard_source("advisory"))

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated relay failure")

    monkeypatch.setattr(commit_v2, "detect_class_transition", _boom)

    result = _call(repo, {"paths": [_GUARD_PATH], "message": "flip class"})

    # The commit itself must be unaffected by the relay step blowing up.
    assert result["committed"] is True
    assert result["sha"] == _head_sha(repo)
    relay = result["guard_class_relay"]
    assert relay["transitions"] == []
    assert len(relay["skips"]) == 1
    assert "simulated relay failure" in relay["skips"][0]


# ---------------------------------------------------------------------------
# commit_v2 commit semantics are unchanged by the relay step's presence
# ---------------------------------------------------------------------------


def test_class_flip_stages_a_real_memo_draft_on_disk(tmp_path):
    """A real commit's guard-class-relay step stages an actual
    state/memo-outbox/ draft in the throwaway repo -- not merely a
    detected transition. Proves the C3 emission wiring runs end-to-end
    (memo.draft + memo.compose, in-process), never a subprocess and never
    a sibling repo's tree.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, _GUARD_PATH, _guard_source("hard-deny"))
    _write(repo, _GUARD_PATH, _guard_source("advisory"))

    result = _call(repo, {"paths": [_GUARD_PATH], "message": "flip class"})

    relay = result["guard_class_relay"]
    assert relay["skips"] == []
    entry = relay["transitions"][0]
    assert entry["memo_staged"] is True
    topic = entry["memo_topic"]
    assert topic is not None

    draft_path = repo / "state" / "memo-outbox" / f"{topic}.md"
    assert draft_path.is_file()
    content = draft_path.read_text(encoding="utf-8")
    assert "to: \"doe-claude-em\"" in content or "to: doe-claude-em" in content
    from coordinator_core.write_guards.guard_class_relay import UNCOVERED_SHAPE
    assert UNCOVERED_SHAPE in content
    assert _GUARD_PATH in content
    assert entry["sha"] in content


def test_commit_semantics_unchanged_by_relay_step(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, _GUARD_PATH, _guard_source("hard-deny"))
    _write(repo, _GUARD_PATH, _guard_source("advisory"))

    result = _call(repo, {"paths": [_GUARD_PATH], "message": "flip class"})

    assert set(result.keys()) == {
        "committed",
        "sha",
        "staged_preferred",
        "worktree_over_staged",
        "no_delta",
        "warnings",
        "guard_class_relay",
    }
    assert result["committed"] is True
    assert result["sha"] == _head_sha(repo)
    assert result["staged_preferred"] == []
    assert result["worktree_over_staged"] == []
    assert result["warnings"] == []
