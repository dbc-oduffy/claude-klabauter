"""A stale `.git/index.lock` in a destination refuses the publish before staging."""

import importlib.util
import os
import sys
import time
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_stale_index_lock_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def test_no_lock_passes(tmp_path):
    (tmp_path / ".git").mkdir()
    assert publish._stale_index_lock(tmp_path) is None


def test_fresh_lock_passes(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index.lock").write_bytes(b"")
    assert publish._stale_index_lock(tmp_path) is None


def test_old_lock_refuses_naming_the_lock(tmp_path):
    (tmp_path / ".git").mkdir()
    lock = tmp_path / ".git" / "index.lock"
    lock.write_bytes(b"")
    old = time.time() - publish._STALE_INDEX_LOCK_SECS - 60
    os.utime(lock, (old, old))
    refusal = publish._stale_index_lock(tmp_path)
    assert refusal is not None and str(lock) in refusal
