"""live_run_writes: footprints of live runs from token records and staged manifests."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import live_run_index, live_run_writes, run_token
from pathlib import Path

SCRIPT = "scratch/warp/x.mjs"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _submit(root, run_id) -> None:
    warp_live.record_submission(
        root, run_id, session_id="s", transcript_path=None, script_path=SCRIPT, sizing_rel=None
    )


def test_index_built_by_mint_stage_consume_matches_live_footprints(repo) -> None:
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


def test_remint_resets_a_staged_footprint(repo) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    live_run_writes.index_stage(repo, "r1", {"rows": [{"writes": ["a.py"]}]})
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    assert _index(repo) == {"r1": None}


def test_unlaunched_entry_ages_out_launched_entry_stays(repo) -> None:
    run_token.mint(repo, "unlaunched", SCRIPT, session_id=None)
    run_token.mint(repo, "launched", SCRIPT, session_id="s")
    _submit(repo, "launched")
    assert live_run_writes.prune_index(repo) == []
    past = time.time() + warp_live.UNLAUNCHED_GRACE_SECONDS + 60
    assert live_run_writes.prune_index(repo, now=past) == ["unlaunched"]
    assert set(_index(repo)) == {"launched"}
    run_token.consume(repo, "launched")
    assert _index(repo) == {}


def test_ended_marker_drops_a_launched_entry(repo) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id="s")
    _submit(repo, "r1")
    marker = warp_live._ask_record_dir(repo, "r1")
    marker.mkdir(parents=True, exist_ok=True)
    (marker / warp_live._HALTED_FILE).write_text(json.dumps({"status": warp_live._STATUS_HALTED}), encoding="utf-8")
    assert live_run_writes.prune_index(repo) == ["r1"]


def _mark(root, run_id, status) -> None:
    d = warp_live._ask_record_dir(root, run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / warp_live._HALTED_FILE).write_text(json.dumps({"status": status}), encoding="utf-8")


@pytest.mark.parametrize("status", ["resumed", "superseded", "closed", "landed", "halted"])
def test_prune_drops_a_launched_row_whose_marker_is_over(repo, status) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id="s")
    _submit(repo, "r1")
    _mark(repo, "r1", status)
    assert live_run_writes.prune_index(repo) == ["r1"]
    assert _index(repo) == {}


def test_prune_keeps_a_launched_row_with_an_unknown_marker_status(repo) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id="s")
    _submit(repo, "r1")
    _mark(repo, "r1", "mystery")
    assert live_run_writes.prune_index(repo) == []


def test_deep_prune_drops_a_resumed_row_without_the_transcript_walk(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id="s")
    _submit(repo, "r1")
    _mark(repo, "r1", "resumed")

    def boom(*a, **k):
        raise AssertionError("transcript liveness walked for an over run")

    monkeypatch.setattr(warp_live, "_record_live", boom)
    assert live_run_writes.prune_index(repo, deep=True) == ["r1"]


def test_footprints_and_deep_prune_read_records_only(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id="s")
    _submit(repo, "r1")
    live_run_writes.index_stage(repo, "r1", {"rows": [{"writes": ["a.py"]}]})
    seen: list = []
    real = warp_live._record_live

    def spy(*a, **k) -> bool:
        seen.append(k.get("records_only"))
        return real(*a, **k)

    monkeypatch.setattr(warp_live, "_record_live", spy)
    assert set(live_run_writes.live_footprints(repo, exclude_run_id=None)) == {"r1"}
    live_run_writes.prune_index(repo, deep=True)
    assert seen and all(v is True for v in seen)


def test_resumed_run_holds_no_footprint_but_stays_live_to_session_readers(repo) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id="s")
    _submit(repo, "r1")
    live_run_writes.index_stage(repo, "r1", {"rows": [{"writes": ["a.py"]}]})
    _mark(repo, "r1", "resumed")
    assert live_run_writes.live_footprints(repo, exclude_run_id=None) == {}
    assert warp_live.live_run_for_session(repo, "s") == "r1"


def test_halted_run_holds_no_footprint_and_keeps_its_record(repo) -> None:
    run_token.mint(repo, "h1", SCRIPT, session_id="s")
    run_token.mint(repo, "live", SCRIPT, session_id="s2")
    for rid in ("h1", "live"):
        _stage(repo, rid, rows=[["chain/__main__.py"]])
    _mark(repo, "h1", "halted")
    got = live_run_writes.live_footprints(repo, exclude_run_id=None)
    assert set(got) == {"live"}
    assert live_run_writes.overlaps(["chain/__main__.py"], got) == {"live": ["chain/__main__.py"]}
    assert (warp_live._ask_record_dir(repo, "h1") / warp_live._HALTED_FILE).is_file()


def test_prune_drops_a_launched_row_whose_owner_exited_and_keeps_a_live_owner(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    for rid, sid in (("dead", "gone"), ("alive", "here")):
        run_token.mint(repo, rid, SCRIPT, session_id=sid)
        _submit_as(repo, rid, sid)
    monkeypatch.setattr(warp_live, "_owner_exited", lambda session_id, minted_at: session_id == "gone")
    assert live_run_writes.prune_index(repo) == ["dead"]
    assert set(_index(repo)) == {"alive"}


def _submit_as(root, run_id, session_id) -> None:
    warp_live.record_submission(
        root, run_id, session_id=session_id, transcript_path=None, script_path=SCRIPT, sizing_rel=None
    )


def test_dirless_ghost_row_ages_out(repo) -> None:
    run_token.mint(repo, "ghost", SCRIPT, session_id="s")
    assert not warp_live._ask_record_dir(repo, "ghost").exists()
    past = time.time() + warp_live.UNLAUNCHED_GRACE_SECONDS + 60
    assert live_run_writes.prune_index(repo, now=past) == ["ghost"]


@pytest.mark.spawns_process
def test_concurrent_mints_in_separate_processes_all_land(repo) -> None:
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


def test_the_index_lock_sidecar_survives_a_prefixed_realpath_of_the_index(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    index = live_run_index.index_path(repo)
    real = os.path.realpath

    def sidecars() -> set[str]:
        from coordinator_core import locked_write

        return {p.name for p in locked_write._machine_lock_dir().glob("*.lock")}

    def flaky(p, *a, **k):
        out = real(p, *a, **k)
        return "\\\\?\\" + out if os.path.normcase(str(p)) == os.path.normcase(str(index)) else out

    with live_run_writes._index_lock(repo) as held:
        assert held
    before = sidecars()
    monkeypatch.setattr(os.path, "realpath", flaky)
    with live_run_writes._index_lock(repo) as held:
        assert held
    assert sidecars() == before
    assert len(before) == 1


def _lock_times_out_once(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from coordinator_core import locked_write

    real, calls = locked_write.held_lock, [0]

    def held_lock(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise locked_write.LockTimeout("busy")
        return real(*args, **kwargs)

    monkeypatch.setattr(locked_write, "held_lock", held_lock)
    return calls


def test_an_unlocked_mint_overwritten_by_a_peer_is_redone_under_the_lock(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    run_token.mint(repo, "peer", SCRIPT, session_id=None)
    real_add, adds = live_run_index.add, [0]

    def add_lost_first(root, run_id, minted_at):
        adds[0] += 1
        return True if adds[0] == 1 else real_add(root, run_id, minted_at)

    monkeypatch.setattr(live_run_index, "add", add_lost_first)
    calls = _lock_times_out_once(monkeypatch)
    live_run_writes.index_mint(repo, "mine", 1.0)
    assert set(_index(repo)) == {"peer", "mine"}
    assert calls[0] >= 2


def test_an_unlocked_mint_that_landed_is_not_redone(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _lock_times_out_once(monkeypatch)
    live_run_writes.index_mint(repo, "mine", 1.0)
    assert set(_index(repo)) == {"mine"}
    assert calls[0] == 1


def _stage(root, run_id, rows=(), declared=(), docs=()) -> None:
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


def test_staged_run_footprint_unions_rows_declared_and_docs(repo) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    _stage(repo, "r1", rows=[["a/x.py"], ["b/y.py"]], declared=["agents/z.md"], docs=["docs/d.md"])
    assert live_run_writes.live_footprints(repo, exclude_run_id=None) == {
        "r1": frozenset({"a/x.py", "b/y.py", "agents/z.md", "docs/d.md"})
    }


def test_unstaged_run_is_none_and_exclude_drops_own_run(repo) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    run_token.mint(repo, "r2", SCRIPT, session_id=None)
    assert live_run_writes.live_footprints(repo, exclude_run_id="r2") == {"r1": None}


def test_one_call_lists_each_launch_dir_at_most_once(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(4):
        run_token.mint(repo, f"r{i}", SCRIPT, session_id=f"s{i}")
    listed: list[str] = []
    real = warp_live.os.listdir

    def spy(path: str = ".") -> list[str]:
        listed.append(str(path))
        return real(path)

    monkeypatch.setattr(warp_live.os, "listdir", spy)
    live_run_writes.live_footprints(repo, exclude_run_id=None)
    launch_dirs = set(warp_live._launch_dirs(None))
    mine = [p for p in listed if p in launch_dirs]
    assert len(mine) == len(set(mine))


def test_ended_run_is_absent(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    run_token.mint(repo, "r1", SCRIPT, session_id=None)
    real = warp_live.time.time()
    monkeypatch.setattr(warp_live.time, "time", lambda: real + warp_live.UNLAUNCHED_GRACE_SECONDS + 60)
    assert live_run_writes.live_footprints(repo, exclude_run_id=None) == {}


def test_overlaps_exact_dir_prefix_and_none() -> None:
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


def test_deferred_emit_run_past_the_grace_loses_its_footprint_records_only(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    """Accepted: records-only liveness cannot see a deferred-emit run's transcript-found launch,
    so once the unlaunched grace passes its footprint is dropped. Within the grace it is held."""
    run_token.mint(repo, "d1", SCRIPT, session_id="s")
    _stage(repo, "d1", rows=[["a.py"]])
    assert live_run_writes.live_footprints(repo, exclude_run_id=None) == {"d1": frozenset({"a.py"})}
    real = time.time
    late = real() + warp_live.UNLAUNCHED_GRACE_SECONDS + 60
    monkeypatch.setattr(warp_live.time, "time", lambda: late)
    assert live_run_writes.live_footprints(repo, exclude_run_id=None) == {}


def test_file_claims_drops_directories_and_root_but_keeps_files_and_unwritten_paths(repo) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg" / "a.py").write_text("x", encoding="utf-8")
    got = live_run_writes.file_claims(repo, ["pkg", "pkg/a.py", "pkg/new.py", ".", "", "pkg\\a.py", "pkg\\"])
    assert got == ["pkg/a.py", "pkg/new.py", "pkg\\a.py"]
