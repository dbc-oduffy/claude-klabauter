"""The caller's kill ceiling must clear the wait its own child will perform.

`warm/client.py` keeps reading the answer to a mutation it has already put on
the wire for `ipc.mutation_read_deadline_for(op)` -- a TRANSPORT deadline,
deliberately not clamped by the `ceremony.*` PERFORMANCE budget, because
abandoning a delivered commit converts a slowness report into an
unknown-whether-it-committed. That child runs under
`cc_invoke::_op_timeout_ceiling`. If the ceiling is sized off the performance
budget alone, the parent SIGKILLs the child at 2 + 2 = 4s and the
`WARM_DISPATCH_INDETERMINATE` envelope -- the one signal that tells an operator
a commit may have landed -- is unreachable on exactly the ops that commit.

This has now been the same defect twice by two different routes:

  - the client derived its wait from `ipc._timeout_for`, inheriting the 2s
    ceremony clamp, which equals `READ_DEADLINE_SECS`, so the extension
    computed `max(0.0, 2.0 - 2.0)` and waited zero seconds (2e3eb215ce);
  - dropping that clamp without publishing the transport number to the caller
    put the 30s wait back outside the 4s ceiling -- WARN dcf219af's finding
    again, reached from the other side.

So the pin is on the RELATIONSHIP, not on either number: whatever the engine
publishes, the ceiling clears the read deadline. Both numbers may move; they
may not cross.

Run: python3 -m pytest
coordinator/bin/tests/test_client_ceiling_clears_the_mutation_read_deadline.py -q
"""
from __future__ import annotations

import math
import sys
import unittest.mock
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_LIB_DIR = _TESTS_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)

from coordinator_core.invoke.__main__ import _dump_op_timeouts  # noqa: E402
from coordinator_core.warm.client import _mutation_deadline_for  # noqa: E402

#:     by `OP_KEY_SCOPE`, the dispatcher prefix-matches);
#:   - `commit.exec_bit_change` -- listed, NOT prefixed (`_CEREMONY_PACKAGE_ALIASES`),
_CEREMONY_OPS = (
    "ceremony.commit_v2",
    "ceremony.scoped_git_commit",
    "commit.exec_bit_change",
)


@pytest.fixture(autouse=True)
def _isolate_op_timeout_state():
    _mod._reset_op_timeout_cache()
    yield
    _mod._reset_op_timeout_cache()


def _ceiling_against_the_live_dump(op: str) -> int:
    payload = {k: float(v) for k, v in _dump_op_timeouts().items()}

    def _install(*_args, **_kwargs):
        _mod._OP_TIMEOUTS_STATE = "ok"
        _mod._OP_TIMEOUTS_MAP = payload

    with unittest.mock.patch.object(_mod, "_resolve_op_timeouts", _install):
        return _mod._op_timeout_ceiling(op, "/nonexistent", {})


def test_the_ceiling_clears_the_wait_its_child_will_actually_perform():
    for op in _CEREMONY_OPS:
        assert _ceiling_against_the_live_dump(op) >= _mutation_deadline_for(op)


def test_the_performance_budget_alone_would_not_have_cleared_it():
    payload = _dump_op_timeouts()
    for op in _CEREMONY_OPS:
        assert int(payload["__ceremony_budget__"]) + 2 < _mutation_deadline_for(op)


def test_the_engine_publishes_the_transport_deadline_at_all():
    payload = _dump_op_timeouts()
    assert payload["__ceremony_mutation_read_deadline__"] == _mutation_deadline_for(
        "ceremony.commit_v2"
    )
    assert payload["__ceremony_mutation_read_deadline__"] > payload["__ceremony_budget__"]


def test_an_engine_that_does_not_publish_it_degrades_to_the_budget():
    payload = {
        k: float(v)
        for k, v in _dump_op_timeouts().items()
        if not k.startswith("__ceremony__")
    }
    payload.pop("__ceremony_mutation_read_deadline__")
    payload.pop("__warm_miss_wait__")

    def _install(*_args, **_kwargs):
        _mod._OP_TIMEOUTS_STATE = "ok"
        _mod._OP_TIMEOUTS_MAP = payload

    with unittest.mock.patch.object(_mod, "_resolve_op_timeouts", _install):
        assert _mod._op_timeout_ceiling("ceremony.commit_v2", "/nonexistent", {}) == int(
            payload["ceremony.commit_v2"]
        ) + 2


