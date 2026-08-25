"""Tests for `coordinator_core.ops.session.guard_roster_ops.list_ported_advisory_ops`
-- the eager, exhaustive listing seam over the six advisory hook ops ported
from the `~/.claude` advisory/nudge command hooks.

Negative spec: this set is NOT `postuse-advisory-dispatch.py`'s carrier
membership (only one of the six is carrier-delivered -- see the module
docstring and DR-297). Nothing here asserts carrier delivery, and no test
added later should.

Purpose: `coordinator_core.ipc::_REGISTRY` only reflects whatever has been
imported so far in this process; since op registration is lazy, a
naive read of it is silently PARTIAL. This file pins that `list_ported_advisory_ops`
stays exhaustive regardless (the whole reason the module exists),
and that a resolution failure raises `AdvisoryRosterUnavailable` rather than
degrading to a short list.

Spec backlink: pln-guard-roster-export-minus-the-a4dec3, chunk C4 (AC5).
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys

import pytest

import coordinator_core.ops.session.guard_roster_ops as guard_roster_ops
from coordinator_core.ops.session.guard_roster_ops import (
    AdvisoryOpEntry,
    AdvisoryRosterUnavailable,
    _PORTED_ADVISORY_HOOK_OP_NAMES,
    list_ported_advisory_ops,
)

# `test_list_ported_advisory_ops_is_exhaustive_under_lazy_ops_in_a_fresh_interpreter`
# spawns a real `sys.executable -c` fresh interpreter because the
# exhaustiveness property -- that `list_ported_advisory_ops` still resolves
# all six ops when `_REGISTRY` starts empty -- only exists in a process with
# no prior op imports, which no same-process mock can reproduce. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this
# file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def test_list_ported_advisory_ops_takes_no_arguments():
    sig = inspect.signature(list_ported_advisory_ops)
    assert list(sig.parameters) == [], (
        "list_ported_advisory_ops() must take no arguments -- found parameters: %r"
        % list(sig.parameters)
    )


def test_list_ported_advisory_ops_returns_all_six_ops_as_plain_data():
    entries = list_ported_advisory_ops()
    assert isinstance(entries, tuple)

    ids = [entry.id for entry in entries]
    assert set(ids) == set(_PORTED_ADVISORY_HOOK_OP_NAMES), (
        "list_ported_advisory_ops() did not return exactly the six named advisory "
        "ops -- expected %r, got %r" % (sorted(_PORTED_ADVISORY_HOOK_OP_NAMES), sorted(ids))
    )
    assert len(ids) == len(set(ids)), "duplicate op id in result: %r" % ids

    for entry in entries:
        assert isinstance(entry, AdvisoryOpEntry)
        assert entry.id
        assert entry.module
        assert entry.qualname

    # Plain-data check: JSON-serialisable after a trivial coercion.
    plain = [
        {"id": e.id, "module": e.module, "qualname": e.qualname} for e in entries
    ]
    json.dumps(plain)


def test_list_ported_advisory_ops_is_exhaustive_under_lazy_ops_in_a_fresh_interpreter():
    """The same result set in a FRESH interpreter, where `_REGISTRY` starts
    empty -- this is the whole reason the module exists, since a naive
    `_REGISTRY` read would be partial there."""
    probe = (
        "from coordinator_core.ops.session.guard_roster_ops import ("
        "list_ported_advisory_ops, _PORTED_ADVISORY_HOOK_OP_NAMES)\n"
        "entries = list_ported_advisory_ops()\n"
        "ids = sorted(e.id for e in entries)\n"
        "expected = sorted(_PORTED_ADVISORY_HOOK_OP_NAMES)\n"
        "assert ids == expected, (ids, expected)\n"
        "print('OK')\n"
    )
    import os

    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        "lazy-ops fresh-interpreter exhaustiveness probe failed:\n"
        "stdout=%s\nstderr=%s" % (result.stdout, result.stderr)
    )
    assert "OK" in result.stdout


def test_unresolvable_advisory_op_raises_rather_than_truncating(monkeypatch):
    """A caller must be able to tell "no advisory ops" from "eager
    resolution failed" -- simulate by monkeypatching `get_op_handler` to
    return None for one name, never by breaking the real registry."""
    from coordinator_core import ipc as _ipc

    real_get_op_handler = _ipc.get_op_handler
    missing_name = _PORTED_ADVISORY_HOOK_OP_NAMES[0]

    def _fake_get_op_handler(name):
        if name == missing_name:
            return None
        return real_get_op_handler(name)

    monkeypatch.setattr(_ipc, "get_op_handler", _fake_get_op_handler)

    try:
        list_ported_advisory_ops()
    except AdvisoryRosterUnavailable as exc:
        assert missing_name in str(exc)
    else:
        raise AssertionError(
            "list_ported_advisory_ops() did not raise AdvisoryRosterUnavailable "
            "when %r was made unresolvable -- it must never degrade to a "
            "short list" % missing_name
        )


def test_eager_import_failure_raises_advisory_roster_unavailable(monkeypatch):
    """The other named failure mode: eager import itself raising must also
    surface as `AdvisoryRosterUnavailable`, not propagate a bare exception
    or silently return a short/empty tuple."""

    def _boom():
        raise RuntimeError("synthetic eager-import failure for this test")

    monkeypatch.setattr(
        "coordinator_core.hooks._eager_import_all", _boom, raising=True
    )

    try:
        list_ported_advisory_ops()
    except AdvisoryRosterUnavailable as exc:
        assert "eager import" in str(exc)
    else:
        raise AssertionError(
            "list_ported_advisory_ops() did not raise AdvisoryRosterUnavailable "
            "when eager import itself raised"
        )
