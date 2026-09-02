"""A name arriving in the envelope-level `_env` object reaches a warm-SERVED
op's `os.environ` for that one request only, then is restored -- the
consuming half of C4 (docs/plans/2026-09-01-the-warm-door-forwards-a-
declared-env-set.md), exercised against the real seam
(`coordinator_core.warm.entry_seam`), no live warm-server round trip.

WHY THIS FILE IS NOT `test_door_stamps_declared_env_set.py`. That file
proves the PRODUCER half: the shipped `door.exe` binary emits the `_env`
wire object with the right names/values (`test_door_stamps_declared_env_
set.py`'s own module docstring names this split explicitly: "this file
does not assert that a served op resolves any name out of `_env` -- that
is `test_declared_env_served_per_request.py`'s job"). This file starts
from an ALREADY-RESOLVED `env` mapping (what `warm.caller_context.merge_
env_axis` would have produced from either wire shape) and proves the
CONSUMING half: `entry_seam._environ_identity_borrow`'s three named
branches (borrow/refuse/override) each land their own declared name in
`os.environ` for the block's duration and restore it afterward, and
`isolated=False` never touches `os.environ` at all.

Negative spec (RAG-bait): this suite does NOT start, stop, or restart the
warm server, does NOT open a pipe/socket, does NOT invoke `door.exe`, and
does NOT duplicate `test_door_stamps_declared_env_set.py`'s wire-shape
assertions -- ~50 concurrent sessions share the resident engine, and this
file calls `entry_seam`'s functions directly, in-process, exactly as any
other unit test does.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.warm import entry_seam
from coordinator_core.warm.entry_seam import (
    BORROW_NAMES,
    OVERRIDE_NAMES,
    REFUSE_NAMES,
    _environ_identity_borrow,
)

_BORROW_NAME = BORROW_NAMES[0]  # "MACHINE_LOCAL_REGISTRY_DIR"
_REFUSE_NAME = REFUSE_NAMES[0]  # "COORDINATOR_SETTINGS_HOME"
_OVERRIDE_TOP = OVERRIDE_NAMES[0]  # "COORDINATOR_SESSION_ID"
_VALID_UUID = "8b40d62c-55ef-4702-83ce-0cd8dc6513e3"

#: Every name any branch below might write, so a fixture can snapshot/
#: restore around each test regardless of which branch it exercises --
#: this file's own safety net, independent of the seam's `finally` under
#: test, so an assertion failure mid-test cannot leak a borrowed value
#: into a sibling test on this shared-process test run.
_ALL_TOUCHED_NAMES = tuple(dict.fromkeys(BORROW_NAMES + REFUSE_NAMES + OVERRIDE_NAMES + ("CLAUDE_PID",)))


@pytest.fixture(autouse=True)
def _restore_touched_env_names():
    """Snapshot every name this file's branches can write, restore after
    each test -- belt-and-suspenders around the seam's own `finally`
    restore, which is itself under test here and must not be trusted
    blindly by the harness proving it."""
    saved = {name: os.environ.get(name) for name in _ALL_TOUCHED_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_borrow_mode_lands_the_declared_name_for_the_block_only():
    """BORROW branch: a name outside the refuse/override triple (e.g.
    `MACHINE_LOCAL_REGISTRY_DIR`) is mirrored into `os.environ` for the
    life of the block under `isolated=True`, and gone afterward."""
    os.environ.pop(_BORROW_NAME, None)
    borrowed_value = "some\\registry\\dir"  # abs-path-ok: synthetic fixture value, never resolved
    env = {_BORROW_NAME: borrowed_value}

    with _environ_identity_borrow(env, isolated=True, caller_pid=None):
        assert os.environ[_BORROW_NAME] == borrowed_value

    assert _BORROW_NAME not in os.environ


def test_borrow_mode_restores_a_prior_ambient_value_not_just_pops():
    """The restore is a snapshot-and-replay, not a blanket pop: a name that
    already had an ambient value before the block must have THAT value
    back, not be left absent."""
    os.environ[_BORROW_NAME] = "prior-ambient-value"
    env = {_BORROW_NAME: "borrowed-value"}

    with _environ_identity_borrow(env, isolated=True, caller_pid=None):
        assert os.environ[_BORROW_NAME] == "borrowed-value"

    assert os.environ[_BORROW_NAME] == "prior-ambient-value"


def test_refuse_mode_mirrors_settings_home_for_isolated_dispatch_only():
    """REFUSE branch: `COORDINATOR_SETTINGS_HOME` is mirrored (not refused
    here -- the refusal itself is one layer up, in `warm.server._run_
    dispatch`) when it is a non-empty absolute path, and popped after."""
    os.environ.pop(_REFUSE_NAME, None)
    named_home = os.path.abspath("some-settings-home")
    env = {_REFUSE_NAME: named_home}

    with _environ_identity_borrow(env, isolated=True, caller_pid=None):
        assert os.environ[_REFUSE_NAME] == named_home

    assert _REFUSE_NAME not in os.environ


def test_refuse_mode_absent_from_env_inherits_the_ambient_value():
    """Absence from the resolved `env` mapping is inherit-on-absent for
    REFUSE/BORROW -- untouched, not popped: a request that carried no claim
    has no opinion on this name."""
    os.environ[_REFUSE_NAME] = "the-workers-own-value"

    with _environ_identity_borrow({}, isolated=True, caller_pid=None):
        assert os.environ[_REFUSE_NAME] == "the-workers-own-value"

    assert os.environ[_REFUSE_NAME] == "the-workers-own-value"


def test_override_mode_binds_the_top_tier_session_id_and_pops_the_rest():
    """OVERRIDE branch: a UUID-shaped candidate on the top-tier declared
    name is bound, and every other name in the session-id precedence triple
    is popped -- matching the pre-C4 `session_env_precedence` behaviour."""
    for name in OVERRIDE_NAMES:
        os.environ[name] = "stale-ambient-value"
    env = {_OVERRIDE_TOP: _VALID_UUID}

    with _environ_identity_borrow(env, isolated=True, caller_pid=None):
        assert os.environ[_OVERRIDE_TOP] == _VALID_UUID
        for name in OVERRIDE_NAMES[1:]:
            assert name not in os.environ

    for name in OVERRIDE_NAMES:
        assert os.environ[name] == "stale-ambient-value"


def test_override_mode_rejects_a_non_uuid_candidate():
    """A carried value that fails the UUID shape gate is treated as no
    carried identity at all -- every override-triple name is popped, never
    a malformed value trusted onto the wire."""
    for name in OVERRIDE_NAMES:
        os.environ[name] = "stale-ambient-value"
    env = {_OVERRIDE_TOP: "not-a-uuid"}

    with _environ_identity_borrow(env, isolated=True, caller_pid=None):
        for name in OVERRIDE_NAMES:
            assert name not in os.environ

    for name in OVERRIDE_NAMES:
        assert os.environ[name] == "stale-ambient-value"


def test_caller_pid_is_bound_only_when_isolated():
    """`caller_pid`, when a decimal digit string, is mirrored to
    `CLAUDE_PID` for the block's duration -- the fourth, non-`FORWARDING_
    SET` axis `_environ_identity_borrow` also carries."""
    os.environ.pop("CLAUDE_PID", None)

    with _environ_identity_borrow({}, isolated=True, caller_pid="4242"):
        assert os.environ["CLAUDE_PID"] == "4242"

    assert "CLAUDE_PID" not in os.environ


def test_isolated_false_is_a_complete_noop_on_os_environ():
    """`isolated=False` never reads or writes `os.environ` at all -- not a
    borrow-then-immediately-restore, a true no-op, which is what keeps an
    unisolated (`BrokenProcessPool`-degrade) dispatch from mutating state
    every other in-flight connection in this same process also observes."""
    os.environ.pop(_BORROW_NAME, None)
    os.environ.pop(_REFUSE_NAME, None)
    for name in OVERRIDE_NAMES:
        os.environ.pop(name, None)
    env = {
        _BORROW_NAME: "would-be-borrowed",
        _REFUSE_NAME: os.path.abspath("would-be-mirrored"),
        _OVERRIDE_TOP: _VALID_UUID,
    }
    before = dict(os.environ)

    with _environ_identity_borrow(env, isolated=False, caller_pid="4242"):
        assert dict(os.environ) == before
        assert _BORROW_NAME not in os.environ
        assert _REFUSE_NAME not in os.environ
        assert _OVERRIDE_TOP not in os.environ

    assert dict(os.environ) == before


def test_restored_even_when_the_block_raises():
    """The restore is a `finally`, not a happy-path-only cleanup -- an
    exception inside the block must not leak a borrowed value into
    whichever request runs next in this process."""
    os.environ.pop(_BORROW_NAME, None)
    env = {_BORROW_NAME: "borrowed-value"}

    with pytest.raises(RuntimeError):
        with _environ_identity_borrow(env, isolated=True, caller_pid=None):
            assert os.environ[_BORROW_NAME] == "borrowed-value"
            raise RuntimeError("op failed mid-dispatch")

    assert _BORROW_NAME not in os.environ


def test_per_request_state_threads_env_to_the_served_op_and_back():
    """One level up: `entry_seam.per_request_state`, the actual seam a
    served op's request runs inside (`warm.server`'s two production call
    sites), lands the same borrowed value and restores it -- proving the
    consuming half end-to-end from the seam a served op sees, not merely
    from the private borrow helper directly."""
    os.environ.pop(_BORROW_NAME, None)
    env = {_BORROW_NAME: "reached-the-served-op"}

    with entry_seam.per_request_state(env=env, isolated=True) as _declared:
        assert os.environ[_BORROW_NAME] == "reached-the-served-op"

    assert _BORROW_NAME not in os.environ


def test_per_request_state_isolated_false_never_touches_os_environ():
    """Same seam, `isolated=False`: the served op runs, but `os.environ` is
    never mutated for it -- the accept-thread / cold-path disposition."""
    os.environ.pop(_BORROW_NAME, None)
    env = {_BORROW_NAME: "should-not-land"}

    with entry_seam.per_request_state(env=env, isolated=False) as _declared:
        assert _BORROW_NAME not in os.environ

    assert _BORROW_NAME not in os.environ
