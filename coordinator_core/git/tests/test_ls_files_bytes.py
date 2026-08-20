"""Tests for `coordinator_core.git.ls_files_bytes :: tracked_files_bytes`.

The point of the module under test is byte-exactness, so these assert on the
properties a decoding enumerator would silently lose: a `bytes` return, parity
with the decoding sibling on an all-ASCII tree, and no U+FFFD substitution.
The argv/spawn-shape assertions exist because the module's whole reason to be a
sibling rather than an edit is that it must keep `ls_files.py`'s one-spawn-per-
(root, pathspec) contract -- AC3 bounds the census at three spawns per repo, and
a regression here is invisible until that budget blows.

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md, C1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.ls_files import tracked_files
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
    """On an all-ASCII tree the two enumerators must agree exactly; any
    divergence here means the bytes path is parsing `-z` output differently,
    not merely preserving more of it."""
    decoded = tracked_files(repo)
    as_bytes = tracked_files_bytes(repo)
    assert tuple(sorted(e.decode("utf-8") for e in as_bytes)) == tuple(sorted(decoded))


def test_pathspec_scopes_the_result(repo: Path) -> None:
    only_py = tracked_files_bytes(repo, "*.py")
    assert b"alpha.py" in only_py
    assert b"beta.txt" not in only_py


def test_never_substitutes_replacement_character(repo: Path) -> None:
    """U+FFFD encodes to b'\\xef\\xbf\\xbd'. Its presence would mean a decode
    happened somewhere on the path -- the exact defect this module exists to
    avoid."""
    assert all(b"\xef\xbf\xbd" not in entry for entry in tracked_files_bytes(repo))


def test_non_repo_folds_to_empty(tmp_path: Path) -> None:
    _tracked_files_bytes_cached.cache_clear()
    assert tracked_files_bytes(tmp_path / "not-a-repo-at-all") == ()


def test_one_spawn_per_root_pathspec_pair(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lru_cache contract AC3's three-spawn budget rests on."""
    _tracked_files_bytes_cached.cache_clear()
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(
        "coordinator_core.git.ls_files_bytes.subprocess.run", counting_run
    )
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
    real_run = subprocess.run

    def capturing_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen_argv.extend(str(a) for a in argv)
        seen_kwargs.update(kwargs)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(
        "coordinator_core.git.ls_files_bytes.subprocess.run", capturing_run
    )
    tracked_files_bytes(repo, "*.py")

    assert seen_argv[1:] == [
        "-C",
        str(Path(repo).resolve()),
        "ls-files",
        "-z",
        "--",
        "*.py",
    ]
    assert "text" not in seen_kwargs and seen_kwargs.get("encoding") is None, (
        "a text-mode subprocess would translate newlines and defeat the "
        "byte-exactness this module exists for"
    )
    assert seen_kwargs.get("stdin") is subprocess.DEVNULL
    assert seen_kwargs.get("timeout") == 10


def test_use_cache_false_sees_files_added_after_the_first_cached_call(
    repo: Path,
) -> None:
    """F4 regression: the cached path pins a root's tracked-file list for
    the life of the cache (correct for a one-shot CLI, wrong for a
    warm-served caller). `use_cache=False` must bypass that pin and see a
    file added after the FIRST cached call, without needing to
    `cache_clear()`."""
    _tracked_files_bytes_cached.cache_clear()
    before = tracked_files_bytes(repo)  # populates the cache
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
    """A `use_cache=False` call must always re-spawn -- never served from,
    and never written into, the `lru_cache` -- independent of any other
    cached call for the same (repo_root, pathspec)."""
    _tracked_files_bytes_cached.cache_clear()
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(
        "coordinator_core.git.ls_files_bytes.subprocess.run", counting_run
    )
    tracked_files_bytes(repo, use_cache=False)
    tracked_files_bytes(repo, use_cache=False)
    tracked_files_bytes(repo, use_cache=False)
    assert len(calls) == 3, f"expected one spawn per call, saw {len(calls)}"
    assert _tracked_files_bytes_cached.cache_info().currsize == 0


def test_tracked_files_is_unmodified_by_this_module() -> None:
    """C1's negative spec: the decoding enumerator keeps its Tuple[str, ...]
    return and five-plus call sites keep working."""
    from coordinator_core.git import ls_files

    assert ls_files.__all__ == ["tracked_files"]
