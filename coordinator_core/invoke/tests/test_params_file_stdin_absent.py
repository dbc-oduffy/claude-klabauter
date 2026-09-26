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


class _TextOnlyStdin:

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
    assert "--params-file <path>" in envelope["error"]["message"]


def test_a_text_only_stdin_refuses_identically(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin", _TextOnlyStdin(), raising=False)
    _, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", "-"], str(tmp_path), allow_warm=False
    )
    assert exit_code == 1
    assert "--params-file -" in json.loads(stderr.strip())["error"]["message"]


def test_a_real_stdin_still_dispatches(monkeypatch, tmp_path):
    class _Stdin:
        buffer = io.BytesIO(b"{}")

    monkeypatch.setattr("sys.stdin", _Stdin(), raising=False)
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", "-"], str(tmp_path), allow_warm=False
    )
    assert exit_code == 0, stderr
    assert json.loads(stdout)["result"]["ok"] is True


def test_a_params_file_path_is_unaffected_by_an_absent_stdin(monkeypatch, tmp_path):
    params_path = tmp_path / "params.json"
    params_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", None, raising=False)
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", str(params_path)], str(tmp_path), allow_warm=False
    )
    assert exit_code == 0, stderr
    assert json.loads(stdout)["result"]["ok"] is True
