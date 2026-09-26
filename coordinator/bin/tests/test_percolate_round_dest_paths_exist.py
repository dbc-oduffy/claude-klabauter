from __future__ import annotations

import importlib.util
import subprocess as _subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = {"creationflags": getattr(_subprocess, "CREATE_NO_WINDOW", 0)}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_dest_paths_exist", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git_run(args, **kwargs):
    return _subprocess.run(args, capture_output=True, text=True, **_NO_WINDOW, **kwargs)


def _init_real_repo(repo_root: Path) -> None:
    _git_run(["git", "init", "-q"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "init"],
        cwd=str(repo_root), check=True,
    )


def test_chunk_paths_by_argv_bytes_splits_past_cap():
    paths = [f"some/deletion/candidate/path-{i:03d}.py" for i in range(10)]
    chunks = _mod._chunk_paths_by_argv_bytes(paths, cap=150)

    assert len(chunks) > 1
    flattened = [p for chunk in chunks for p in chunk]
    assert flattened == paths
    for chunk in chunks:
        assert chunk


def test_chunk_paths_by_argv_bytes_single_chunk_under_cap():
    paths = ["a.py", "b.py", "c.py"]
    chunks = _mod._chunk_paths_by_argv_bytes(paths, cap=_mod._LS_FILES_ARGV_BYTE_CAP)
    assert chunks == [paths]


def test_chunk_paths_by_argv_bytes_never_splits_a_single_path():
    long_path = "x" * 500
    chunks = _mod._chunk_paths_by_argv_bytes(["short.py", long_path], cap=100)
    assert chunks[-1] == [long_path]
    assert sum(chunks, []) == ["short.py", long_path]


def test_dest_paths_exist_issues_one_ls_files_spawn_per_chunk(monkeypatch, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(_mod, "_LS_FILES_ARGV_BYTE_CAP", 150)

    rels = [f"deletion/candidate/path-{i:03d}.py" for i in range(12)]
    calls = []

    def _fake_run(cmd, **_kwargs):
        assert "ls-files" in cmd
        chunk = cmd[cmd.index("--") + 1:]
        calls.append(chunk)
        return _mod.subprocess.CompletedProcess(cmd, 0, "\n".join(chunk) + "\n", "")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    result = _mod._dest_paths_exist(str(dest), rels)

    assert len(calls) > 1
    seen_across_chunks = [rel for chunk in calls for rel in chunk]
    assert seen_across_chunks == rels
    assert result == {rel: True for rel in rels}


def test_dest_paths_exist_chunk_boundary_preserves_mixed_attribution(monkeypatch, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(_mod, "_LS_FILES_ARGV_BYTE_CAP", 50)

    tracked_rels = ["chunk-a-tracked-1.py", "chunk-a-tracked-2.py"]
    untracked_rels = ["chunk-b-untracked-1.py", "chunk-b-untracked-2.py"]
    rels = tracked_rels + untracked_rels

    def _fake_run(cmd, **_kwargs):
        chunk = cmd[cmd.index("--") + 1:]
        if chunk == tracked_rels:
            return _mod.subprocess.CompletedProcess(cmd, 0, "\n".join(chunk) + "\n", "")
        if chunk == untracked_rels:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        raise AssertionError(f"unexpected chunk shape: {chunk!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    result = _mod._dest_paths_exist(str(dest), rels)

    assert result == {
        "chunk-a-tracked-1.py": True,
        "chunk-a-tracked-2.py": True,
        "chunk-b-untracked-1.py": False,
        "chunk-b-untracked-2.py": False,
    }


def test_dest_paths_exist_chunk_probe_failure_fails_open_for_only_that_chunk(monkeypatch, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(_mod, "_LS_FILES_ARGV_BYTE_CAP", 40)

    good_rels = ["ok-chunk-1.py", "ok-chunk-2.py"]
    bad_rels = ["undetermined-1.py", "undetermined-2.py"]
    rels = good_rels + bad_rels

    def _fake_run(cmd, **_kwargs):
        chunk = cmd[cmd.index("--") + 1:]
        if chunk == good_rels:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        if chunk == bad_rels:
            return _mod.subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        raise AssertionError(f"unexpected chunk shape: {chunk!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    result = _mod._dest_paths_exist(str(dest), rels)

    assert result == {
        "ok-chunk-1.py": False,
        "ok-chunk-2.py": False,
        "undetermined-1.py": True,
        "undetermined-2.py": True,
    }


def test_mixed_tracked_and_untracked_batch_real_git_attribution_preserved(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)

    tracked_file = repo_root / "still-tracked.py"
    tracked_file.write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )

    result = _mod._dest_paths_exist(
        str(repo_root),
        [
            "still-tracked.py",
            "never-existed-1.py",
            "never-existed-2.py",
        ],
    )

    assert result == {
        "still-tracked.py": True,
        "never-existed-1.py": False,
        "never-existed-2.py": False,
    }


def test_mixed_batch_with_worktree_fast_path_and_git_probe_combined(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)

    still_on_disk = repo_root / "still-on-disk.py"
    still_on_disk.write_text("x\n")
    index_only = repo_root / "index-only.py"
    index_only.write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )
    index_only.unlink()

    result = _mod._dest_paths_exist(
        str(repo_root),
        ["still-on-disk.py", "index-only.py", "gone-entirely.py"],
    )

    assert result == {
        "still-on-disk.py": True,
        "index-only.py": True,
        "gone-entirely.py": False,
    }
