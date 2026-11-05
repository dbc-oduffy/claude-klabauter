"""Loader contract: absent, disabled and present, in one process."""

from __future__ import annotations

import logging
import sys
import types

import pytest

import coordinator_core.native as native

_NAME = "coordinator_core.native._hot"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("COORDINATOR_NATIVE", raising=False)
    native._reset_for_tests()
    yield
    native._reset_for_tests()


def test_absent_returns_none(monkeypatch):
    monkeypatch.setitem(sys.modules, _NAME, None)  # import raises ImportError
    assert native.load() is None


def test_present_returns_module(monkeypatch):
    fake = types.ModuleType(_NAME)
    monkeypatch.setitem(sys.modules, _NAME, fake)
    assert native.load() is fake


@pytest.mark.parametrize("value", ["0", "false", "OFF", "no"])
def test_disabled_returns_none_even_when_present(monkeypatch, value):
    monkeypatch.setitem(sys.modules, _NAME, types.ModuleType(_NAME))
    monkeypatch.setenv("COORDINATOR_NATIVE", value)
    assert native.load() is None


def test_env_read_at_call_time_and_disable_does_not_poison_cache(monkeypatch):
    fake = types.ModuleType(_NAME)
    monkeypatch.setitem(sys.modules, _NAME, fake)
    monkeypatch.setenv("COORDINATOR_NATIVE", "0")
    assert native.load() is None
    monkeypatch.delenv("COORDINATOR_NATIVE")
    assert native.load() is fake


def test_import_is_attempted_once(monkeypatch):
    calls = []

    def fake_import(name):
        calls.append(name)
        raise ImportError("nope")

    monkeypatch.setattr(native.importlib, "import_module", fake_import)
    assert native.load() is None
    assert native.load() is None
    assert calls == [_NAME]


def test_reset_allows_a_second_attempt(monkeypatch):
    monkeypatch.setitem(sys.modules, _NAME, None)
    assert native.load() is None
    fake = types.ModuleType(_NAME)
    monkeypatch.setitem(sys.modules, _NAME, fake)
    assert native.load() is None  # cached
    native._reset_for_tests()
    assert native.load() is fake


def test_non_import_error_becomes_none_and_logs_once_at_debug(monkeypatch, caplog):
    def boom(name):
        raise RuntimeError("ABI mismatch")

    monkeypatch.setattr(native.importlib, "import_module", boom)
    with caplog.at_level(logging.DEBUG, logger=native.__name__):
        assert native.load() is None
        assert native.load() is None
    records = [r for r in caplog.records if r.name == native.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert "ABI mismatch" in records[0].getMessage()
