"""A claim-index rebuild never deletes the previous index on the caller's path, and a rename that
fails under a reader's handle still builds and serves an index."""

import os
import time
import types

import pytest

from coordinator_core.session import claim_index, touch_record
from pathlib import Path

SWEEPS = []


@pytest.fixture(autouse=True)
def _no_background_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    SWEEPS.clear()
    monkeypatch.setattr(claim_index, "_spawn_sweep", lambda index_dir: SWEEPS.append(index_dir))


def _sink(base, sid):
    return os.path.join(base, sid, "touch-record.jsonl")


def _claim(base, sid, path) -> None:
    touch_record.append_event(
        _sink(base, sid),
        session_id=sid,
        agent_id=None,
        verb="T",
        path=path,
        timestamp=1_780_000_000.0,
        kind=touch_record.KIND_WRITE,
        name=None,
    )


def _boom(*args, **kwargs):
    raise AssertionError("shutil.rmtree on the lookup path")


def _deny_index_dir_rename(monkeypatch, index_dir) -> None:
    """Refuse only the rename of the index directory itself, as a reader's open handle does."""
    real = os.replace

    def _replace(src, dst, *args, **kwargs):
        if os.path.normpath(str(src)) == os.path.normpath(index_dir):
            raise PermissionError("a reader holds a handle inside the old index dir")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _replace)


@pytest.fixture
def stale_build(tmp_path: Path) -> tuple[str, str, str, str]:
    """A built index whose COMPLETE stamp is gone, plus a stream naming a sink that no longer exists."""
    base = str(tmp_path / "sessions")
    _claim(base, "peer", "a.py")
    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}
    index_dir = touch_record.claim_index_dir(base)
    ghost = touch_record.claim_index_file(index_dir, "p", "gone.py")
    with open(ghost, "w", encoding="utf-8") as fh:
        fh.write("ghost/touch-record.jsonl\n")
    complete = os.path.join(index_dir, touch_record.CLAIM_INDEX_COMPLETE)
    os.remove(complete)
    return base, index_dir, complete, ghost


def test_rebuild_over_prior_streams_never_calls_rmtree(stale_build, monkeypatch: pytest.MonkeyPatch) -> None:
    base, index_dir, complete, ghost = stale_build
    monkeypatch.setattr(claim_index, "shutil", types.SimpleNamespace(rmtree=_boom))
    _claim(base, "late", "b.py")

    start = time.process_time()
    result = claim_index.lookup(["a.py", "b.py", "gone.py"], sessions_dir=base)
    elapsed = time.process_time() - start

    assert result == {"a.py": ["peer"], "b.py": ["late"], "gone.py": []}
    assert os.path.isfile(complete)
    assert not os.path.exists(ghost)
    assert elapsed < 0.5


def test_prior_index_is_retired_by_rename_and_swept_later(stale_build) -> None:
    base, index_dir, complete, ghost = stale_build

    claim_index.lookup(["a.py"], sessions_dir=base)

    parent = os.path.dirname(index_dir)
    retired = [e for e in os.listdir(parent) if e.startswith("v1" + claim_index._RETIRED_MARK)]
    assert len(retired) == 1
    assert SWEEPS == [index_dir]
    claim_index._sweep_retired(index_dir)
    assert not [e for e in os.listdir(parent) if e.startswith("v1" + claim_index._RETIRED_MARK)]
    assert os.path.isdir(index_dir)


def test_first_build_retires_nothing_and_spawns_no_sweep(tmp_path: Path) -> None:
    base = str(tmp_path / "sessions")
    _claim(base, "peer", "a.py")

    claim_index.lookup(["a.py"], sessions_dir=base)

    assert SWEEPS == []


def test_a_rename_refused_by_a_reader_handle_still_builds_and_serves_an_index(stale_build, monkeypatch: pytest.MonkeyPatch) -> None:
    base, index_dir, complete, ghost = stale_build
    _deny_index_dir_rename(monkeypatch, index_dir)
    monkeypatch.setattr(claim_index, "shutil", types.SimpleNamespace(rmtree=_boom))
    _claim(base, "late", "b.py")

    start = time.process_time()
    result = claim_index.lookup(["a.py", "b.py", "gone.py"], sessions_dir=base)
    elapsed = time.process_time() - start

    assert result == {"a.py": ["peer"], "b.py": ["late"], "gone.py": []}
    assert os.path.isfile(complete) and os.path.isdir(index_dir) and SWEEPS == []
    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}
    assert elapsed < 0.5


def test_a_second_build_token_stops_the_first_build_stamping(stale_build, monkeypatch: pytest.MonkeyPatch) -> None:
    base, index_dir, complete, ghost = stale_build
    _deny_index_dir_rename(monkeypatch, index_dir)
    first = claim_index._begin_index(base, index_dir)
    second = claim_index._begin_index(base, index_dir)

    assert first and second and first != second
    assert claim_index._read_build_token(index_dir) == second
    assert not os.path.isfile(complete)
