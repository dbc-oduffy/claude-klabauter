"""live_run_writes: footprints of live runs from token records and staged manifests."""

from __future__ import annotations

import json

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import live_run_index, live_run_writes, run_token

SCRIPT = "scratch/warp/x.mjs"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "locks"))
    return root


def _index(root):
    got = live_run_index.read(root, 2000.0)
    assert got is not live_run_index.MISSING and got is not live_run_index.STALLED
    return got


def _submit(root, run_id):
    warp_live.record_submission(
        root, run_id, session_id="s", transcript_path=None, script_path=SCRIPT, sizing_rel=None
    )


def test_index_built_by_mint_stage_consume_matches_live_footprints(repo):
    for rid, rows in (("r1", [["a/x.py"]]), ("r2", [["b/y.py"], ["c/z.py"]]), ("r3", None)):
        run_token.mint(repo, rid, SCRIPT, session_id=None)
        if rows is not None:
            _stage(repo, rid, rows=rows, declared=["d/e.md"])
            manifest = json.loads((repo / "scratch" / "warp" / rid / "manifest.json").read_text(encoding="utf-8"))
            live_run_writes.index_stage(repo, rid, manifest)
    run_token.mint(repo, "gone", SCRIPT, session_id=None)
    run_token.consume(repo, "gone")
    run_token.mint(repo, "dropped", SCRIPT, session_id=None)
    run_token.discard(repo, "dropped")
    index = _index(repo)
    assert index == live_run_writes.live_footprints(repo, exclude_run_id=None)
    for writes in (["a/x.py"], ["d"], ["c/z.py", "q"], ["nothing"]):
        assert live_run_index.overlaps(writes, index) == live_run_writes.overlaps(
            writes, live_run_writes.live_footprints(repo, exclude_run_id=None)
        )


def test_remint_resets_a_staged_footprint(repo):
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    live_run_writes.index_stage(repo, "r1", {"rows": [{"writes": ["a.py"]}]})
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    assert _index(repo) == {"r1": None}


def test_unlaunched_entry_ages_out_launched_entry_stays(repo):
    run_token.mint(repo, "unlaunched", SCRIPT, session_id=None)
    run_token.mint(repo, "launched", SCRIPT, session_id="s")
    _submit(repo, "launched")
    assert live_run_writes.prune_index(repo) == []
    past = __import__("time").time() + warp_live.UNLAUNCHED_GRACE_SECONDS + 60
    assert live_run_writes.prune_index(repo, now=past) == ["unlaunched"]
    assert set(_index(repo)) == {"launched"}
    run_token.consume(repo, "launched")
    assert _index(repo) == {}


def test_ended_marker_drops_a_launched_entry(repo):
    run_token.mint(repo, "r1", SCRIPT, session_id="s")
    _submit(repo, "r1")
    marker = warp_live._ask_record_dir(repo, "r1")
    marker.mkdir(parents=True, exist_ok=True)
    (marker / warp_live._HALTED_FILE).write_text(json.dumps({"status": warp_live._STATUS_HALTED}), encoding="utf-8")
    assert live_run_writes.prune_index(repo) == ["r1"]


@pytest.mark.spawns_process
def test_concurrent_mints_in_separate_processes_all_land(repo):
    import subprocess
    import sys

    code = (
        "import sys; from pathlib import Path; from coordinator_core.ops.dispatch_emit import run_token;"
        "[run_token.mint(Path(sys.argv[1]), f'{sys.argv[2]}{i}', sys.argv[3], session_id=None) for i in range(4)]"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(repo), f"p{n}_", SCRIPT],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for n in range(4)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0, 0]
    assert set(_index(repo)) == {f"p{n}_{i}" for n in range(4) for i in range(4)}


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
