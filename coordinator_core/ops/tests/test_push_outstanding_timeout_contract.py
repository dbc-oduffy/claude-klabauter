"""Pins the fix that stopped `push.outstanding` reporting a timeout on a landed push.

`push.outstanding` used to live under `coordinator_core.ops.ceremony`, so
`ipc.is_ceremony_method` clamped its dispatch timeout to the 2.0s
`CEREMONY_BUDGET_SECS` -- the sink's 9 `outcome: "timeout"` rows
(`.git/coordinator-sessions/logs/op-latency*.jsonl`, all dated 2026-08-26,
clustered at ~2000-2015ms) are that clamp firing on a push that landed. Moving
the module out of the ceremony package (bd745a329b) fixed it: the op now
resolves at the 30s `DISPATCH_TIMEOUT_SECS` default. Zero timeout rows since,
through 2026-08-30.

This file pins that fix, it does not re-fix anything. The regression this
guards against is a future refactor moving `push_outstanding.py` back under
`coordinator_core.ops.ceremony` (or adding a `push.outstanding` row to
`_OP_TIMEOUT_OVERRIDES`) -- either would silently reintroduce the 2.0s clamp.

Residual NOT closed by this fix or this test: `ipc._timeout_for`'s guard is
`asyncio.wait_for`, which cancels the CALLER's wait, not server-side
execution. A push that outran 30s would still report `timeout` to the caller
while the git push itself keeps running and lands on the remote. Naming that
gap is this file's job; closing it is out of scope for this chunk.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import coordinator_core.ipc as ipc

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_push_outstanding_resolves_to_dispatch_timeout_not_ceremony_budget():
    """`_timeout_for("push.outstanding")` is 30.0s, never the 2.0s ceremony clamp."""
    resolved = ipc._timeout_for("push.outstanding")
    assert resolved == ipc.DISPATCH_TIMEOUT_SECS
    assert resolved != ipc.CEREMONY_BUDGET_SECS
    assert resolved == 30.0


def test_push_outstanding_is_not_ceremony_membership():
    """`push.outstanding` is not a ceremony op by any of the three membership signals.

    Not name-prefixed `ceremony.`, not in the alias table, and not resolved to the
    ceremony package by either `_REGISTRY` or `OP_MODULE_MAP` -- so
    `is_ceremony_method` cannot clamp it no matter what else has been imported.
    """
    assert not ipc.is_ceremony_method("push.outstanding")
    assert "push.outstanding" not in ipc._OP_TIMEOUT_OVERRIDES


def test_push_outstanding_timeout_is_stable_regardless_of_ceremony_import_order():
    """The 30s resolution does not depend on whether the ceremony package has been
    imported into the process.

    This is the exact shape of the original bug: `_owning_module_is_ceremony`
    consults `_REGISTRY` and `OP_MODULE_MAP`, both of which are populated only by
    prior imports. A cold process that has never imported
    `coordinator_core.ops._registry_map` or dispatched through the registry must
    resolve identically to a warm one that has -- otherwise the timeout an op gets
    would depend on unrelated import order rather than on where the op actually
    lives.
    """
    script = (
        "import coordinator_core.ipc as ipc\n"
        "assert ipc._timeout_for('push.outstanding') == ipc.DISPATCH_TIMEOUT_SECS, "
        "ipc._timeout_for('push.outstanding')\n"
        "assert not ipc.is_ceremony_method('push.outstanding')\n"
        "import coordinator_core.ops._registry_map as registry_map\n"
        "assert registry_map.OP_MODULE_MAP.get('push.outstanding') == "
        "'coordinator_core.ops.push_outstanding'\n"
        "assert ipc._timeout_for('push.outstanding') == ipc.DISPATCH_TIMEOUT_SECS\n"
        "assert not ipc.is_ceremony_method('push.outstanding')\n"
        "print('OK')\n"
    )
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # popup-intentional-last-resort
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=no_window,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
