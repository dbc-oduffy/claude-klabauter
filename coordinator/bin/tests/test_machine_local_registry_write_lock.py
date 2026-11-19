"""Mutating machine-local verbs hold a cross-process .registry-write.lock; readers do not."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_IMPL = _REPO / "coordinator" / "templates" / "bin" / "_machine_local.py"
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _env(sandbox: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("REPO_")}
    env["MACHINE_LOCAL_REGISTRY_DIR"] = str(sandbox)
    return env


def _run(sandbox: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_IMPL), *args],
        env=_env(sandbox), capture_output=True, text=True, timeout=60,
        creationflags=_NO_CONSOLE,
    )


def _load_impl():
    spec = importlib.util.spec_from_file_location("_ml_lock_under_test", _IMPL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.spawns_process
def test_parallel_sets_of_distinct_keys_all_survive(tmp_path):
    (tmp_path / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    keys = [f"repos.par{i}" for i in range(6)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda k: _run(tmp_path, "set", k, f"/x/{k}"), keys))

    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    content = (tmp_path / "registry.local.toml").read_text(encoding="utf-8")
    assert [k for k in keys if f'"{k}"' not in content] == []
    assert not (tmp_path / ".registry-write.lock").exists()


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


@pytest.mark.spawns_process
def test_read_only_verb_ignores_a_held_lock(tmp_path):
    (tmp_path / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    (tmp_path / ".registry-write.lock").write_text("123", encoding="utf-8")
    result = _run(tmp_path, "has", "repos.nope")
    assert result.returncode == 1, result.stderr
    assert (tmp_path / ".registry-write.lock").exists()
