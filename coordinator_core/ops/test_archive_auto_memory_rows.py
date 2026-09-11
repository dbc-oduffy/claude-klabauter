"""
Tests for coordinator_core.ops.archive_auto_memory_rows.

Spec backlink: this repo
  docs/plans/2026-08-07-archive-on-drain-memory-evicts-to-cold-tier.md
  § C5, AC1, AC2, AC5, AC7, AC8, AC11, AC12.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.archive_auto_memory_rows import main
from coordinator_core.ops.check_auto_memory_drained import _slugify_repo_root
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

# Spawns a real external process (git init / git log) to set up and verify
# fixtures; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SELF_SID = "self-session-id-0123456789"
_PEER_SID = "peer-session-id"


def _run(*extra_args: str) -> "tuple[int, str, str]":
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = main(list(extra_args))
    return exit_code, out.getvalue(), err.getvalue()


def _init_repo(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(root)], check=True, **no_console_passthrough_kwargs()
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        check=True,
        **no_console_passthrough_kwargs(),
    )
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "README.md"],
        check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "seed"],
        check=True,
        **no_console_passthrough_kwargs(),
    )


def _git_log(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "log", "--oneline", "-n", "20"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **no_console_creationflags(),
    )
    return result.stdout


def _body_text(origin_sid: "str | None", sentence: str = "body\n") -> str:
    if origin_sid is None:
        return sentence
    return (
        "---\n"
        "name: some-fact\n"
        "metadata:\n"
        f"  originSessionId: {origin_sid}\n"
        "---\n"
        f"{sentence}"
    )


def _setup_memory_dir(tmp_path: Path, repo: Path) -> "tuple[Path, Path]":
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    return home, memory_dir


def _archive_dir(repo: Path) -> Path:
    return repo / "state" / "auto-memory-archive"


def test_own_body_and_index_row_land_in_artifact_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home, memory_dir = _setup_memory_dir(tmp_path, repo)
    distinctive = "the quokka guards the archive at dawn\n"
    (memory_dir / "some-fact.md").write_text(_body_text(_SELF_SID, distinctive))
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n- [Some fact](some-fact.md) — hook\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0, err

    files = list(_archive_dir(repo).glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert distinctive.strip() in text
    assert "- [Some fact](some-fact.md) — hook" in text

    log = _git_log(repo)
    assert "archive-auto-memory-rows" in log


def test_peer_owned_body_is_not_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home, memory_dir = _setup_memory_dir(tmp_path, repo)
    (memory_dir / "peer-fact.md").write_text(_body_text(_PEER_SID, "peer secret\n"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0, err
    assert not _archive_dir(repo).exists()


def test_unresolvable_self_sid_archives_nothing_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home, memory_dir = _setup_memory_dir(tmp_path, repo)
    (memory_dir / "some-fact.md").write_text(_body_text(_SELF_SID, "hi\n"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    exit_code, out, err = _run("--root", str(repo), "--session-id", "")
    assert exit_code == 0
    assert out == ""
    assert "identity" in err.lower()
    assert not _archive_dir(repo).exists()


def test_unresolvable_root_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PATH", raising=False)
    exit_code, out, err = _run("--session-id", _SELF_SID)
    # No --root and cwd is not inside a git worktree -> unresolvable.
    assert exit_code == 1
    assert err.strip() != ""


def test_write_failure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home, memory_dir = _setup_memory_dir(tmp_path, repo)
    (memory_dir / "some-fact.md").write_text(_body_text(_SELF_SID, "hi\n"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    # Occupy the destination directory's parent with a FILE so mkdir(parents=True)
    # fails -- a deterministic, cross-platform write failure.
    state_dir = repo / "state"
    state_dir.mkdir()
    (state_dir / "auto-memory-archive").write_text("blocker", encoding="utf-8")

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 1
    assert "FAILED" in err
    assert "auto-memory-archive" in err


def test_two_home_roots_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    userprofile = tmp_path / "userprofile"
    slug = _slugify_repo_root(str(repo))
    home_memory = home / ".claude" / "projects" / slug / "memory"
    up_memory = userprofile / ".claude" / "projects" / slug / "memory"
    home_memory.mkdir(parents=True)
    up_memory.mkdir(parents=True)
    (home_memory / "home-fact.md").write_text(_body_text(_SELF_SID, "home sentence here\n"))
    (up_memory / "up-fact.md").write_text(_body_text(_SELF_SID, "userprofile sentence here\n"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0, err

    files = list(_archive_dir(repo).glob("*.md"))
    text = files[0].read_text(encoding="utf-8")
    assert "home sentence here" in text
    assert "userprofile sentence here" in text


def test_dedupe_body_referenced_by_two_index_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home, memory_dir = _setup_memory_dir(tmp_path, repo)
    sentence = "the archived quokka sentence\n"
    (memory_dir / "some-fact.md").write_text(_body_text(_SELF_SID, sentence))
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "- [Some fact](some-fact.md) — hook\n"
        "- [Some fact again](some-fact.md) — hook\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0, err

    files = list(_archive_dir(repo).glob("*.md"))
    text = files[0].read_text(encoding="utf-8")
    # Body content appears once (dedup by resolved path), both index rows survive.
    assert text.count(sentence.strip()) == 1
    assert text.count("- [Some fact](some-fact.md) — hook") == 1
    assert text.count("- [Some fact again](some-fact.md) — hook") == 1


def test_collision_appends_rather_than_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home, memory_dir = _setup_memory_dir(tmp_path, repo)
    (memory_dir / "first-fact.md").write_text(_body_text(_SELF_SID, "first drain sentence\n"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0, err

    (memory_dir / "first-fact.md").unlink()
    (memory_dir / "second-fact.md").write_text(_body_text(_SELF_SID, "second drain sentence\n"))

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0, err

    files = list(_archive_dir(repo).glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "first drain sentence" in text
    assert "second drain sentence" in text


def test_source_imports_selection_seam_and_defines_no_independent_parsing() -> None:
    """AC5: assert `archive_auto_memory_rows` imports the selection seam from
    `check_auto_memory_drained` and defines no independent `originSessionId`
    parsing or index-row regex of its own."""
    import inspect

    import coordinator_core.ops.archive_auto_memory_rows as mod

    source = inspect.getsource(mod)
    assert "from coordinator_core.ops.check_auto_memory_drained import" in source
    assert "_own_residue" in source

    # Scope the negative assertions to CODE only -- the module docstring
    # above deliberately names `originSessionId`/`MEMORY.md` regex in prose
    # to explain what is NOT re-derived; that is documentation, not a
    # second implementation. Strip everything up to (and including) the
    # module's own closing `"""` before checking.
    code_only = source.split('"""', 2)[-1]
    assert "originSessionId" not in code_only
    assert "_INDEX_ROW_RE" not in code_only
    assert "re.compile" not in code_only


def test_no_push_attempted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C3b: commit-only by construction -- `commit_paths` has no push leg.
    Assert this module never invokes `git push` and calls no push-shaped
    argument into any subprocess/commit helper."""
    import inspect

    import coordinator_core.ops.archive_auto_memory_rows as mod

    source = inspect.getsource(mod)
    code_only = source.split('"""', 2)[-1]
    assert "git push" not in source
    assert "push_mode" not in source
    assert "subprocess" not in code_only
