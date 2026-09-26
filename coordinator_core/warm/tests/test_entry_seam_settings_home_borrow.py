"""`entry_seam.per_request_state`'s sixth axis: the `COORDINATOR_SETTINGS_HOME`
borrow (C1, docs/plans/2026-08-31-the-settings-home-crosses-the-warm-boundary.md).

Purpose: a resident warm server otherwise answers `settings_home()` from its
own process's ambient `COORDINATOR_SETTINGS_HOME` -- belonging to whoever
spawned the server, not the caller whose request it is currently serving.
This pins the same `isolated`-gated bind/pop/restore contract the five
pre-existing axes already carry (`test_entry_seam_env_borrow.py`), applied to
the sixth.

Negative-spec (RAG-bait):
    Does not exercise a live warm server, `_pool_dispatch_worker`, or the
    `_worker_process_init` verify-at-entry repair -- those are C2's / the
    `warm/server.py` suite's job. This file pins `per_request_state`'s own
    `os.environ` contract for the `settings_home` axis in isolation, against
    the real function, no stand-in.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.warm.entry_seam import per_request_state

_HOME_ABS = os.path.abspath("carried-settings-home")
_SPAWNER_HOME = os.path.abspath("spawner-settings-home")


def _set_spawner_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", _SPAWNER_HOME)


def test_carried_home_isolated_binds_the_callers_home(monkeypatch):
    _set_spawner_env(monkeypatch)

    with per_request_state(settings_home=_HOME_ABS, isolated=True):
        assert os.environ["COORDINATOR_SETTINGS_HOME"] == _HOME_ABS

    assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME


def test_restore_unwinds_even_when_the_block_raises(monkeypatch):
    _set_spawner_env(monkeypatch)
    before = dict(os.environ)

    with pytest.raises(RuntimeError):
        with per_request_state(settings_home=_HOME_ABS, isolated=True):
            assert os.environ["COORDINATOR_SETTINGS_HOME"] == _HOME_ABS
            raise RuntimeError("boom")

    assert dict(os.environ) == before


def test_not_isolated_leaves_os_environ_untouched(monkeypatch):
    _set_spawner_env(monkeypatch)
    before = dict(os.environ)

    with per_request_state(settings_home=_HOME_ABS, isolated=False):
        assert dict(os.environ) == before

    assert dict(os.environ) == before


@pytest.mark.parametrize("bad", ["", "relative/path", "not-a-home", "C:foo"])
def test_malformed_claim_pops_rather_than_binds(monkeypatch, bad):
    _set_spawner_env(monkeypatch)

    with per_request_state(settings_home=bad, isolated=True):
        assert "COORDINATOR_SETTINGS_HOME" not in os.environ

    assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME


@pytest.mark.parametrize("bad", ["", "relative/path", "not-a-home", "C:foo"])
def test_malformed_claim_emits_a_diagnostic(monkeypatch, bad):
    _set_spawner_env(monkeypatch)
    diagnostics: list = []

    with per_request_state(settings_home=bad, diagnostics=diagnostics, isolated=True):
        assert "COORDINATOR_SETTINGS_HOME" not in os.environ

    assert diagnostics, "malformed settings_home claim must emit a diagnostic"
    assert "COORDINATOR_SETTINGS_HOME" in diagnostics[0]


def test_absence_binds_nothing(monkeypatch):
    _set_spawner_env(monkeypatch)

    with per_request_state(isolated=True):
        assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME

    assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME


def test_absent_claim_after_a_carried_one_resolves_the_workers_own_home(monkeypatch):
    _set_spawner_env(monkeypatch)

    with per_request_state(settings_home=_HOME_ABS, isolated=True):
        assert os.environ["COORDINATOR_SETTINGS_HOME"] == _HOME_ABS

    with per_request_state(isolated=True):
        assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME
