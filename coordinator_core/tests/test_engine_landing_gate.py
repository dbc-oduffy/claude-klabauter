"""The package-init landing gate: parks while a swap is live at its own root, never raises."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import coordinator_core
from coordinator_core import _engine_landing


def _run_init_at(root: Path) -> None:
    """Execute coordinator_core/__init__.py's source as if it lived at <root>/coordinator_core."""
    pkg = root / "coordinator_core"
    pkg.mkdir(exist_ok=True)
    fake = pkg / "__init__.py"
    fake.write_text(Path(coordinator_core.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("coordinator_core_gate_probe", fake)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def _engine_root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _raise_marker(root: Path, deadline_in: float) -> Path:
    marker = root / ".git" / _engine_landing.MARKER_NAME
    marker.write_text(json.dumps({"deadline_epoch": time.time() + deadline_in}), encoding="utf-8")
    return marker


def test_no_marker_costs_one_stat(tmp_path, monkeypatch):
    root = _engine_root(tmp_path)
    marker_str = os.path.join(str(root), ".git", _engine_landing.MARKER_NAME)
    real_stat = os.stat
    hits = []

    def counting(path, *a, **kw):
        if os.fspath(path) == marker_str:
            hits.append(path)
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(os, "stat", counting)
    _run_init_at(root)
    assert len(hits) == 1


def test_parks_then_proceeds_when_marker_removed(tmp_path):
    root = _engine_root(tmp_path)
    marker = _raise_marker(root, deadline_in=30)
    threading.Timer(0.2, marker.unlink).start()
    start = time.monotonic()
    _run_init_at(root)
    assert time.monotonic() - start >= 0.15
    assert not marker.exists()


def test_stale_marker_does_not_park(tmp_path):
    root = _engine_root(tmp_path)
    _raise_marker(root, deadline_in=-5)
    start = time.monotonic()
    _run_init_at(root)
    assert time.monotonic() - start < _engine_landing.WAIT_BUDGET_S


def test_gate_exception_is_swallowed(tmp_path, monkeypatch):
    root = _engine_root(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("gate failure")

    monkeypatch.setattr(_engine_landing, "wait_until_clear", boom)
    _run_init_at(root)
