
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.git.ls_files import tracked_files
from coordinator_core.git import ls_files_bytes
from coordinator_core.git.ls_files_bytes import (
    _tracked_files_bytes_cached,
    tracked_files_bytes,
)
from coordinator_core.win_portability import no_console_creationflags


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "t@example.invalid", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    (tmp_path / "alpha.py").write_bytes(b"x = 1\n")
    (tmp_path / "beta.txt").write_bytes(b"hello\n")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "gamma.py").write_bytes(b"y = 2\n")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-q", "-m", "seed", cwd=tmp_path)
    _tracked_files_bytes_cached.cache_clear()
    return tmp_path


def test_returns_bytes_entries(repo: Path) -> None:
    result = tracked_files_bytes(repo)
    assert result, "expected a non-empty tracked set"
    assert all(isinstance(entry, bytes) for entry in result)


def test_no_empty_entries_from_nul_split(repo: Path) -> None:
    assert b"" not in tracked_files_bytes(repo)


def test_matches_decoding_sibling_on_ascii_tree(repo: Path) -> None:
    decoded = tracked_files(repo)
    as_bytes = tracked_files_bytes(repo)
    assert tuple(sorted(e.decode("utf-8") for e in as_bytes)) == tuple(sorted(decoded))


def test_pathspec_scopes_the_result(repo: Path) -> None:
    only_py = tracked_files_bytes(repo, "*.py")
    assert b"alpha.py" in only_py
    assert b"beta.txt" not in only_py


def test_never_substitutes_replacement_character(repo: Path) -> None:
    assert all(b"\xef\xbf\xbd" not in entry for entry in tracked_files_bytes(repo))


def test_non_repo_folds_to_empty(tmp_path: Path) -> None:
    _tracked_files_bytes_cached.cache_clear()
    assert tracked_files_bytes(tmp_path / "not-a-repo-at-all") == ()


def test_one_spawn_per_root_pathspec_pair(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tracked_files_bytes_cached.cache_clear()
    calls: list[list[str]] = []
    real_run_git = ls_files_bytes.run_git

    def counting_run_git(args, *rest, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(args))
        return real_run_git(args, *rest, **kwargs)

    monkeypatch.setattr(ls_files_bytes, "run_git", counting_run_git)
    tracked_files_bytes(repo)
    tracked_files_bytes(repo)
    tracked_files_bytes(repo)
    assert len(calls) == 1, f"expected one spawn, saw {len(calls)}"


def test_argv_convention_matches_the_sibling_module(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tracked_files_bytes_cached.cache_clear()
    seen_argv: list[str] = []
    seen_kwargs: dict[str, object] = {}
    real_run_git = ls_files_bytes.run_git

    def capturing_run_git(args, *rest, **kwargs):  # type: ignore[no-untyped-def]
        seen_argv.extend(str(a) for a in args)
        seen_kwargs.update(kwargs)
        return real_run_git(args, *rest, **kwargs)

    monkeypatch.setattr(ls_files_bytes, "run_git", capturing_run_git)
    tracked_files_bytes(repo, "*.py")

    assert seen_argv == [
        "-C",
        str(Path(repo).resolve()),
        "ls-files",
        "-z",
        "--",
        "*.py",
    ]
    assert seen_kwargs.get("binary") is True, (
        "without binary=True this module reads GitResult.stdout, which decodes "
        "with errors='replace' and defeats the byte-exactness it exists for"
    )
    # No `timeout=`: the bound is `run.LOCAL_PLUMBING_BUDGET_SECS`, and a


def test_use_cache_false_sees_files_added_after_the_first_cached_call(
    repo: Path,
) -> None:
    _tracked_files_bytes_cached.cache_clear()
    before = tracked_files_bytes(repo)
    assert b"new.py" not in before

    (repo / "new.py").write_bytes(b"z = 3\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add new.py", cwd=repo)

    still_cached = tracked_files_bytes(repo)
    assert b"new.py" not in still_cached, (
        "sanity: the cached call must still be stale here -- otherwise this "
        "test cannot distinguish use_cache=False from an accidental cache miss"
    )

    fresh = tracked_files_bytes(repo, use_cache=False)
    assert b"new.py" in fresh


def test_use_cache_false_never_populates_or_reads_the_cache(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tracked_files_bytes_cached.cache_clear()
    calls: list[list[str]] = []
    real_run_git = ls_files_bytes.run_git

    def counting_run_git(args, *rest, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(args))
        return real_run_git(args, *rest, **kwargs)

    monkeypatch.setattr(ls_files_bytes, "run_git", counting_run_git)
    tracked_files_bytes(repo, use_cache=False)
    tracked_files_bytes(repo, use_cache=False)
    tracked_files_bytes(repo, use_cache=False)
    assert len(calls) == 3, f"expected one spawn per call, saw {len(calls)}"
    assert _tracked_files_bytes_cached.cache_info().currsize == 0


def test_tracked_files_is_unmodified_by_this_module() -> None:
    from coordinator_core.git import ls_files

    assert ls_files.__all__ == ["tracked_files"]
