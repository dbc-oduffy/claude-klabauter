"""`--params-file -` refuses honestly when the process has no stdin to read.

THE FAILURE THIS REPLACES. `sys.stdin` is `None` in a warm pool worker --
spawned via `pythonw.exe` (`warm/server.py :: _suppress_pool_worker_consoles`),
whose companion `_bind_null_std_streams` rebinds stdout and stderr and
deliberately not stdin. `sys.stdin.buffer` therefore raised `AttributeError`,
which the read's `(OSError, UnicodeDecodeError)` catch does not cover, so it
escaped the op handler as a `-32603` -- and a `-32603` is not in
`is_provably_undispatched`'s set, so the door that carried it answered `-32004`
"the op may have COMPLETED" for a request whose params were never read. Trail:
`state/bug-backlog/2026-09-02-warm-engine-door-returns-indeterminate-for-every-op.yaml`.

WHY THIS EXISTS ALONGSIDE THE DOOR'S OWN GATE. A current door decides this
route pre-delivery and falls through cold, so it never reaches this branch
without a stdin (`door_core.h :: door_argv_declares_params_stdin`, pinned by
`coordinator_core/warm/door/tests/test_params_file_stdin_route.py`). Door
images are compiled binaries installed per machine and refreshed only by a
publish round, so every peer still running the previous image keeps sending
this shape until then. What they get now is a PRE-DISPATCH refusal naming the
cause -- which is provably undispatched, and therefore never an indeterminate
verdict about a mutation that did not run.
"""

from __future__ import annotations

import io
import json

from coordinator_core.invoke.__main__ import _dispatch_argv


class _NoBuffer:
    """A stream object present but carrying no binary buffer -- the shape a
    replacement stream (a test harness's capture, a `StringIO`) presents. It
    fails the same way `None` does and must be refused the same way."""


def test_absent_stdin_refuses_before_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin", None, raising=False)
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", "-"], str(tmp_path), allow_warm=False
    )

    assert exit_code == 1
    assert stdout == ""
    envelope = json.loads(stderr.strip())
    assert "--params-file -" in envelope["error"]["message"]
    # The refusal must name a route the caller can actually take.
    assert "--params-file <path>" in envelope["error"]["message"]


def test_a_stream_without_a_buffer_refuses_identically(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin", _NoBuffer(), raising=False)
    _, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", "-"], str(tmp_path), allow_warm=False
    )
    assert exit_code == 1
    assert "--params-file -" in json.loads(stderr.strip())["error"]["message"]


def test_a_real_stdin_still_dispatches(monkeypatch, tmp_path):
    """THE CONTROL. A refusal that fired whenever stdin was merely unusual
    would break the quoting-immune transport this flag exists to provide --
    the cold CLI path, where stdin is real, must be untouched."""
    class _Stdin:
        buffer = io.BytesIO(b"{}")

    monkeypatch.setattr("sys.stdin", _Stdin(), raising=False)
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", "-"], str(tmp_path), allow_warm=False
    )
    assert exit_code == 0, stderr
    assert json.loads(stdout)["result"]["ok"] is True


def test_a_params_file_path_is_unaffected_by_an_absent_stdin(monkeypatch, tmp_path):
    """The file form binds no stream, so it must keep working in exactly the
    process where the stdin form cannot."""
    params_path = tmp_path / "params.json"
    params_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", None, raising=False)
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", str(params_path)], str(tmp_path), allow_warm=False
    )
    assert exit_code == 0, stderr
    assert json.loads(stdout)["result"]["ok"] is True
