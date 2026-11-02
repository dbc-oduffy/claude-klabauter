"""The warm engine raises its own priority at startup; pool workers are not raised."""

from __future__ import annotations

import inspect
from unittest import mock

from coordinator_core.warm import server


def test_boot_sequence_raises_priority_first():
    source = inspect.getsource(server._run_guarded)
    assert "_raise_engine_priority()" in source
    assert source.index("_raise_engine_priority()") < source.index("_engine_clone_root()")


def test_windows_sets_high_priority_class_on_own_process(monkeypatch):
    kernel32 = mock.MagicMock()
    kernel32.SetPriorityClass.return_value = 1
    kernel32.GetCurrentProcess.return_value = 123
    fake_ctypes = mock.MagicMock()
    fake_ctypes.windll.kernel32 = kernel32
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setitem(__import__("sys").modules, "ctypes", fake_ctypes)

    assert server._raise_engine_priority() is True
    assert server._WINDOWS_HIGH_PRIORITY_CLASS == 0x80
    kernel32.SetPriorityClass.assert_called_once_with(123, 0x80)


def test_priority_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "win32")
    fake_ctypes = mock.MagicMock()
    fake_ctypes.windll.kernel32.SetPriorityClass.side_effect = OSError
    monkeypatch.setitem(__import__("sys").modules, "ctypes", fake_ctypes)
    assert server._raise_engine_priority() is False


def test_pool_worker_init_never_raises_priority():
    assert "_raise_engine_priority" not in inspect.getsource(server._worker_process_init)
