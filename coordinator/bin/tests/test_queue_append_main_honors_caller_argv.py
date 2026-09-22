"""test_queue_append_main_honors_caller_argv.py — `main(argv)` must parse the
caller-supplied `argv`, not fall back to ambient `sys.argv`.

Spec backlink: state/bug-backlog/
2026-09-03-workstream-complete-s-queue-append-direc-50f009dc8b1f.yaml.
`main`'s own F12 pre-check already derives `_argv = sys.argv[1:] if argv is
None else argv` and honors it, but the actual `argparse` call immediately
below it read `parser.parse_args()` with no argument — argparse's own
default is `sys.argv[1:]`, so any caller passing an explicit `argv` differing
from the process's ambient `sys.argv` had it silently discarded. This is
exactly the in-process dispatch shape `coordinator_core.ceremony_common.
cli_dispatch.invoke_cli_main` uses for any `main(argv)`-taking CLI (a `main`
with parameters skips that primitive's zero-arg-trampoline `sys.argv`
splice and calls `main_fn(list(args))` directly) — `workstream_complete`'s
directive runner reaches this exact path, in-process, in the same
interpreter as its own unrelated `sys.argv`.

Distinguishing argv shape: `sys.argv` is set to a bare program name (would
raise `SystemExit(2)` with argparse's OWN "the following arguments are
required: --schema" message if read) while the caller-supplied `argv` is
`["--schema"]` (a --schema flag present but missing its value, which
argparse rejects with the DIFFERENT message "argument --schema: expected
one argument"). Only the explicit-argv message can appear if `main` is
actually parsing what was passed to it. Chosen deliberately so the assertion
never reaches `_bootstrap_imports`'s native-dispatch calls (`schema.
describe`/`schema.validate`) — argparse rejects at `parser.parse_args()`
itself, before any of that runs.

Verified failing against the pre-fix module (`args = parser.parse_args()`):
reproduces the row's reported symptom byte-for-byte — argparse names
`--schema` as missing even though the caller's own `argv` carries it.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from coordinator_core.ceremony_common.cli_dispatch import load_cli_module

pytestmark = [pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_QUEUE_APPEND_CLI = _BIN_DIR / "coordinator-queue-append.py"


def test_main_parses_explicit_argv_not_ambient_sys_argv(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(_BIN_DIR))
    monkeypatch.setattr(sys, "argv", ["coordinator-queue-append.py"])

    module = load_cli_module("test_queue_append_main_honors_caller_argv_module", _QUEUE_APPEND_CLI)

    stderr_buf = io.StringIO()
    with redirect_stderr(stderr_buf):
        with pytest.raises(SystemExit) as exc:
            module.main(["--schema"])

    assert exc.value.code == 2
    stderr_text = stderr_buf.getvalue()
    assert "argument --schema: expected one argument" in stderr_text, (
        "main(argv) did not parse the explicit argv it was given -- expected "
        "argparse's error for a valueless --schema (from the passed-in argv), "
        f"got:\n{stderr_text}"
    )
    assert "the following arguments are required: --schema" not in stderr_text, (
        "main(argv) fell back to ambient sys.argv (a bare program name, which "
        "argparse reports as a wholly-missing --schema) instead of the "
        f"caller-supplied argv:\n{stderr_text}"
    )
