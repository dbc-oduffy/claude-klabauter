"""Logged tracebacks open no source file."""
import linecache
import logging

from coordinator_core import source_free_traceback
import pytest


def test_logged_exception_never_touches_linecache(monkeypatch: pytest.MonkeyPatch) -> None:
    source_free_traceback.install()
    monkeypatch.setattr(linecache, "getline", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened source")))
    try:
        raise ValueError("boom")
    except ValueError:
        text = logging.Formatter().formatException(__import__("sys").exc_info())
    assert "ValueError: boom" in text and "test_source_free_traceback.py" in text
