"""A live warm server that predates the in-flight endpoint must not block every handover.

Its 404 reads as no count (its stop path drains in flight); any other failure still reads as in flight.
"""

from __future__ import annotations

import urllib.error

import pytest

from coordinator_core.warm import front_door, supervisor


def _raising(code: int):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)

    return opener


@pytest.fixture
def live_supervisor(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "read_discovery", lambda root: {"port": 1})
    monkeypatch.setattr(supervisor, "discovery_is_live", lambda rec: True)
    monkeypatch.setattr(supervisor, "listener_url", lambda rec: "http://127.0.0.1:1")
    monkeypatch.setattr(supervisor.cookie, "read", lambda root: "secret")
    return tmp_path


@pytest.fixture
def live_door(monkeypatch, tmp_path):
    monkeypatch.setattr(front_door, "read_discovery", lambda root: {"port": 1})
    monkeypatch.setattr(front_door, "discovery_is_live", lambda rec: True)
    monkeypatch.setattr(front_door.door_credential, "read_secret", lambda: "secret")
    return tmp_path


@pytest.mark.parametrize("code,expected", [(404, 0), (500, None), (403, None)])
def test_supervisor_probe_reads_404_as_no_count(live_supervisor, code, expected):
    assert supervisor.in_flight_count(live_supervisor, opener=_raising(code)) == expected


@pytest.mark.parametrize("code,expected", [(404, 0), (500, None), (403, None)])
def test_front_door_probe_reads_404_as_no_count(live_door, code, expected):
    assert front_door.in_flight_count(live_door, opener=_raising(code)) == expected
