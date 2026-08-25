"""
test_read_local_md_mapping.py — coverage for the C1 chunk of
docs/plans/2026-08-15-app-session-launch-census-teardown-ops.md:

  - cs_read_local_md_mapping (coordinator_core/resolve_validation_cmd.py)
  - git_root_zero_spawn (coordinator_core/ops/_git_root_util.py)

AC1: mapping reader returns a nested dict, never raises, empty mapping when
     the key is absent, and cs_read_local_md_key's existing behaviour stays
     byte-identical.
AC2: root resolver is zero-spawn, honours CLAUDE_PROJECT_DIR first when set
     and a real directory, and resolves a `.git` FILE (worktree) as well as
     a `.git` directory. No subprocess is spawned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.resolve_validation_cmd import (
    cs_read_local_md_key,
    cs_read_local_md_mapping,
)
from coordinator_core.ops._git_root_util import git_root_zero_spawn


# --- AC1: cs_read_local_md_mapping --------------------------------------------


def test_mapping_basic_nested_block(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\n"
        "app_session:\n"
        "  desktop:\n"
        "    runtime: electron\n"
        "    command: pnpm dev\n"
        "  web:\n"
        "    runtime: server\n"
        "    port: 5173\n"
        "---\n"
    )
    result = cs_read_local_md_mapping(str(tmp_path), "app_session")
    assert result == {
        "desktop": {"runtime": "electron", "command": "pnpm dev"},
        "web": {"runtime": "server", "port": "5173"},
    }


def test_mapping_missing_file_returns_empty_dict(tmp_path):
    assert cs_read_local_md_mapping(str(tmp_path), "app_session") == {}


def test_mapping_missing_key_returns_empty_dict(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfast_test_cmd: pytest\n---\n"
    )
    assert cs_read_local_md_mapping(str(tmp_path), "app_session") == {}


def test_mapping_key_present_but_no_indented_block_returns_empty_dict(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\napp_session:\nfast_test_cmd: pytest\n---\n"
    )
    assert cs_read_local_md_mapping(str(tmp_path), "app_session") == {}


def test_mapping_never_raises_on_malformed_block(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\n"
        "app_session:\n"
        "  desktop\n"  # no colon at all — malformed leaf
        "    runtime: electron\n"
        "---\n"
    )
    # Must not raise; result shape is best-effort.
    result = cs_read_local_md_mapping(str(tmp_path), "app_session")
    assert isinstance(result, dict)


def test_mapping_strips_wrapping_quotes_on_leaf_values(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\n"
        "app_session:\n"
        "  desktop:\n"
        '    command: "pnpm dev --flag \'x\'"\n'
        "---\n"
    )
    result = cs_read_local_md_mapping(str(tmp_path), "app_session")
    assert result["desktop"]["command"] == "pnpm dev --flag 'x'"


def test_mapping_does_not_match_key_appearing_indented(tmp_path):
    # An indented line containing "app_session:" must not be mistaken for
    # the top-level key — only a column-0 match anchors the block.
    (tmp_path / "coordinator.local.md").write_text(
        "---\n"
        "other:\n"
        "  app_session: not-the-real-one\n"
        "---\n"
    )
    assert cs_read_local_md_mapping(str(tmp_path), "app_session") == {}


def test_read_local_md_key_byte_identical_after_mapping_reader_added(tmp_path):
    """AC1: cs_read_local_md_key's existing behaviour is untouched by the
    new sibling — same fixture, same three assertions its own suite makes
    in coordinator_core/test_resolve_validation_cmd.py."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfull_test_cmd: pytest -q\n---\n"
    )
    assert cs_read_local_md_key(str(tmp_path), "full_test_cmd") == "pytest -q"
    assert cs_read_local_md_key(str(tmp_path), "missing_key") == ""


def test_mapping_and_flat_key_coexist_in_same_file(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\n"
        "fast_test_cmd: pytest -q\n"
        "app_session:\n"
        "  desktop:\n"
        "    runtime: electron\n"
        "---\n"
    )
    assert cs_read_local_md_key(str(tmp_path), "fast_test_cmd") == "pytest -q"
    assert cs_read_local_md_mapping(str(tmp_path), "app_session") == {
        "desktop": {"runtime": "electron"}
    }


# --- AC2: git_root_zero_spawn --------------------------------------------------


def test_root_resolver_finds_git_directory(tmp_path):
    repo = tmp_path / "consuming-repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert git_root_zero_spawn(nested) == str(repo)


def test_root_resolver_finds_git_file_in_worktree(tmp_path):
    """A git worktree's `.git` is a FILE holding a `gitdir:` pointer, not a
    directory. A directory-only test silently fails here — this is the
    exact anchoring defect the plan names (Hard constraint 6/7)."""
    repo = tmp_path / "worktree-repo"
    repo.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/x\n")
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert git_root_zero_spawn(nested) == str(repo)


def test_root_resolver_returns_none_when_no_git_entry_found(tmp_path):
    lone = tmp_path / "no-repo-here"
    lone.mkdir()
    assert git_root_zero_spawn(lone) is None


def test_root_resolver_honours_claude_project_dir_first(tmp_path, monkeypatch):
    real_repo = tmp_path / "real-repo"
    (real_repo / ".git").mkdir(parents=True)
    decoy = tmp_path / "decoy-repo"
    (decoy / ".git").mkdir(parents=True)

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(real_repo))
    assert git_root_zero_spawn(decoy) == str(real_repo)


def test_root_resolver_ignores_claude_project_dir_if_not_a_real_directory(
    tmp_path, monkeypatch
):
    repo = tmp_path / "consuming-repo"
    (repo / ".git").mkdir(parents=True)

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "does-not-exist"))
    assert git_root_zero_spawn(repo) == str(repo)


def test_root_resolver_spawns_no_subprocess(tmp_path, monkeypatch):
    repo = tmp_path / "consuming-repo"
    (repo / ".git").mkdir(parents=True)

    calls = []
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("git_root_zero_spawn must not spawn a subprocess")
        )
    )
    result = git_root_zero_spawn(repo)
    assert result == str(repo)
    assert calls == []


def test_root_resolver_fixture_repo_root_is_unrelated_to_code_location(tmp_path):
    """Anti-scope: a fixture whose repo root and code root coincide proves
    nothing about anchoring. This repo (claude-klabauter, the code's own
    tree) is NOT tmp_path — the consuming tree here lives at a path with no
    relationship to coordinator_core's own location on disk."""
    import coordinator_core

    code_root = Path(coordinator_core.__file__).resolve().parent
    consuming_repo = tmp_path / "unrelated-consuming-repo"
    (consuming_repo / ".git").mkdir(parents=True)

    assert str(consuming_repo) != str(code_root)
    assert git_root_zero_spawn(consuming_repo) == str(consuming_repo)
