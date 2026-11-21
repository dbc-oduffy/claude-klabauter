"""Apply-path tests for klabauter freshness over tmp_path bare-origin + clone git fixtures."""

import os
from pathlib import Path

import pytest

from coordinator_core.install import klabauter_freshness as kf
from coordinator_core.install import klabauter_freshness_apply as apply

pytestmark = pytest.mark.complete

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _legs(monkeypatch, record_pid, sup, door, own_pid=4242):
    from coordinator_core.warm import supervisor

    monkeypatch.setattr(supervisor, "read_discovery", lambda root=None: {"pid": record_pid})
    return apply.engine_leg_in_flight(
        Path("."), sup_count=lambda r: sup, door_count=lambda r: door, own_pid=own_pid
    )


def test_engine_leg_gate_discounts_own_request_when_served_by_the_supervisor(monkeypatch):
    assert _legs(monkeypatch, 4242, 1, 1) == 0
    assert _legs(monkeypatch, 4242, 1, 0) == 0
    assert _legs(monkeypatch, 4242, 2, 1) == 1


def test_engine_leg_gate_counts_every_request_when_not_served_by_the_supervisor(monkeypatch):
    assert _legs(monkeypatch, 1, 1, 0) == 1
    assert _legs(monkeypatch, 1, 0, 0) == 0


def test_engine_leg_gate_unanswerable_probe_reads_in_flight(monkeypatch):
    assert _legs(monkeypatch, 4242, None, 0) == 1
    assert _legs(monkeypatch, 4242, 1, None) == 1


def test_non_checkout_reports_without_raising(tmp_path):
    res = apply.apply_freshness(tmp_path, tmp_path, hostname="boxy", live_runs=lambda: 0, installers={})
    assert res.action == kf.ACTION_CURRENT and res.reason
