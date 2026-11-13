"""`_mutate_register` retries a lock timeout inside a bounded budget and refuses when it persists."""

from __future__ import annotations

import time

import pytest
import yaml

from coordinator_core import locked_write
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

REL = "state/sizings/2026-10-01-retry.yaml"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    doc = {"schema": "sizing-object", "name": "retry", "intent": "exercise retry", "status": "sized"}
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return tmp_path


def _fake_rmw(monkeypatch, fail_times):
    calls = {"n": 0}
    real = locked_write.locked_rmw

    def fake(path, mutate, **kw):
        calls["n"] += 1
        if fail_times is None or calls["n"] <= fail_times:
            raise locked_write.LockTimeout("Could not open coordinator lock sidecar (filesystem stalled)")
        return real(path, mutate, **kw)

    monkeypatch.setattr(locked_write, "locked_rmw", fake)
    return calls


def test_stall_once_then_clear_proceeds(repo, monkeypatch):
    calls = _fake_rmw(monkeypatch, fail_times=1)
    rows = rl._mutate_register(repo, REL, lambda rows: rows)
    assert rows == []
    assert calls["n"] == 2


def test_persistent_stall_refuses_within_budget(repo, monkeypatch):
    monkeypatch.setattr(rl, "SIDECAR_RETRY_BUDGET_SECS", 0.3)
    calls = _fake_rmw(monkeypatch, fail_times=None)
    started = time.monotonic()
    with pytest.raises(rl.LedgerRefused, match="timed out waiting for the lock"):
        rl._mutate_register(repo, REL, lambda rows: rows)
    assert time.monotonic() - started < 2.0
    assert calls["n"] >= 2
