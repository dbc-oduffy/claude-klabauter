"""Within one call_io_scope a path's checkin form is computed once however many legs ask."""

from __future__ import annotations

from coordinator_core.git import call_io_scope, checkin_attrs, commit


def _count_dispositions(monkeypatch):
    calls = []
    orig = checkin_attrs.checkin_disposition

    def inner(root, rel):
        calls.append(rel)
        return orig(root, rel)

    monkeypatch.setattr(checkin_attrs, "checkin_disposition", inner)
    return calls


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_three_legs_compute_each_path_once_in_a_scope(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = _count_dispositions(monkeypatch)
    paths = [f"f{i}.txt" for i in range(5)]
    with call_io_scope.call_io_scope():
        for _ in range(3):
            for p in paths:
                assert commit._checkin_bytes(root, p, b"x\n") == b"x\n"
    assert len(calls) == len(paths)


def test_outside_a_scope_every_call_computes(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = _count_dispositions(monkeypatch)
    for _ in range(3):
        commit._checkin_bytes(root, "a.txt", b"x\n")
    assert len(calls) == 3


def test_changed_content_is_not_served_stale(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = _count_dispositions(monkeypatch)
    with call_io_scope.call_io_scope():
        assert commit._checkin_bytes(root, "a.txt", b"one\n") == b"one\n"
        assert commit._checkin_bytes(root, "a.txt", b"two\n") == b"two\n"
    assert len(calls) == 2
