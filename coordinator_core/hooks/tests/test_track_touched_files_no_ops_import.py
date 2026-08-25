"""
coordinator_core.hooks.tests.test_track_touched_files_no_ops_import — guards the
hygiene property C2 landed: importing the session-id resolver directly from
``coordinator_core.session.core`` rather than through
``coordinator_core.ops.session_context``.

HYGIENE, NOT A SAVING (docs/plans/2026-08-22-track-touched-files-pays-only-for-
the-append.md § C2). The withdrawn 390.6ms/850-module figure attached to the
``ops.session_context`` import was measured in a bare ``python3 -c``
interpreter — a shape this op is never invoked in. On both live invocation
shapes (warm engine, cold trampoline) the saving is ~zero; the only real
justification is that the hook's cost no longer depends on whether the
invoking process armed lazy ops. This test pins exactly that property, not a
budget: after a subagent-shaped ``_handler`` call, in a fresh interpreter with
lazy ops UNARMED, ``coordinator_core.ops.session_context`` must be absent from
``sys.modules``. Arming lazy ops would make the assertion pass for the wrong
reason (the whole ops package eagerly imported ahead of the call), so this
runs in a subprocess that never arms it.

Negative-spec:
    Do NOT assert anything about process/module counts or timing — the plan
    text this test backs explicitly retracts that framing as the wrong
    measurement shape. This guards import-graph hygiene only.
    Do NOT drop the assertion that the back-pointer write itself still landed
    — the goal is removing the IMPORT, not the WRITE (cs_compute_scope
    withholds ownerless agent dirs from every session for 30 minutes).

Spec backlink: docs/plans/2026-08-22-track-touched-files-pays-only-for-the-append.md § C2
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Spawns a real subprocess (fresh interpreter) — the property under test
# (sys.modules absence) is only meaningful in a process that never imported
# ops.session_context for any other reason; the current test process may
# already have it loaded via other test modules' imports.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run_subagent_shaped_handler(repo: "object") -> str:
    """Return combined stdout from a fresh-interpreter subagent-shaped
    ``_handler`` call, with a trailing sentinel line reporting whether
    ``coordinator_core.ops.session_context`` ended up in ``sys.modules``."""
    script = textwrap.dedent(
        f"""
        import asyncio
        import sys

        # Lazy ops deliberately left UNARMED — no COORDINATOR_CORE_LAZY_OPS
        # env var, no sys._coordinator_core_lazy_ops attribute set. This is
        # the shape the test needs: if the hook still reached the ops
        # package's eager sweep by some other path, arming lazy ops here
        # would mask that and make the assertion pass for the wrong reason.

        from coordinator_core.hooks.track_touched_files import _handler

        params = {{
            "session_id": "aabbccddeeff1234",
            "tool_name": "Write",
            "file_path": "coordinator_core/hooks/hygiene_probe.py",
            "agent_id": "deadbeefcafe0001",
        }}
        asyncio.run(_handler(params, repo_root={str(repo)!r}))

        print("OPS_SESSION_CONTEXT_LOADED=" + str(
            "coordinator_core.ops.session_context" in sys.modules
        ))
        """
    )
    import os

    # Hermeticity: this test process may itself be a dispatched session with
    # CLAUDE_CODE_SESSION_ID set in its real environment. Piece 2 (C7) reads
    # that var and would attribute the back-pointer to it instead of the
    # firing session_id, which is not what this test is pinning — strip it
    # from the child's environment (mirrors test_hooks_bookkeeping.py's
    # _isolate_em_session_env fixture).
    child_env = dict(os.environ)
    child_env.pop("CLAUDE_CODE_SESSION_ID", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env=child_env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout


class TestNoOpsSessionContextImport:
    """Pins the C2 hygiene property: the ops package is off this hook's import graph."""

    def test_ops_session_context_absent_after_subagent_fire(self, tmp_path) -> None:
        repo = tmp_path
        (repo / ".git" / "coordinator-sessions").mkdir(parents=True, exist_ok=True)
        stdout = _run_subagent_shaped_handler(str(repo / ".git"))
        assert "OPS_SESSION_CONTEXT_LOADED=False" in stdout, (
            "coordinator_core.ops.session_context must NOT be imported as a "
            "side effect of a subagent-shaped track_touched_files fire — the "
            "whole point of C2 was replacing that import with a direct "
            f"session.core import. Subprocess stdout: {stdout!r}"
        )

    def test_back_pointer_write_still_lands(self, tmp_path) -> None:
        """The import is gone; the back-pointer WRITE it fed is not (§ C2:
        removing the write needs a far higher evidence bar than removing the
        import, and is out of scope here)."""
        repo = tmp_path
        (repo / ".git" / "coordinator-sessions").mkdir(parents=True, exist_ok=True)
        _run_subagent_shaped_handler(str(repo / ".git"))
        bp = (
            repo / ".git" / "coordinator-sessions" / ".agents"
            / "deadbeefcafe0001" / "em-session-id.txt"
        )
        assert bp.exists(), (
            "back-pointer write must still land after the import swap — "
            "removing the WRITE was explicitly out of scope for C2"
        )
        assert bp.read_text().strip() == "aabbccddeeff1234"
