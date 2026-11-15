"""Falsifier: pytest session hooks stay O(changes) against a 30k-file machine-local registry.

Times the root `drive_root_write_guard` start+finish pair plus the `coordinator_core/conftest.py`
engine-records tripwire snapshot pair, in process time, against a synthetic registry under tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.conftest import snapshot_engine_records

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUARD_PATH = _REPO_ROOT / "drive_root_write_guard.py"
_LIVE_RUN_INDEX = "coordinator-warp-live-run-index.json"

_TOTAL_FILES = 30_000
_KEY_DIRS = 330
_BUDGET_SECONDS = 0.200


def _build_registry(settings_home: Path) -> int:
    """Create `_TOTAL_FILES` files under machine-local; return how many were written."""
    registry = settings_home / "machine-local"
    registry.mkdir(parents=True)
    (registry / "registry.local.toml").write_text("[box]\nname = 'synthetic'\n", encoding="utf-8")
    written = 1
    receipts_per_key = (_TOTAL_FILES - written - _KEY_DIRS) // _KEY_DIRS + 1
    for k in range(_KEY_DIRS):
        key_dir = registry / "engine-records" / f"key{k:04d}"
        spent = key_dir / "coordinator-warp-run-tokens" / "spent"
        spent.mkdir(parents=True)
        runs = {f"run-{k}-{i}": {"state": "live"} for i in range(3)}
        (key_dir / _LIVE_RUN_INDEX).write_text(json.dumps({"runs": runs}), encoding="utf-8")
        written += 1
        for i in range(receipts_per_key):
            if written >= _TOTAL_FILES:
                return written
            (spent / f"receipt-{i:05d}.json").write_text('{"settled": true}', encoding="utf-8")
            written += 1
    return written


def _count_files(root: Path) -> int:
    return sum(len(files) for _dirpath, _dirs, files in os.walk(root))


def _load_guard():
    """A private copy of the guard module, so the live plugin's globals stay untouched."""
    spec = importlib.util.spec_from_file_location("drive_root_write_guard_budget_probe", _GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Audit hooks cannot be removed and the live plugin already installed one.
    module._installed = True
    return module


@pytest.fixture(scope="module")
def big_registry(tmp_path_factory):
    settings_home = tmp_path_factory.mktemp("settings")
    written = _build_registry(settings_home)
    assert written == _TOTAL_FILES
    return settings_home


def test_registry_fixture_is_30k_files(big_registry):
    assert _count_files(big_registry / "machine-local") == _TOTAL_FILES
    assert len(os.listdir(big_registry / "machine-local" / "engine-records")) == _KEY_DIRS


def test_session_hooks_stay_under_budget_at_30k_files(big_registry, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(big_registry))
    guard = _load_guard()
    session = SimpleNamespace(exitstatus=0, config=SimpleNamespace())

    t0 = time.process_time()
    guard.pytest_sessionstart(session)
    guard.pytest_sessionfinish(session, 0)
    before = snapshot_engine_records(str(big_registry))
    after = snapshot_engine_records(str(big_registry))
    elapsed = time.process_time() - t0

    # Non-vacuous: the guard watched the synthetic registry, and the tripwire read every key dir.
    assert guard._REGISTRY_DIR == os.path.normcase(os.path.normpath(os.path.realpath(big_registry / "machine-local")))
    assert len(before) == _KEY_DIRS and before == after
    print(f"registry session-hook process time at {_TOTAL_FILES} files: {elapsed * 1000:.1f} ms")
    assert elapsed < _BUDGET_SECONDS, (
        f"guard start+finish plus tripwire pair took {elapsed * 1000:.1f} ms process time "
        f"(budget {_BUDGET_SECONDS * 1000:.0f} ms) at {_TOTAL_FILES} files"
    )
