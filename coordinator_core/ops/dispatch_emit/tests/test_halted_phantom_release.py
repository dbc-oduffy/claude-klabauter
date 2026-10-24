"""phantom_holders: dead owner and no own hunk in any withheld path."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_phantom_release as hpr
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.session import liveness

H = "ask-20261024T000000-aaaaaa"
PATH = "src/target.py"


def _patch(monkeypatch, owners: dict, live: set):
    monkeypatch.setattr(halted_run, "recorded_emitter", lambda root, rid: owners.get(rid))
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid in live)


def _dirty(calls: list, paths=()):
    def dirty():
        calls.append(1)
        return set(paths)

    return dirty


def test_dead_and_clean_is_phantom(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {H: "s1"}, set())
    got = hpr.phantom_holders(tmp_path, [H], {H: {PATH}}, _dirty([]), lambda r, p: set(p))
    assert got == {H}


def test_live_owner_is_not_phantom_and_dirty_never_read(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {H: "s1"}, {"s1"})
    calls: list = []
    got = hpr.phantom_holders(tmp_path, [H], {H: {PATH}}, _dirty(calls), lambda r, p: set())
    assert got == set()
    assert calls == []


def test_unknown_owner_is_not_phantom(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {}, set())
    calls: list = []
    assert hpr.phantom_holders(tmp_path, [H], {H: {PATH}}, _dirty(calls), lambda r, p: set()) == set()
    assert calls == []


def test_dead_dirty_attributed_holds(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {H: "s1"}, set())
    got = hpr.phantom_holders(tmp_path, [H], {H: {PATH}}, _dirty([], {PATH}), lambda r, p: set(p))
    assert got == set()


def test_dead_dirty_not_attributed_is_phantom(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {H: "s1"}, set())
    asked: list = []

    def attributable(rid, paths):
        asked.append((rid, set(paths)))
        return set()

    got = hpr.phantom_holders(tmp_path, [H], {H: {PATH}}, _dirty([], {PATH, "other.py"}), attributable)
    assert got == {H}
    assert asked == [(H, {PATH})]


def test_empty_withheld_is_not_phantom(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {H: "s1"}, set())
    assert hpr.phantom_holders(tmp_path, [H], {H: set()}, _dirty([]), lambda r, p: set()) == set()


def test_reader_exception_returns_empty(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {H: "s1"}, set())

    def boom():
        raise RuntimeError("status failed")

    assert hpr.phantom_holders(tmp_path, [H], {H: {PATH}}, boom, lambda r, p: set()) == set()
