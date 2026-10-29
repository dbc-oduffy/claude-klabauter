"""live_run_writes: footprints of live runs from token records and staged manifests."""

from __future__ import annotations

import json

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import live_run_writes, run_token

SCRIPT = "scratch/warp/x.mjs"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    return root


def _stage(root, run_id, rows=(), declared=(), docs=()):
    d = root / "scratch" / "warp" / run_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "rows": [{"id": f"C{i}", "writes": list(w)} for i, w in enumerate(rows)],
                "review_declared_paths": list(declared),
                "doc_writes": list(docs),
            }
        ),
        encoding="utf-8",
    )


def test_staged_run_footprint_unions_rows_declared_and_docs(repo):
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    _stage(repo, "r1", rows=[["a/x.py"], ["b/y.py"]], declared=["agents/z.md"], docs=["docs/d.md"])
    assert live_run_writes.live_footprints(repo, exclude_run_id=None) == {
        "r1": frozenset({"a/x.py", "b/y.py", "agents/z.md", "docs/d.md"})
    }


def test_unstaged_run_is_none_and_exclude_drops_own_run(repo):
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    run_token.mint(repo, "r2", SCRIPT, session_id=None)
    assert live_run_writes.live_footprints(repo, exclude_run_id="r2") == {"r1": None}


def test_one_call_lists_each_launch_dir_at_most_once(repo, monkeypatch):
    for i in range(4):
        run_token.mint(repo, f"r{i}", SCRIPT, session_id=f"s{i}")
    listed: list[str] = []
    real = warp_live.os.listdir

    def spy(path="."):
        listed.append(str(path))
        return real(path)

    monkeypatch.setattr(warp_live.os, "listdir", spy)
    live_run_writes.live_footprints(repo, exclude_run_id=None)
    launch_dirs = set(warp_live._launch_dirs(None))
    mine = [p for p in listed if p in launch_dirs]
    assert len(mine) == len(set(mine))


def test_ended_run_is_absent(repo, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    real = warp_live.time.time()
    monkeypatch.setattr(warp_live.time, "time", lambda: real + warp_live.UNLAUNCHED_GRACE_SECONDS + 60)
    assert live_run_writes.live_footprints(repo, exclude_run_id=None) == {}


def test_overlaps_exact_dir_prefix_and_none():
    fp = {
        "r1": frozenset({"a/b/x.py"}),
        "r2": frozenset({"c/y.py"}),
        "r3": None,
        "r4": frozenset({"a"}),
    }
    got = live_run_writes.overlaps(["a\\b/x.py", "c/other.py"], fp)
    assert got == {"r1": ["a/b/x.py"], "r4": ["a"]}
    assert live_run_writes.overlaps(["a/b"], fp) == {"r1": ["a/b/x.py"], "r4": ["a"]}
    assert live_run_writes.overlaps([], fp) == {}
