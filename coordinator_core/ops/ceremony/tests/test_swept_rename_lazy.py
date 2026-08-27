"""C5, state/dispatch-briefs/2026-08-27-the-commit-op-resolves-one-pass-
context/C5.md: `_swept_rename_delete_paths` must not call `head_blobs`
(pack-index parse) or build its reverse sha->paths map over the index
unless at least one candidate path is absent from the index but present
at HEAD -- the ordinary-commit common case touches zero objects.

Negative-spec companion to `test_swept_rename_delete_paths_*` in
`test_commit_pipeline.py`: those prove correctness of the classification;
these prove the ordering fix -- the absent-from-index test gates both the
`head_blobs` call and the reverse-map build, in that order, never after.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.commit_pipeline as cp

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


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


class _CountingIndex(dict):
    """`read_index` return-shape stand-in that counts `.items()` calls --
    the reverse sha->paths map build is the only caller of `.items()` on
    this object, so a zero count is direct proof the map was never built."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.items_calls = 0

    def items(self):
        self.items_calls += 1
        return super().items()


def test_ordinary_commit_path_present_in_index_never_calls_head_blobs(tmp_path, monkeypatch):
    """The common case (`explicit_stage()` on a path already in the index --
    e.g. a modified, previously-committed file) must resolve with ZERO
    calls to `head_blobs` (zero pack-index parses) and ZERO map entries
    built (the reverse sha->paths map is never touched)."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a.md", "one\n")
    _git(["add", "--", "a.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "a.md", "two\n")
    _git(["add", "--", "a.md"], repo)

    head_blobs_calls = []

    def _fail_if_called(*a, **k):
        head_blobs_calls.append((a, k))
        raise AssertionError("head_blobs must not be called when no path is absent from the index")

    monkeypatch.setattr(cp, "head_blobs", _fail_if_called)

    real_read_index = cp.read_index
    counting_index_holder = {}

    def _wrapped_read_index(root):
        idx = _CountingIndex(real_read_index(root))
        counting_index_holder["index"] = idx
        return idx

    monkeypatch.setattr(cp, "read_index", _wrapped_read_index)

    swept_rename, swept_delete = cp._swept_rename_delete_paths(repo, ["a.md"])

    assert swept_rename == {}
    assert swept_delete == set()
    assert head_blobs_calls == [], "zero pack parses is the exit criterion, not fewer"
    assert counting_index_holder["index"].items_calls == 0, (
        "reverse sha->paths map must not be built when there are no "
        "absent-from-index candidates"
    )


def test_genuine_swept_rename_still_detected_and_head_blobs_scoped_to_candidates(
    tmp_path, monkeypatch
):
    """A genuine swept rename (path absent from index, present at HEAD,
    exact (mode, sha) match elsewhere in the index) is still detected --
    and `head_blobs` is called with ONLY the absent-from-index candidate,
    never the full `paths` argument."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "old.md", "content\n")
    _seed_file(repo, "untouched.md", "kept\n")
    _git(["add", "--", "old.md", "untouched.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["mv", "old.md", "new.md"], repo)

    real_head_blobs = cp.head_blobs
    head_blobs_calls = []

    def _spy(root, paths):
        head_blobs_calls.append(list(paths))
        return real_head_blobs(root, paths)

    monkeypatch.setattr(cp, "head_blobs", _spy)

    swept_rename, swept_delete = cp._swept_rename_delete_paths(
        repo, ["old.md", "untouched.md"]
    )

    assert swept_rename == {"old.md": "new.md"}
    assert swept_delete == set()
    assert head_blobs_calls == [["old.md"]], (
        "head_blobs must be scoped to the absent-from-index candidates only "
        "-- 'untouched.md' is present in the index and must be excluded"
    )
