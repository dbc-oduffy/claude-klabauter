"""A claim-index rebuild never deletes the previous index on the caller's path."""

import os
import time
import types

import pytest

from coordinator_core.session import claim_index, touch_record

SWEEPS = []


@pytest.fixture(autouse=True)
def _no_background_sweep(monkeypatch):
    SWEEPS.clear()
    monkeypatch.setattr(claim_index, "_spawn_sweep", lambda index_dir: SWEEPS.append(index_dir))


def _sink(base, sid):
    return os.path.join(base, sid, "touch-record.jsonl")


def _claim(base, sid, path):
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


def test_rebuild_over_prior_streams_never_calls_rmtree(tmp_path, monkeypatch):
    base = str(tmp_path / "sessions")
    _claim(base, "peer", "a.py")
    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}
    index_dir = touch_record.claim_index_dir(base)
    stale = touch_record.claim_index_file(index_dir, "p", "gone.py")
    with open(stale, "w", encoding="utf-8") as fh:
        fh.write("ghost/touch-record.jsonl\n")
    os.remove(os.path.join(index_dir, touch_record.CLAIM_INDEX_COMPLETE))
    monkeypatch.setattr(claim_index, "shutil", types.SimpleNamespace(rmtree=_boom))
    _claim(base, "late", "b.py")

    start = time.process_time()
    result = claim_index.lookup(["a.py", "b.py", "gone.py"], sessions_dir=base)
    elapsed = time.process_time() - start

    assert result == {"a.py": ["peer"], "b.py": ["late"], "gone.py": []}
    assert os.path.isfile(os.path.join(index_dir, touch_record.CLAIM_INDEX_COMPLETE))
    assert not os.path.exists(stale)
    assert elapsed < 0.5


def test_prior_index_is_retired_by_rename_and_swept_later(tmp_path):
    base = str(tmp_path / "sessions")
    _claim(base, "peer", "a.py")
    claim_index.lookup(["a.py"], sessions_dir=base)
    index_dir = touch_record.claim_index_dir(base)
    os.remove(os.path.join(index_dir, touch_record.CLAIM_INDEX_COMPLETE))

    claim_index.lookup(["a.py"], sessions_dir=base)

    parent = os.path.dirname(index_dir)
    retired = [e for e in os.listdir(parent) if e.startswith("v1" + claim_index._RETIRED_MARK)]
    assert len(retired) == 1
    assert SWEEPS == [index_dir, index_dir]
    claim_index._sweep_retired(index_dir)
    assert not [e for e in os.listdir(parent) if e.startswith("v1" + claim_index._RETIRED_MARK)]
    assert os.path.isdir(index_dir)


def test_failed_rename_answers_without_an_index(tmp_path, monkeypatch):
    base = str(tmp_path / "sessions")
    _claim(base, "peer", "a.py")
    claim_index.lookup(["a.py"], sessions_dir=base)
    index_dir = touch_record.claim_index_dir(base)
    os.remove(os.path.join(index_dir, touch_record.CLAIM_INDEX_COMPLETE))
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError("held")))

    assert claim_index._begin_index(base, index_dir) is None
    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}
