"""A headless worker that dies inside the liveness window is reported exited in the submit reply."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from coordinator_core.install.engine_root_for_install import InstallEngineRoot
from coordinator_core.ops.workflow_fire import headless
from coordinator_core.ops.workflow_fire.tests.test_headless_published_root import _isolate


def _start(tmp_path, monkeypatch, code):
    source, registry = _isolate(tmp_path, monkeypatch, InstallEngineRoot('published', tmp_path / 'published', None))
    real_popen = headless.subprocess.Popen
    monkeypatch.setattr(headless, '_pid_birth', lambda pid: None)
    monkeypatch.setattr(headless.fire, '_LIVENESS_POLL_INTERVAL_S', 0.02)
    monkeypatch.setattr(headless.fire, '_LIVENESS_POLL_ATTEMPTS', 150)

    def popen(command, **kwargs):
        return real_popen([sys.executable, '-c', code], **kwargs)

    monkeypatch.setattr(headless.subprocess, 'Popen', popen)
    return headless.start(str(source)), registry


def test_worker_that_exits_at_once_is_reported_exited(tmp_path, monkeypatch):
    record, registry = _start(tmp_path, monkeypatch, 'print("boom: unstamped_root"); raise SystemExit(1)')
    assert record['state'] == 'exited'
    assert record['exit_code'] == 1
    assert 'boom: unstamped_root' in record['startup_log_tail']
    on_disk = json.loads(next(registry.glob('*.json')).read_text())
    assert on_disk['state'] == 'exited' and on_disk['exit_code'] == 1


def test_worker_that_outlives_the_window_stays_running(tmp_path, monkeypatch):
    source, registry = _isolate(tmp_path, monkeypatch, InstallEngineRoot('published', tmp_path / 'published', None))
    monkeypatch.setattr(headless, '_pid_birth', lambda pid: None)
    monkeypatch.setattr(headless.fire, '_LIVENESS_POLL_INTERVAL_S', 0.01)
    monkeypatch.setattr(headless.fire, '_LIVENESS_POLL_ATTEMPTS', 3)
    real_popen = headless.subprocess.Popen
    children = []

    def popen(command, **kwargs):
        children.append(real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs))
        return children[-1]

    monkeypatch.setattr(headless.subprocess, 'Popen', popen)
    try:
        record = headless.start(str(source))
        assert record['state'] == 'running'
        assert 'exit_code' not in record and 'startup_log_tail' not in record
    finally:
        for child in children:
            child.kill()
            child.wait()
