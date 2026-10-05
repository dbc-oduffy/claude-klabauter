"""No hook corpus row may signal or spawn a real process.

A row that reaches `os.kill` or a non-git `subprocess.Popen` can retire or
start a live box process (the resident http forwarder) while measuring a
message. `git` is allowed: rows build scratch repos with it.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards.tests import guard_message_corpus as corpus


def _is_git(args) -> bool:
    argv = args if isinstance(args, (list, tuple)) else [args]
    return bool(argv) and os.path.basename(str(argv[0])).lower() in ("git", "git.exe")


@pytest.mark.parametrize(
    "row", corpus.HOOK_ROWS, ids=lambda r: f"{r.guard}:{r.row_id}"
)
def test_hook_row_reaches_no_real_process_control(row, monkeypatch):
    real_popen = subprocess.Popen

    def guarded_popen(args, *a, **kw):
        if not _is_git(args):
            raise AssertionError(f"corpus row {row.guard} spawned a real process: {args!r}")
        return real_popen(args, *a, **kw)

    def guarded_kill(pid, sig):
        raise AssertionError(f"corpus row {row.guard} signalled real pid {pid} (sig {sig})")

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(os, "kill", guarded_kill)
    corpus.fire_hook_row(row)
