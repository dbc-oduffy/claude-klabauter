"""phantom_holders: a dead owner releases, hunks or not."""

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


def test_dead_owner_is_phantom(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {H: "s1"}, set())
    assert hpr.phantom_holders(tmp_path, [H], {H: {PATH}}) == {H}


def test_live_owner_is_not_phantom(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {H: "s1"}, {"s1"})
    assert hpr.phantom_holders(tmp_path, [H], {H: {PATH}}) == set()


def test_unknown_owner_is_not_phantom(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {}, set())
    assert hpr.phantom_holders(tmp_path, [H], {H: {PATH}}) == set()


def test_empty_withheld_is_not_phantom(monkeypatch, tmp_path: Path):
    _patch(monkeypatch, {H: "s1"}, set())
    assert hpr.phantom_holders(tmp_path, [H], {H: set()}) == set()


def test_reader_exception_returns_empty(monkeypatch, tmp_path: Path):
    def boom(root, rid):
        raise RuntimeError("receipt unreadable")

    monkeypatch.setattr(halted_run, "recorded_emitter", boom)
    assert hpr.phantom_holders(tmp_path, [H], {H: {PATH}}) == set()
