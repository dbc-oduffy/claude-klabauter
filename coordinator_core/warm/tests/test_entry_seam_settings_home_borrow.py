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

# Platform-absolute per `os.path.isabs`, the same gate the production code
# checks against -- never a hardcoded platform-specific literal, so this
# suite behaves identically under POSIX and Windows interpreters alike.
_HOME_ABS = os.path.abspath("carried-settings-home")  # abs-path-ok: cwd-relative synthetic fixture, never resolved
_SPAWNER_HOME = os.path.abspath("spawner-settings-home")  # abs-path-ok: cwd-relative synthetic fixture, never resolved


def _set_spawner_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", _SPAWNER_HOME)


def test_carried_home_isolated_binds_the_callers_home(monkeypatch):
    """(a) Inside the block under isolated=True, the borrowed home is what
    `settings_home()` resolves to."""
    _set_spawner_env(monkeypatch)

    with per_request_state(settings_home=_HOME_ABS, isolated=True):
        assert os.environ["COORDINATOR_SETTINGS_HOME"] == _HOME_ABS

    assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME


def test_restore_unwinds_even_when_the_block_raises(monkeypatch):
    """(b) After the block, os.environ is byte-identical to before, including
    the case where the block raises."""
    _set_spawner_env(monkeypatch)
    before = dict(os.environ)

    with pytest.raises(RuntimeError):
        with per_request_state(settings_home=_HOME_ABS, isolated=True):
            assert os.environ["COORDINATOR_SETTINGS_HOME"] == _HOME_ABS
            raise RuntimeError("boom")

    assert dict(os.environ) == before


def test_not_isolated_leaves_os_environ_untouched(monkeypatch):
    """(c) Under isolated=False the variable is untouched -- shared with
    every other in-flight connection on that leg."""
    _set_spawner_env(monkeypatch)
    before = dict(os.environ)

    with per_request_state(settings_home=_HOME_ABS, isolated=False):
        assert dict(os.environ) == before

    assert dict(os.environ) == before


@pytest.mark.parametrize("bad", ["", "relative/path", "not-a-home", "C:foo"])
def test_malformed_claim_pops_rather_than_binds(monkeypatch, bad):
    """(d) A malformed claim (empty, relative) pops rather than binds --
    never mirrored into os.environ where every ambient settings_home()
    reader downstream would trust it."""
    _set_spawner_env(monkeypatch)

    with per_request_state(settings_home=bad, isolated=True):
        assert "COORDINATOR_SETTINGS_HOME" not in os.environ

    assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME


def test_absence_binds_nothing(monkeypatch):
    """(e) Absence binds nothing -- inherit-on-absent, matching this seam's
    other axes: a request that carried no claim leaves the enclosing scope's
    value untouched."""
    _set_spawner_env(monkeypatch)

    with per_request_state(isolated=True):
        assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME

    assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME


def test_absent_claim_after_a_carried_one_resolves_the_workers_own_home(monkeypatch):
    """(f) A request carrying NO claim, run immediately after one that did on
    the same worker, resolves the worker's own pristine home -- the
    inherit-on-absent + failed-restore composition (b) and C2's leakage leg
    do not jointly cover, since both of those pin a SINGLE request's own
    round trip rather than a second request's view of what the first left
    behind."""
    _set_spawner_env(monkeypatch)

    with per_request_state(settings_home=_HOME_ABS, isolated=True):
        assert os.environ["COORDINATOR_SETTINGS_HOME"] == _HOME_ABS

    with per_request_state(isolated=True):
        assert os.environ["COORDINATOR_SETTINGS_HOME"] == _SPAWNER_HOME
