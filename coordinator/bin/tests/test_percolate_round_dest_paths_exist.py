"""test_percolate_round_dest_paths_exist.py — chunking + per-row attribution
coverage for `_dest_paths_exist` (percolate-round.py).

Review: state/subagent-share/a3d742ff-223c-4133-aedd-ed60ce61b558/amp-review-s6.md

Finding 3 (LOW): `_dest_paths_exist` batches every DELETE/REMOVE-tagged row
of a round into one `git ls-files --error-unmatch -- <paths>` argv with no
size cap. The sibling site in this same slice,
`reap-stale-subagent-sidecars.py::_tracked_paths`, was explicitly
engineered around the ~32KB Windows `CreateProcess` argv ceiling because
`git ls-files` has no `--pathspec-from-file` support. `_dest_paths_exist`
now chunks the same way (`_chunk_paths_by_argv_bytes`) -- these tests pin
that chunking actually happens past the cap, and that per-row attribution
survives a chunk boundary.

Finding 5 (INFO): no existing test exercised a MIXED tracked/untracked
batch through `_dest_paths_exist` against a REAL git repo. Per-item
attribution lost or misaligned in a batched call is the exact class this
amplification chain has already produced twice, so
`test_mixed_tracked_and_untracked_batch_real_git_attribution_preserved`
below constructs 2+ delete candidates spanning both outcomes and asserts
per-row attribution via a real `git` invocation (never mocked, per
`test_percolate_round_commit_pathspec.py`'s own real-git precedent for
exactly this class of defect).

Run: python -m pytest coordinator/bin/tests/test_percolate_round_dest_paths_exist.py -q
"""
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


# ---------------------------------------------------------------------------
# Finding 3 — chunking past the argv byte cap, per-row attribution across
# chunk boundaries.
# ---------------------------------------------------------------------------


def test_chunk_paths_by_argv_bytes_splits_past_cap():
    # 10 paths of ~40 bytes each under a tiny cap forces multiple chunks;
    # every input path must appear in exactly one chunk, in order.
    paths = [f"some/deletion/candidate/path-{i:03d}.py" for i in range(10)]
    chunks = _mod._chunk_paths_by_argv_bytes(paths, cap=150)

    assert len(chunks) > 1
    flattened = [p for chunk in chunks for p in chunk]
    assert flattened == paths
    for chunk in chunks:
        assert chunk  # no empty chunk


def test_chunk_paths_by_argv_bytes_single_chunk_under_cap():
    paths = ["a.py", "b.py", "c.py"]
    chunks = _mod._chunk_paths_by_argv_bytes(paths, cap=_mod._LS_FILES_ARGV_BYTE_CAP)
    assert chunks == [paths]


def test_chunk_paths_by_argv_bytes_never_splits_a_single_path():
    # A single path longer than the cap still gets its own chunk rather
    # than being truncated or dropped.
    long_path = "x" * 500
    chunks = _mod._chunk_paths_by_argv_bytes(["short.py", long_path], cap=100)
    assert chunks[-1] == [long_path]
    assert sum(chunks, []) == ["short.py", long_path]


def test_dest_paths_exist_issues_one_ls_files_spawn_per_chunk(monkeypatch, tmp_path):
    """Mutation-verify (Finding 3 body): under a small cap, a batch of
    deletion candidates that all miss the worktree/symlink fast-path must
    trigger MORE THAN ONE `git ls-files` spawn -- pins the chunking fix.
    Pre-fix (single unbounded `-- <paths>` argv), this would be exactly
    one `_run` call regardless of batch size; this test would fail red
    (asserting `calls == 1`, not `> 1`) against that pre-fix shape."""
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(_mod, "_LS_FILES_ARGV_BYTE_CAP", 150)

    rels = [f"deletion/candidate/path-{i:03d}.py" for i in range(12)]
    calls = []

    def _fake_run(cmd, **_kwargs):
        assert "ls-files" in cmd
        chunk = cmd[cmd.index("--") + 1:]
        calls.append(chunk)
        # Every candidate reported as tracked -- kept.
        return _mod.subprocess.CompletedProcess(cmd, 0, "\n".join(chunk) + "\n", "")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    result = _mod._dest_paths_exist(str(dest), rels)

    assert len(calls) > 1
    # Every rel appears in exactly one chunk, and per-row attribution
    # survives the chunk split.
    seen_across_chunks = [rel for chunk in calls for rel in chunk]
    assert seen_across_chunks == rels
    assert result == {rel: True for rel in rels}


def test_dest_paths_exist_chunk_boundary_preserves_mixed_attribution(monkeypatch, tmp_path):
    """A chunked call where one chunk reports its paths tracked and the
    NEXT chunk reports its paths NOT tracked must still attribute each
    `rel` to its own chunk's verdict -- not bleed one chunk's result into
    another's."""
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
    """A returncode outside {0, 1} for one chunk (undetermined probe) must
    fail OPEN for that chunk's rels only -- a sibling chunk's own
    successful, determinate verdict must not be clobbered."""
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


# ---------------------------------------------------------------------------
# Finding 5 — real-git mixed tracked/untracked batch, per-row attribution.
# ---------------------------------------------------------------------------


def test_mixed_tracked_and_untracked_batch_real_git_attribution_preserved(tmp_path):
    """A single `_dest_paths_exist` call over 2+ delete candidates, some
    still tracked at dest and some already gone from both worktree and
    index, against a REAL git repo (never mocked) -- confirms per-row
    attribution is not lost or misaligned by the batched call. This is
    exactly the class of bug (per-item attribution corrupted by batching)
    this amplification chain has already produced twice."""
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
            "still-tracked.py",       # tracked in the index -- True
            "never-existed-1.py",     # absent from both -- False
            "never-existed-2.py",     # absent from both -- False
        ],
    )

    assert result == {
        "still-tracked.py": True,
        "never-existed-1.py": False,
        "never-existed-2.py": False,
    }


def test_mixed_batch_with_worktree_fast_path_and_git_probe_combined(tmp_path):
    """A batch mixing the worktree/symlink fast-path (file physically
    still present at dest) with entries that need the git probe -- both
    outcome classes attributed correctly within one call."""
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
    # Physically removed, still index-tracked (real publish swap shape).
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
