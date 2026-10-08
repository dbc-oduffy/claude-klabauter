"""Tests for coordinator_core.session.suite_authority."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import env_locality
from coordinator_core.env_locality import Locality
from coordinator_core.session import grant, suite_authority as sa

CLOUD = Locality("cloud", "high", "machine", "t")
SUSPECT = Locality("suspect", "low", "machine", "t")
ATTENDED = Locality("attended", "high", "machine", "t")
REMOTE = {"CLAUDE_CODE_REMOTE": "true"}


def _rung(monkeypatch, loc):
    monkeypatch.setattr(env_locality, "machine_rung", lambda *a, **k: loc)


def test_cloud_marker_and_cloud_rung(monkeypatch):
    _rung(monkeypatch, CLOUD)
    assert isinstance(sa.cloud_box_basis(REMOTE), str)


@pytest.mark.parametrize("loc", [SUSPECT, ATTENDED])
def test_non_cloud_rung_is_none(monkeypatch, loc):
    _rung(monkeypatch, loc)
    assert sa.cloud_box_basis(REMOTE) is None


def test_empty_env_is_none(monkeypatch):
    _rung(monkeypatch, CLOUD)
    assert sa.cloud_box_basis({}) is None


def test_only_forwarded_marker_counts(monkeypatch):
    _rung(monkeypatch, CLOUD)
    env = {"CLAUDE_CODE_REMOTE_SESSION_ID": "cse_x",
           "CLAUDE_CODE_ENTRYPOINT": "remote_x"}
    assert sa.cloud_box_basis(env) is None


def test_windows_spoof_is_none(monkeypatch):
    monkeypatch.setattr(env_locality, "IS_WINDOWS", True)
    monkeypatch.setattr(env_locality, "IS_DARWIN", False)
    monkeypatch.setattr(env_locality, "IS_LINUX", False)
    monkeypatch.setattr(env_locality, "_MACHINE_CACHE", {})
    monkeypatch.setattr(env_locality, "_cpu_brand_windows",
                        lambda env: "Intel(R) Xeon(R) Gold 6130")
    env = dict(REMOTE, PROCESSOR_IDENTIFIER="Intel(R) Xeon(R) Gold 6130")
    assert sa.cloud_box_basis(env) is None


def test_machine_rung_called_without_caller_env(monkeypatch):
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return CLOUD

    monkeypatch.setattr(env_locality, "machine_rung", spy)
    sa.cloud_box_basis(dict(REMOTE, PROCESSOR_IDENTIFIER="forged"))
    assert calls == [((), {})]


def test_machine_rung_exception_reads_not_cloud(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(env_locality, "machine_rung", boom)
    assert sa.cloud_box_basis(REMOTE) is None


def test_suite_authority_cloud_no_grant(monkeypatch):
    _rung(monkeypatch, CLOUD)
    monkeypatch.setattr(grant, "check_tier_u_grant",
                        lambda *a, **k: (False, None))
    assert sa.suite_authority(env=REMOTE) == (True, "cloud-box", None)


def test_suite_authority_attended_forged_marker_no_grant(monkeypatch):
    _rung(monkeypatch, ATTENDED)
    monkeypatch.setattr(grant, "check_tier_u_grant",
                        lambda *a, **k: (False, None))
    assert sa.suite_authority(env=REMOTE) == (False, None, None)


def test_suite_authority_live_grant(monkeypatch):
    _rung(monkeypatch, ATTENDED)
    rec = {"granted_by": "pm", "note": "go"}
    monkeypatch.setattr(grant, "check_tier_u_grant",
                        lambda *a, **k: (True, rec))
    assert sa.suite_authority(env={}) == (True, "grant", rec)


def test_suite_authority_grant_exception_fails_closed(monkeypatch):
    _rung(monkeypatch, ATTENDED)

    def boom(*a, **k):
        raise OSError("x")

    monkeypatch.setattr(grant, "check_tier_u_grant", boom)
    assert sa.suite_authority(env={}) == (False, None, None)


def test_zero_spawns(monkeypatch):
    def no_spawn(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    _rung(monkeypatch, CLOUD)
    monkeypatch.setattr(grant, "check_tier_u_grant",
                        lambda *a, **k: (False, None))
    assert sa.suite_authority(env=REMOTE).authorized
    assert not sa.suite_authority(env={}).authorized
