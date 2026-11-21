"""Mutating machine-local verbs hold a cross-process .registry-write.lock; readers do not."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_IMPL = _REPO / "coordinator" / "templates" / "bin" / "_machine_local.py"
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_impl():
    spec = importlib.util.spec_from_file_location("_ml_lock_under_test", _IMPL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mutating_set_is_the_documented_verb_list():
    mod = _load_impl()
    assert mod._MUTATING_COMMANDS == frozenset(
        {"set", "unset", "retire-publisher", "array-append", "array-set",
         "migrate-publish-mirrors", "relocate-store"}
    )
    assert not mod._MUTATING_COMMANDS & {"get", "has", "keys", "dump", "check", "path", "dir", "require"}


def test_lock_removed_on_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    mod = _load_impl()
    with pytest.raises(RuntimeError):
        with mod._registry_write_lock():
            assert (tmp_path / ".registry-write.lock").exists()
            raise RuntimeError("boom")
    assert not (tmp_path / ".registry-write.lock").exists()


def test_held_lock_times_out_with_clear_error_and_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    lock = tmp_path / ".registry-write.lock"
    lock.write_text("123", encoding="utf-8")
    mod = _load_impl()
    with pytest.raises(SystemExit) as exc:
        with mod._registry_write_lock(timeout_s=0.2):
            pytest.fail("lock acquired while held")
    assert "locked by another writer" in str(exc.value)
    assert lock.exists()


def test_stale_lock_older_than_60s_is_broken(tmp_path, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    lock = tmp_path / ".registry-write.lock"
    lock.write_text("123", encoding="utf-8")
    old = lock.stat().st_mtime - 120
    os.utime(lock, (old, old))
    mod = _load_impl()
    with mod._registry_write_lock(timeout_s=1.0):
        assert lock.exists()
    assert not lock.exists()


def test_unbreakable_stale_lock_still_times_out(tmp_path, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    lock = tmp_path / ".registry-write.lock"
    lock.write_text("123", encoding="utf-8")
    old = lock.stat().st_mtime - 120
    os.utime(lock, (old, old))
    mod = _load_impl()

    def _deny(_path):
        raise PermissionError("held open")

    monkeypatch.setattr(mod.os, "remove", _deny)
    with pytest.raises(SystemExit) as exc:
        with mod._registry_write_lock(timeout_s=0.2):
            pytest.fail("lock acquired while held")
    assert "locked by another writer" in str(exc.value)
