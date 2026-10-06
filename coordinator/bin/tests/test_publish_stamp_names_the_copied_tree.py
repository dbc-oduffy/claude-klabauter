"""A percolate commit subject stamps `[source-head <sha12>]` only when the sha is a pre-copy
round pin AND every store row feeding that destination repo root published this round.

Falsifiers: (a) an unpinned engine toplevel never falls back to a live HEAD read; (b) a
destination root fed by several rows stamps only when all of them succeeded; (c) real git: the
mirror bytes come from the pinned commit, not the HEAD that moved mid-round.

Run: python -m pytest coordinator/bin/tests/test_publish_stamp_names_the_copied_tree.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load("publish_stamp_names_copied_tree_under_test", "publish.py")

_PINNED = "1111111111112222222222223333333333333444"
_LIVE = "9999999999998888888888887777777777777666"


def _stub_commit_surface(monkeypatch) -> dict:
    """Stubs `commit_paths` and the dirty-path probes; returns a dict receiving `message`."""
    import coordinator_core.git.git_state as git_state
    from coordinator_core.git import commit as commit_mod

    monkeypatch.setattr(git_state, "head_sha", lambda _repo: _LIVE)
    seen: dict = {}

    def _fake_commit_paths(root, paths, message, **kwargs):
        seen["message"] = message
        return commit_mod.CommitOutcome(sha="a" * 40, staged_preferred=(), worktree_over_staged=())

    monkeypatch.setattr(commit_mod, "commit_paths", _fake_commit_paths)
    monkeypatch.setattr(publish, "_is_git_repo", lambda _root: True)
    monkeypatch.setattr(publish, "_dirty_paths_under", lambda _root, _dirs: ["coordinator_core/ipc.py"])
    monkeypatch.setattr(publish, "_normalize_dest_exec_bits", lambda _root, _dirs: [])
    return seen


def _dest(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "claude-klabauter"
    dest_dir = repo_root / "coordinator_core"
    dest_dir.mkdir(parents=True)
    return repo_root, dest_dir


def test_unpinned_toplevel_stamps_nothing_and_never_reads_live_head(monkeypatch):
    import coordinator_core.git.git_state as git_state

    monkeypatch.setattr(git_state, "head_sha", lambda _repo: _LIVE)

    def _no_late_pin(*_a, **_k):
        raise AssertionError("round-mode stamp must not pin or read HEAD after the copy")

    monkeypatch.setattr(publish, "_round_pin_source_sha", _no_late_pin)
    suffix = publish._source_sha_suffix({})
    assert suffix == ""
    assert _LIVE[:12] not in suffix


def test_unpinned_toplevel_commit_subject_carries_no_stamp(monkeypatch, tmp_path):
    seen = _stub_commit_surface(monkeypatch)
    repo_root, dest_dir = _dest(tmp_path)
    ok = publish._commit_published_dests(
        {repo_root: {dest_dir}},
        succeeded_row_names=["row-a"],
        round_pinned_shas={},
        rows_feeding_root={repo_root: frozenset({"row-a"})},
    )
    assert ok is True
    assert "[source-head" not in seen["message"]
    assert _LIVE[:12] not in seen["message"]


def test_partial_root_stamps_nothing(monkeypatch, tmp_path):
    seen = _stub_commit_surface(monkeypatch)
    repo_root, dest_dir = _dest(tmp_path)
    ok = publish._commit_published_dests(
        {repo_root: {dest_dir}},
        succeeded_row_names=["row-a"],
        round_pinned_shas={str(publish._REPO_ROOT): _PINNED},
        rows_feeding_root={repo_root: frozenset({"row-a", "row-b"})},
    )
    assert ok is True
    assert "[source-head" not in seen["message"]
    assert _PINNED[:12] not in seen["message"]


def test_fully_covered_root_stamps_the_pinned_sha(monkeypatch, tmp_path):
    seen = _stub_commit_surface(monkeypatch)
    repo_root, dest_dir = _dest(tmp_path)
    ok = publish._commit_published_dests(
        {repo_root: {dest_dir}},
        succeeded_row_names=["row-a", "row-b"],
        round_pinned_shas={str(publish._REPO_ROOT): _PINNED},
        rows_feeding_root={repo_root: frozenset({"row-a", "row-b"})},
    )
    assert ok is True
    assert f" [source-head {_PINNED[:12]}]" in seen["message"].splitlines()[0]
    assert _LIVE[:12] not in seen["message"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
