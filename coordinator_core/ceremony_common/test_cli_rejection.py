"""Tests for coordinator_core.ceremony_common.cli_rejection — the shared
`classify_cli_exit` discriminator all three ceremony `_invoke_cli_main`
sites route through.

Spec backlink:
docs/plans/2026-08-15-bind-the-klabauter-publish-rows-into-a-parity-group.md, chunk C5
"""

from __future__ import annotations

from coordinator_core.ceremony_common.cli_rejection import (
    CliExitClass,
    classify_cli_exit,
    describe_exit_class,
)

_ARGPARSE_STDERR = (
    "usage: wsc-tail.py [-h] --sid SID [--adjudication-present]\n"
    "wsc-tail.py: error: unrecognized arguments: --partition-mandatory\n"
)


def test_raised_exit_2_with_argparse_stderr_classifies_argv_rejected():
    assert (
        classify_cli_exit(raised=True, code=2, stderr_text=_ARGPARSE_STDERR)
        is CliExitClass.ARGV_REJECTED
    )


def test_returned_exit_2_classifies_returned_even_with_argparse_shaped_stderr():
    # main() RETURNING 2 is never argv_rejected, regardless of stderr shape —
    # `raised` is one of the three required conditions, not incidental.
    assert (
        classify_cli_exit(raised=False, code=2, stderr_text=_ARGPARSE_STDERR)
        is CliExitClass.RETURNED
    )


def test_raised_exit_2_with_non_argparse_stderr_classifies_returned():
    # The zero-arg-trampoline case: the callee raised SystemExit(2) carrying
    # its own semantic exit — e.g. wsc-tail.py's documented "not a halt,
    # proceed" outcome — and its stderr is not an argparse rejection banner.
    stderr_text = "wsc-tail.py: partition mandate unresolved for sid s-1\n"
    assert (
        classify_cli_exit(raised=True, code=2, stderr_text=stderr_text)
        is CliExitClass.RETURNED
    )


def test_raised_exit_2_with_usage_line_but_no_error_marker_classifies_returned():
    # Both the usage banner AND the ": error: " marker are required —
    # a CLI that prints its own usage block on success/help paths must not
    # be misclassified.
    stderr_text = "usage: wsc-tail.py [-h] --sid SID\n"
    assert (
        classify_cli_exit(raised=True, code=2, stderr_text=stderr_text)
        is CliExitClass.RETURNED
    )


def test_raised_exit_2_with_error_marker_but_no_usage_line_classifies_returned():
    stderr_text = "wsc-tail.py: error: something else entirely\n"
    assert (
        classify_cli_exit(raised=True, code=2, stderr_text=stderr_text)
        is CliExitClass.RETURNED
    )


def test_raised_other_code_classifies_returned():
    assert (
        classify_cli_exit(raised=True, code=1, stderr_text=_ARGPARSE_STDERR)
        is CliExitClass.RETURNED
    )


def test_clean_exit_classifies_returned():
    assert (
        classify_cli_exit(raised=False, code=0, stderr_text="")
        is CliExitClass.RETURNED
    )


def test_describe_exit_class_is_nonempty_only_for_argv_rejected():
    assert describe_exit_class(CliExitClass.ARGV_REJECTED)
    assert describe_exit_class(CliExitClass.RETURNED) == ""
