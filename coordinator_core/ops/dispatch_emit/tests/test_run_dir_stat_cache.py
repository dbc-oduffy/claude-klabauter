"""run_dir_stat_cache: stat-keyed reuse, invalidation, LRU bound, thread safety."""
import os
import threading

import pytest

from coordinator_core.ops.dispatch_emit import run_dir_stat_cache as rc


@pytest.fixture(autouse=True)
def _clean():
    rc.clear()
    yield
    rc.clear()


def _reader(calls):
    def compute(p):
        calls.append(p)
        with open(p, "rb") as f:
            return f.read()
    return compute


def test_unchanged_file_is_computed_once(tmp_path):
    f = tmp_path / "a.json"
    f.write_bytes(b"abc")
    calls = []
    for _ in range(5):
        assert rc.cached("ns", f, _reader(calls)) == b"abc"
    assert len(calls) == 1


def test_size_or_mtime_change_recomputes(tmp_path):
    f = tmp_path / "a.json"
    f.write_bytes(b"abc")
    calls = []
    rc.cached("ns", f, _reader(calls))
    f.write_bytes(b"abcd")
    assert rc.cached("ns", f, _reader(calls)) == b"abcd"
    f.write_bytes(b"wxyz")
    st = os.stat(f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert rc.cached("ns", f, _reader(calls)) == b"wxyz"
    assert len(calls) == 3


def test_drop_forces_recompute_of_same_size_rewrite(tmp_path):
    f = tmp_path / "a.json"
    f.write_bytes(b"abc")
    st = os.stat(f)
    calls = []
    rc.cached("ns", f, _reader(calls))
    f.write_bytes(b"xyz")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size and mtime: the documented trap
    assert rc.cached("ns", f, _reader(calls)) == b"abc"
    rc.drop("ns", f)
    assert rc.cached("ns", f, _reader(calls)) == b"xyz"


def test_missing_file_returns_compute_answer_and_is_never_cached(tmp_path):
    f = tmp_path / "gone.json"
    answers = iter([None, "later"])
    assert rc.cached("ns", f, lambda p: next(answers)) is None
    assert rc.size("ns") == 0
    assert rc.cached("ns", f, lambda p: next(answers)) == "later"
    assert rc.size("ns") == 0


def test_deleted_file_evicts_stale_entry(tmp_path):
    f = tmp_path / "a.json"
    f.write_bytes(b"abc")
    rc.cached("ns", f, lambda p: "v")
    f.unlink()
    assert rc.cached("ns", f, lambda p: None) is None
    assert rc.size("ns") == 0


def test_namespaces_are_independent(tmp_path):
    f = tmp_path / "a.json"
    f.write_bytes(b"abc")
    assert rc.cached("n1", f, lambda p: 1) == 1
    assert rc.cached("n2", f, lambda p: 2) == 2
    rc.drop("n1", f)
    assert rc.cached("n2", f, lambda p: 99) == 2


def test_compute_exception_stores_nothing(tmp_path):
    f = tmp_path / "a.json"
    f.write_bytes(b"abc")

    def boom(p):
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        rc.cached("ns", f, boom)
    assert rc.size("ns") == 0


def test_supplied_stat_is_used_without_restat(tmp_path):
    f = tmp_path / "a.json"
    f.write_bytes(b"abc")
    st = os.stat(f)
    calls = []
    rc.cached("ns", f, _reader(calls), st=st)
    f.write_bytes(b"abcd")  # a re-stat would see this; the held stat does not
    rc.cached("ns", f, _reader(calls), st=st)
    assert len(calls) == 1


def test_lru_cap_holds_a_full_warp_scan_and_evicts_least_recent(tmp_path, monkeypatch):
    # Question: does a scan of more run dirs than the cap keep memory bounded, evicting the coldest?
    assert rc.MAX_ENTRIES_PER_NAMESPACE >= 5157  # a 5157-run-dir state/warp must fit in one namespace
    monkeypatch.setattr(rc, "MAX_ENTRIES_PER_NAMESPACE", 3)
    files = []
    for i in range(4):
        p = tmp_path / f"{i}.json"
        p.write_bytes(b"x" * (i + 1))
        files.append(p)
    calls = []
    for p in files[:3]:
        rc.cached("ns", p, _reader(calls))
    rc.cached("ns", files[0], _reader(calls))  # touch 0: 1 is now coldest
    rc.cached("ns", files[3], _reader(calls))  # evicts 1
    assert rc.size("ns") == 3
    n = len(calls)
    rc.cached("ns", files[0], _reader(calls))
    assert len(calls) == n
    rc.cached("ns", files[1], _reader(calls))
    assert len(calls) == n + 1


def test_threads_share_one_computation_per_signature_safely(tmp_path):
    f = tmp_path / "a.json"
    f.write_bytes(b"abc")
    errors = []

    def work():
        try:
            for _ in range(200):
                assert rc.cached("ns", f, lambda p: "v") == "v"
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert rc.size("ns") == 1


def _warp_run(root, run_id):
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    return d


def test_record_dir_is_resolved_once_while_state_dir_keeps_identity(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_contract

    d = _warp_run(tmp_path, "ask-1")
    calls = []
    real = ask_contract._record_dir
    monkeypatch.setattr(ask_contract, "_record_dir", lambda r, i: calls.append(i) or real(r, i))
    first = rc.record_dir(tmp_path, "ask-1")
    assert first == d.resolve() and rc.record_dir(tmp_path, "ask-1") == first
    assert calls == ["ask-1"]


def test_record_dir_recomputes_when_state_dir_is_replaced(tmp_path):
    d = _warp_run(tmp_path, "ask-2")
    scratch = tmp_path / "scratch" / "warp" / "ask-2"
    scratch.mkdir(parents=True)
    (scratch / "run.json").write_text("{}", encoding="utf-8")
    assert rc.record_dir(tmp_path, "ask-2") == d.resolve()
    d.rmdir()
    assert rc.record_dir(tmp_path, "ask-2") == scratch.resolve()


def test_record_dir_sees_a_state_dir_created_mid_scope(tmp_path):
    from coordinator_core.git.call_io_scope import call_io_scope

    scratch = tmp_path / "scratch" / "warp" / "ask-3"
    scratch.mkdir(parents=True)
    (scratch / "run.json").write_text("{}", encoding="utf-8")
    with call_io_scope():
        assert rc.record_dir(tmp_path, "ask-3") == scratch.resolve()
        d = _warp_run(tmp_path, "ask-3")
        assert rc.record_dir(tmp_path, "ask-3") == d.resolve()


def test_record_dir_memoizes_a_present_dir_per_scope_without_restat(tmp_path, monkeypatch):
    from coordinator_core.git.call_io_scope import call_io_scope
    from coordinator_core.ops.dispatch_emit import ask_contract

    d = _warp_run(tmp_path, "ask-4")
    probe = os.path.join(os.fspath(tmp_path), ask_contract.RECORD_DIR_ROOT, "ask-4")
    stats = []
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: stats.append(os.fspath(p)) or real_stat(p, *a, **k))
    with call_io_scope():
        for _ in range(5):
            assert rc.record_dir(tmp_path, "ask-4") == d.resolve()
    assert stats.count(probe) == 1
    with call_io_scope():
        rc.record_dir(tmp_path, "ask-4")
    assert stats.count(probe) == 2  # a new scope re-checks


def test_record_dir_still_refuses_an_unsafe_run_id(tmp_path):
    with pytest.raises(ValueError):
        rc.record_dir(tmp_path, "../escape")
