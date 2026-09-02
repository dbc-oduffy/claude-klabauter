"""`--params-file -` refuses honestly when the process has no stdin to read.

Why this failure shape exists at all: door_core.h ::
door_argv_declares_params_stdin (the full pythonw / AttributeError / -32603
/ -32004 causal chain lives there, once). Trail:
`state/bug-backlog/2026-09-02-warm-engine-door-returns-indeterminate-for-every-op.yaml`.

WHY THIS EXISTS ALONGSIDE THE DOOR'S OWN GATE. A current door decides this
route pre-delivery and falls through cold, so it never reaches this branch
without a stdin, pinned by
`coordinator_core/warm/door/tests/test_params_file_stdin_route.py`. Door
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

# THE `.buffer` LEG IS HERE BECAUSE TWO REVIEWERS DISAGREED ABOUT IT.
# overengineering-reviewer had it dropped as covering no caller this diff
# names; code-reviewer then named the shapes it does cover -- `io.StringIO`,
# an embedding host's stream wrapper. It stays because the failure it guards
# is not "a test asserts a hypothetical": a text stream standing in for
# `sys.stdin` raises `AttributeError` at `.buffer`, which the read's own
# `(OSError, UnicodeDecodeError)` catch does not cover, so it escapes as a
# -32603 -- the same escape that made a warm-served `--params-file -` answer
# -32004 in the first place. Reopening that class is what the leg costs.


class _TextOnlyStdin:
    """A stream present but carrying no binary buffer -- `io.StringIO`'s shape,
    and an embedding host's. It fails the same way `None` does at `.buffer`,
    so it must be refused the same way and not one layer later."""

    def read(self) -> str:  # pragma: no cover -- must never be reached
        raise AssertionError("the refusal must fire before any read")


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


def test_a_text_only_stdin_refuses_identically(monkeypatch, tmp_path):
    """The named shape from the disagreement above, pinned. Without the
    `.buffer` leg this raises `AttributeError` out of the op handler and the
    caller is told its op may have completed."""
    monkeypatch.setattr("sys.stdin", _TextOnlyStdin(), raising=False)
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