def test_a_non_ceremony_op_is_untouched():
    payload = {k: float(v) for k, v in _dump_op_timeouts().items()}
    assert _ceiling_against_the_live_dump("session.boot_sweep") == int(
        payload.get("session.boot_sweep", payload["__default__"])
    ) + math.ceil(payload["__warm_miss_wait__"]) + 2


def test_an_unlisted_ceremony_op_is_bounded_by_the_ceremony_budget_not_the_default():
    """`OP_KEY_SCOPE` drives the dump's projection; `ipc._timeout_for`
    prefix-matches. So a `ceremony.*` op the dump omits is clamped to 2s
    server-side while its row is absent here -- and the caller used to fall
    through to `__default__`, 15x the budget the engine will actually allow.
    `__ceremony_budget__` is published precisely to be that bound, and until
    now nothing read it for anything but the remedy text."""
    payload = {k: float(v) for k, v in _dump_op_timeouts().items()}
    unlisted = "ceremony.scoped_git_commit"
    assert unlisted not in payload, "fixture stale: this op is now listed by name"

    def _install(*_args, **_kwargs):
        _mod._OP_TIMEOUTS_STATE = "ok"
        _mod._OP_TIMEOUTS_MAP = dict(payload)

    stripped = dict(payload)
    stripped.pop("__ceremony_mutation_read_deadline__")
    stripped.pop("__warm_miss_wait__")

    def _install_stripped(*_args, **_kwargs):
        _mod._OP_TIMEOUTS_STATE = "ok"
        _mod._OP_TIMEOUTS_MAP = stripped

    with unittest.mock.patch.object(_mod, "_resolve_op_timeouts", _install_stripped):
        assert _mod._op_timeout_ceiling(unlisted, "/nonexistent", {}) == int(
            payload["__ceremony_budget__"]
        ) + 2

    with unittest.mock.patch.object(_mod, "_resolve_op_timeouts", _install):
        assert _mod._op_timeout_ceiling(unlisted, "/nonexistent", {}) >= _mutation_deadline_for(
            unlisted
        )


def test_the_client_learns_alias_membership_from_the_engine():
    """`commit.exec_bit_change` and `review.snapshot_diff_and_head` are ceremony
    ops with no `ceremony.` prefix (`ipc._CEREMONY_PACKAGE_ALIASES`).
    `cc_invoke` cannot import `ipc` to find that out, so the engine says so with
    a `__ceremony__<op>` row and the client reads it. Without that, both were
    sized as ordinary ops -- and one of them commits."""
    payload = {k: float(v) for k, v in _dump_op_timeouts().items()}

    def _install(*_args, **_kwargs):
        _mod._OP_TIMEOUTS_STATE = "ok"
        _mod._OP_TIMEOUTS_MAP = payload

    for alias in ("commit.exec_bit_change", "review.snapshot_diff_and_head"):
        assert not alias.startswith("ceremony.")
        with unittest.mock.patch.object(_mod, "_resolve_op_timeouts", _install):
            _mod._reset_op_timeout_cache()
            _mod._resolve_op_timeouts("/nonexistent", {}, 1)
            assert _mod._is_ceremony_op(alias), (
                "the engine published this op as ceremony and the client ignored it"
            )
            assert _mod._op_timeout_ceiling(
                alias, "/nonexistent", {}
            ) >= _mutation_deadline_for(alias)


def test_membership_falls_back_to_the_prefix_when_no_dump_is_available():
    _mod._reset_op_timeout_cache()
    assert _mod._is_ceremony_op("ceremony.commit_v2")
    assert not _mod._is_ceremony_op("session.boot_sweep")


_ORDINARY_MUTATION = "queue.append"


def test_the_ceiling_clears_a_warm_miss_plus_the_read():
    from coordinator_core.invoke.__main__ import _warm_boot_wait_deadline
    from coordinator_core.warm.client import READ_DEADLINE_SECS

    boot = _warm_boot_wait_deadline()
    assert boot > 0, "fixture stale: the boot wait is off, so this pins nothing"
    for op in (*_CEREMONY_OPS, _ORDINARY_MUTATION):
        assert _ceiling_against_the_live_dump(op) >= (
            READ_DEADLINE_SECS + boot + _mutation_deadline_for(op)
        ), op


def test_the_published_miss_wait_follows_the_childs_own_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0")
    assert _dump_op_timeouts()["__warm_miss_wait__"] == 0.0

