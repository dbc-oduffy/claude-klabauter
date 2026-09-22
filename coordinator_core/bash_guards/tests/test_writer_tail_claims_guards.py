"""
coordinator_core.bash_guards.tests.test_writer_tail_claims_guards — AC5 for
C6 (migration batch B: coordinator_core/hooks and coordinator_core/bash_
guards to-fix writers onto the seam, with their entry paths).

One behavioural test per (entry-path class x seam entry point) pair this
batch migrated, driven through its REAL entry (`block_subagent_destructive_
action.check`, the real fail-open trigger), asserting the seam declares the
write. Watched RED with the seam call swapped back for the raw primitive it
replaced (``block_subagent_destructive_action._log_fail_open``'s
``open(log_path, "a") + fh.write``), per the row body's instruction: that
shape never calls ``declare_write`` at all, so the collected-declarations
list below stays empty even though the guard's own log file is written.

Entry-path class covered here: "bare process main (hook subprocess)" — the
package default C3's census recorded for every ``coordinator_core/bash_
guards/`` module (no ``entry_seam``/``reentrant_dispatch`` import anywhere
under the package at C3's ref). This batch closes that gap by wrapping
``dispatch.py::main`` ONCE in ``cli_entry.recording_declared_writes()``
rather than opening a collection per guard site (D4).

This asserts against `session.declared_writes.collecting()` directly
(pre-containment), NOT the touch record `test_writer_tail_claims_ops.py`'s
sibling test checks: `_fail_open_log_path` is deliberately settings-home-
rooted (module docstring, `_fail_open_log_path`), i.e. OUTSIDE the caller's
own repo, so `ipc._record_self_reported_touches`'s cross-repo containment
rule (ipc.py, "a declared path is recorded ONLY when it resolves inside the
CALLER's own repo") correctly drops it before it ever reaches the touch
record — this is why the module's register entry recategorizes to
outside-repo rather than staying to-fix (see test_raw_writes_have_a_
disposition.py). The seam call still fires and still declares; only the
final containment step (working as designed, unrelated to this migration)
filters it.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md
§ C6.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.session import declared_writes

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def test_fail_open_log_declares_through_the_seam(tmp_path, monkeypatch):
    """entry-path class: bare process main (hook subprocess, wrapped once at
    dispatch.py::main) x seam entry point: append_claimed_line. Drives
    `block_subagent_destructive_action.check` (the real fail-open trigger,
    no-agent-id-key branch) inside a `declared_writes.collecting()` scope
    (the same collection `cli_entry.recording_declared_writes` -- which
    `dispatch.main` now opens once, per D4 -- ultimately hands to the
    recorder), and asserts `declare_write` fired for the log path."""
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git rm --cached secret.txt"},
        "session_id": "sid-c6-fail-open",
        "cwd": str(tmp_path),
    }

    with declared_writes.collecting() as declared:
        result = guard.check(payload)

    assert result is None  # fail-open: no verdict, same as pre-migration
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "FAIL-OPEN" in content
    assert "branch='no-agent-id-key'" in content

    assert str(log_path) in declared, (
        "block_subagent_destructive_action._log_fail_open's write must be "
        "declared through the seam (session/claimed_write.py::"
        "append_claimed_line), not a raw open()+write that never calls "
        "declare_write"
    )
