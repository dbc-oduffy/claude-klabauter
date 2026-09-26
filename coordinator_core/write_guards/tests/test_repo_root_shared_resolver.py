
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import repo_root as _git_repo_root
from coordinator_core.write_guards import _repo_root
from coordinator_core.write_guards import block_consumed_handoff_edit as _handoff_guard
from coordinator_core.write_guards import (
    block_cutover_phase_hand_edit as _cutover_guard,
)
from coordinator_core.write_guards import (
    block_memo_status_hand_edit as _memo_status_guard,
)
from coordinator_core.write_guards import (
    block_subagent_archive_write as _archive_guard,
)
from coordinator_core.write_guards import (
    block_subagent_plan_body_write as _plan_body_guard,
)
from coordinator_core.write_guards import (
    bump_out_of_repo_tool_write as _bump_guard,
)
from coordinator_core.write_guards import guard_doctrine_surface_edits as _doctrine_guard
from coordinator_core.write_guards import (
    validate_frontmatter_schema_advisory as _advisory_guard,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


@pytest.fixture
def scratch_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, **_NO_CONSOLE)
    return repo


@pytest.fixture(autouse=True)
def _clear_repo_root_memo():
    _git_repo_root.clear_memo()
    yield
    _git_repo_root.clear_memo()


class TestSharedResolver:
    def test_resolves_repo_root_for_a_real_repo(self, scratch_repo):
        resolved = _repo_root.resolve_repo_root(str(scratch_repo))
        assert resolved is not None
        import os

        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo))

    def test_returns_none_outside_any_repo(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert _repo_root.resolve_repo_root(str(outside)) is None

    def test_ordinary_repo_walk_never_spawns(self, scratch_repo, monkeypatch):
        spawn_calls = []
        real_spawn = _git_repo_root._spawn_rev_parse

        def _counting_spawn(args, cwd):
            spawn_calls.append((tuple(args), cwd))
            return real_spawn(args, cwd)

        monkeypatch.setattr(_git_repo_root, "_spawn_rev_parse", _counting_spawn)

        first = _repo_root.resolve_repo_root(str(scratch_repo))
        second = _repo_root.resolve_repo_root(str(scratch_repo))
        third = _repo_root.resolve_repo_root(str(scratch_repo))

        assert first == second == third
        assert spawn_calls == []

    def test_walk_failure_resolves_to_none_without_ever_spawning(
        self, scratch_repo, monkeypatch
    ):
        monkeypatch.setattr(_git_repo_root, "_walk_for_repo", lambda start: None)

        spawn_calls = []
        real_spawn = _git_repo_root._spawn_rev_parse

        def _counting_spawn(args, cwd):
            spawn_calls.append((tuple(args), cwd))
            return real_spawn(args, cwd)

        monkeypatch.setattr(_git_repo_root, "_spawn_rev_parse", _counting_spawn)

        first = _repo_root.resolve_repo_root(str(scratch_repo))
        second = _repo_root.resolve_repo_root(str(scratch_repo))
        third = _repo_root.resolve_repo_root(str(scratch_repo))

        assert first == second == third is None
        assert spawn_calls == []


class TestMigratedGuardVerdictsUnchanged:
    def test_doctrine_guard_git_root_resolves_inside_scratch_repo(self, scratch_repo, monkeypatch):
        monkeypatch.chdir(scratch_repo)
        root = _doctrine_guard._git_root()
        assert root is not None

    def test_doctrine_guard_git_root_none_outside_repo(self, tmp_path, monkeypatch):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        monkeypatch.chdir(outside)
        assert _doctrine_guard._git_root() is None

    def test_doctrine_guard_allows_unprotected_file(self, scratch_repo):
        payload = {
            "tool_name": "Write",
            "cwd": str(scratch_repo),
            "tool_input": {
                "file_path": str(scratch_repo / "some-ordinary-file.md"),
                "content": "hello",
            },
        }
        assert _doctrine_guard.check(payload) is None

    def test_advisory_resolve_repo_root_returns_repo_root_for_real_repo(self, scratch_repo):
        resolved = _advisory_guard.resolve_repo_root(str(scratch_repo))
        import os

        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo))

    def test_advisory_resolve_repo_root_falls_back_to_cwd_outside_repo(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert _advisory_guard.resolve_repo_root(str(outside)) == str(outside)


class TestC3bMigratedGuardVerdictsUnchanged:

    @pytest.mark.parametrize(
        "guard_module",
        [
            _handoff_guard,
            _cutover_guard,
            _memo_status_guard,
            _archive_guard,
            _plan_body_guard,
            _bump_guard,
        ],
        ids=[
            "block_consumed_handoff_edit",
            "block_cutover_phase_hand_edit",
            "block_memo_status_hand_edit",
            "block_subagent_archive_write",
            "block_subagent_plan_body_write",
            "bump_out_of_repo_tool_write",
        ],
    )
    def test_resolve_git_root_resolves_inside_scratch_repo(self, guard_module, scratch_repo):
        import os

        resolved = guard_module._resolve_git_root(str(scratch_repo))
        assert resolved is not None
        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo))

    @pytest.mark.parametrize(
        "guard_module",
        [
            _handoff_guard,
            _cutover_guard,
            _memo_status_guard,
            _archive_guard,
            _plan_body_guard,
            _bump_guard,
        ],
        ids=[
            "block_consumed_handoff_edit",
            "block_cutover_phase_hand_edit",
            "block_memo_status_hand_edit",
            "block_subagent_archive_write",
            "block_subagent_plan_body_write",
            "bump_out_of_repo_tool_write",
        ],
    )
    def test_resolve_git_root_none_outside_any_repo(self, guard_module, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert guard_module._resolve_git_root(str(outside)) is None

    def test_memo_status_guard_git_common_dir_still_resolves_via_no_console_creationflags(
        self, scratch_repo
    ):
        import os

        resolved = _memo_status_guard._resolve_git_common_dir(str(scratch_repo))
        assert resolved is not None
        assert os.path.realpath(resolved) == os.path.realpath(
            str(scratch_repo / ".git")
        )

    def test_plan_body_guard_git_dir_still_resolves_via_no_console_creationflags(
        self, scratch_repo
    ):
        import os

        resolved = _plan_body_guard._resolve_git_dir(str(scratch_repo))
        assert resolved is not None
        assert os.path.basename(os.path.normpath(resolved)) == ".git"

    def test_handoff_guard_allows_unprotected_write_outside_any_handoff(self, scratch_repo):
        payload = {
            "tool_name": "Write",
            "cwd": str(scratch_repo),
            "tool_input": {
                "file_path": str(scratch_repo / "some-ordinary-file.md"),
                "content": "hello",
            },
        }
        assert _handoff_guard.check(payload) is None

    def test_archive_guard_allows_top_level_em_write(self, scratch_repo):
        payload = {
            "tool_name": "Write",
            "cwd": str(scratch_repo),
            "tool_input": {
                "file_path": str(scratch_repo / "archive" / "x.md"),
                "content": "hello",
            },
        }
        assert _archive_guard.check(payload) is None


class TestD4MigratedGitDirResolversUnchanged:

    def test_memo_status_common_dir_resolves_absolute_inside_repo(self, scratch_repo):
        import os

        resolved = _memo_status_guard._resolve_git_common_dir(str(scratch_repo))
        assert resolved is not None
        assert os.path.isabs(resolved)
        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo / ".git"))

    def test_memo_status_common_dir_none_outside_any_repo(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert _memo_status_guard._resolve_git_common_dir(str(outside)) is None

    def test_plan_body_git_dir_resolves_absolute_inside_repo(self, scratch_repo):
        import os

        resolved = _plan_body_guard._resolve_git_dir(str(scratch_repo))
        assert resolved is not None
        assert os.path.isabs(resolved)
        assert os.path.realpath(resolved) == os.path.realpath(str(scratch_repo / ".git"))

    def test_plan_body_git_dir_none_outside_any_repo(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        assert _plan_body_guard._resolve_git_dir(str(outside)) is None
